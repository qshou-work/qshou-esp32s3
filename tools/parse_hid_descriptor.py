#!/usr/bin/env python3
"""
tools/parse_hid_descriptor.py — 独立解析固件 HID descriptor 并校验规格一致性

为什么要这个脚本：
    「C 代码能编译」不等于「descriptor 语义正确」。HID report descriptor 的
    字段长度是隐式约定（Report Size × Report Count + padding），一旦算错，
    设备会向中心端宣告错误的报告长度 —— 表现为「能连上但点击无反应」，
    是极难排查的故障模式。本脚本用独立实现解析 descriptor，逐项比对规格。

校验项（对应 docs/ble_descriptor_review.md 第 1 节）：
    1. Usage Page = Digitizer (0x0D)
    2. Usage = Touch Screen (0x04)
    3. Report ID = 0x01
    4. 单个 Input 报告总长度 = 5 字节（与 HID_REPORT_LEN 一致）
    5. X / Y 各 16 bit，Logical Max = 10000
    6. 字段顺序为 Tip Switch, In Range, padding, X, Y（state 独立成第 1 字节）
    7. 单一 Application Collection，含且仅含 1 个 Report ID

零第三方依赖，直接 python 运行。
"""
import re
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESC_C = os.path.join(ROOT, "firmware", "ble_hid", "hid_descriptor.c")


def read_source():
    """读取 descriptor 源文件原文（未去注释，供 #define 提取用）。"""
    with open(DESC_C, encoding="utf-8") as f:
        return f.read()


def collect_defines(src):
    """
    从源文件里提取简单的整数 #define，支持：
        #define NAME 0x02
        #define NAME (1u << 2)
        #define NAME (OTHER_MACRO)          /* 别名 */
    只认整数字面量；无法求值的（如带运算/条件编译）忽略，由调用方兜底。
    """
    defs = {}
    pending = []
    for m in re.finditer(r"^[ \t]*#define[ \t]+([A-Z_][A-Z0-9_]*)[ \t]+(.+?)[ \t]*(?:/\*.*)?$",
                         src, flags=re.M):
        name, expr = m.group(1), m.group(2).strip()
        mm = re.fullmatch(r"\(\s*1u?\s*<<\s*(\d+)\s*\)", expr)
        if mm:
            defs[name] = 1 << int(mm.group(1))
            continue
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+|\d+", expr):
            defs[name] = int(expr, 0)
            continue
        mm = re.fullmatch(r"\(\s*([A-Z_][A-Z0-9_]*)\s*\)", expr)
        if mm:
            pending.append((name, mm.group(1)))
    # 解析别名（宏引用宏），最多两轮
    for _ in range(2):
        for name, ref in pending:
            if name not in defs and ref in defs:
                defs[name] = defs[ref]
    return defs

ITEM_TYPE_MAIN, ITEM_TYPE_GLOBAL, ITEM_TYPE_LOCAL = 0, 1, 2
TAG_MAIN_INPUT, TAG_MAIN_COLLECTION, TAG_MAIN_END_COLLECTION = 8, 10, 12
TAG_G_USAGE_PAGE, TAG_G_LOGICAL_MIN, TAG_G_LOGICAL_MAX = 0, 1, 2
TAG_G_PHYSICAL_MAX = 4
TAG_G_REPORT_SIZE, TAG_G_REPORT_ID, TAG_G_REPORT_COUNT = 7, 8, 9
TAG_L_USAGE = 0


def parse_c_array(path):
    """从 C 源文件提取 qshou_hid_report_map[] 的字节序列。

    支持以下写法（本项目的实际写法）：
      HID_ITEM(TAG, TYPE, SIZE), VALUE,          → 2 字节
      HID_ITEM(TAG, TYPE, SIZE), V1, V2,         → 3 字节（size=2）
      HID_ITEM(...), 0x00,                       → 1 字节 payload
      HID_ITEM(..., 0),                          → size=0，无 payload
    """
    src = open(path, encoding="utf-8").read()
    m = re.search(r"qshou_hid_report_map\[\]\s*=\s*\{(.*?)\n\};", src, re.S)
    if not m:
        raise SystemExit("ERROR: 找不到 qshou_hid_report_map[] 定义")
    body = m.group(1)

    # 宏展开：HID_ITEM(tag, type, size)
    def expand_item(mo):
        tag = mo.group(1).strip()
        typ = mo.group(2).strip()
        size = int(mo.group(3).strip())
        # 解析符号 tag
        tmap = {
            "HID_TAG_USAGE_PAGE": TAG_G_USAGE_PAGE, "HID_TAG_LOGICAL_MIN": TAG_G_LOGICAL_MIN,
            "HID_TAG_LOGICAL_MAX": TAG_G_LOGICAL_MAX, "HID_TAG_PHYSICAL_MIN": 3,
            "HID_TAG_PHYSICAL_MAX": TAG_G_PHYSICAL_MAX, "HID_TAG_REPORT_SIZE": TAG_G_REPORT_SIZE,
            "HID_TAG_REPORT_ID": TAG_G_REPORT_ID, "HID_TAG_REPORT_COUNT": TAG_G_REPORT_COUNT,
            "HID_TAG_USAGE": TAG_L_USAGE, "HID_TAG_USAGE_MIN": 1, "HID_TAG_USAGE_MAX": 2,
            "HID_TAG_INPUT": TAG_MAIN_INPUT, "HID_TAG_COLLECTION": TAG_MAIN_COLLECTION,
            "HID_TAG_END_COLLECT": TAG_MAIN_END_COLLECTION, "HID_TAG_FEATURE": 11,
        }
        tmap_type = {"HID_TYPE_MAIN": ITEM_TYPE_MAIN, "HID_TYPE_GLOBAL": ITEM_TYPE_GLOBAL,
                     "HID_TYPE_LOCAL": ITEM_TYPE_LOCAL}
        t = tmap.get(tag)
        ty = tmap_type.get(typ)
        if t is None or ty is None:
            raise SystemExit(f"ERROR: 未知符号 tag={tag} type={typ}")
        return str((t << 4) | (ty << 2) | size)

    body = re.sub(r"HID_ITEM\(\s*([A-Z_]+)\s*,\s*([A-Z_]+)\s*,\s*(\d+)\s*\)", expand_item, body)
    # 去掉注释
    body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
    body = re.sub(r"//[^\n]*", " ", body)

    # 逐个 token 取值
    toks = [t.strip() for t in body.split(",")]
    syms = {
        "QSHOU_HID_USAGE_PAGE_DIGITIZER": 0x0D, "QSHOU_HID_USAGE_TOUCH_SCREEN": 0x04,
        "QSHOU_HID_USAGE_X": 0x30, "QSHOU_HID_USAGE_Y": 0x31,
        "HID_COLLECTION_APPLICATION": 0x01,
        "HID_REPORT_ID": 0x01,
    }
    # ⚠️ 不要在这里硬编码 HID_INPUT_* 的值！
    # 曾经硬编码成 HID_INPUT_DATA=0x01 / HID_INPUT_ABS=0x04（错的），
    # 于是 C 端的错误宏定义被工具"复现"，校验永远 PASS，bug 被彻底掩盖。
    # 现在改为从源文件自动提取 #define，宏改了工具就跟着变，不会再脱节。
    syms.update(collect_defines(read_source()))
    vals = []
    for t in toks:
        if not t:
            continue
        if t in syms:
            vals.append(syms[t])
            continue
        if t.startswith("(") or "|" in t:
            # 形如 (HID_INPUT_DATA | HID_INPUT_VAR | HID_INPUT_ABS) 或 0x00
            expr = t.strip("()")
            acc = 0
            for part in expr.split("|"):
                part = part.strip()
                if part in syms:
                    acc |= syms[part]
                elif part:
                    acc |= int(part, 0)
            vals.append(acc)
            continue
        try:
            vals.append(int(t, 0))
        except ValueError:
            raise SystemExit(f"ERROR: 无法解析 token: {t!r}")
    return vals


def iter_items(data):
    """按 HID 短条目格式切分 (tag, type, size, payload)。"""
    i = 0
    while i < len(data):
        b = data[i]
        size = b & 0x03
        size = 4 if size == 3 else size
        typ = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        if i + 1 + size > len(data):
            raise SystemExit(f"ERROR: 条目在 offset {i} 处越界")
        payload = data[i + 1: i + 1 + size]
        yield i, tag, typ, size, payload
        i += 1 + size


def main():
    data = parse_c_array(DESC_C)
    print(f"descriptor 字节数: {len(data)}")
    print("原始字节: " + " ".join(f"{b:02X}" for b in data))
    print()

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        print(f"  [{'ok ' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    items = list(iter_items(data))

    # ---- 1/2: Usage Page + Usage ----
    usage_pages = [p[0] for _, t, ty, s, p in items
                   if ty == ITEM_TYPE_GLOBAL and t == TAG_G_USAGE_PAGE and s == 1]
    check("Usage Page = Digitizer (0x0D)",
          len(usage_pages) >= 1 and usage_pages[0] == 0x0D, f"got {usage_pages[:2]}")

    top_usage = None
    for _, t, ty, s, p in items:
        if ty == ITEM_TYPE_LOCAL and t == TAG_L_USAGE and s == 1:
            top_usage = p[0]
            break
    check("首个 Usage = Touch Screen (0x04)", top_usage == 0x04, f"got {top_usage}")

    # ---- 7: 单一 Application Collection ----
    colls = [p[0] for _, t, ty, s, p in items
             if ty == ITEM_TYPE_MAIN and t == TAG_MAIN_COLLECTION]
    ends = [1 for _, t, ty, s, p in items
            if ty == ITEM_TYPE_MAIN and t == TAG_MAIN_END_COLLECTION]
    check("单一 Application Collection 且正确闭合",
          colls == [0x01] and len(ends) == 1, f"colls={colls} ends={len(ends)}")

    # ---- 3: Report ID ----
    report_ids = [p[0] for _, t, ty, s, p in items
                  if ty == ITEM_TYPE_GLOBAL and t == TAG_G_REPORT_ID]
    check("Report ID = 0x01（且仅 1 个）",
          report_ids == [0x01], f"got {report_ids}")

    # ---- 4/6: Input 报告字段长度与顺序 ----
    report_size, report_count = 0, 0
    log_max = None
    phys_max = None
    fields = []          # (bits, is_constant, usage)
    cur_usage = None
    for _, t, ty, s, p in items:
        if ty == ITEM_TYPE_GLOBAL:
            v = int.from_bytes(p, "little")
            if t == TAG_G_REPORT_SIZE:
                report_size = v
            elif t == TAG_G_REPORT_COUNT:
                report_count = v
            elif t == TAG_G_LOGICAL_MAX:
                log_max = v
            elif t == TAG_G_PHYSICAL_MAX:
                phys_max = v
        elif ty == ITEM_TYPE_LOCAL and t == TAG_L_USAGE and s == 1:
            cur_usage = p[0]
        elif ty == ITEM_TYPE_MAIN and t == TAG_MAIN_INPUT:
            bits = report_size * report_count
            # ⚠️ HID 规范：Input 标志 bit0=1 表示 **Constant**（常量/保留位）。
            # 曾写成 `(p[0] & 0x01) == 0`（判反），导致「数据字段被标成常量、
            # 填充位被标成数据」这类致命错误被判为 PASS —— 务必保持 != 0。
            is_const = (p[0] & 0x01) != 0
            # bit2=1 表示 Relative；触摸屏/绝对坐标设备必须为 Absolute。
            is_relative = (p[0] & 0x04) != 0
            fields.append({"bits": bits, "const": is_const,
                           "rel": is_relative, "usage": cur_usage})
            cur_usage = None

    total_bits = sum(f["bits"] for f in fields)
    check("Input 报告总长度 = 5 字节",
          total_bits == 40, f"total_bits={total_bits} (={total_bits // 8}B)")

    # 字段顺序：Tip(1) InRange(1) pad(6) X(16) Y(16)
    exp = [(1, False, 0x42), (1, False, 0x32), (6, True, None), (16, False, 0x30), (16, False, 0x31)]
    got = [(f["bits"], f["const"], f["usage"]) for f in fields]
    check("字段顺序 = Tip(1) InRange(1) pad(6) X(16) Y(16)", got == exp,
          f"got {got}")

    # ---- Input 标志位语义（曾经被漏检，导致 descriptor 原形毕露不了）----
    # 预期：Tip/InRange/X/Y 为 Data(非 Const) 且 Absolute；pad 为 Const。
    data_fields = [(0x42, "Tip Switch"), (0x32, "In Range"),
                   (0x30, "X"), (0x31, "Y")]
    for usage, name in data_fields:
        f = next((x for x in fields if x["usage"] == usage), None)
        if f is None:
            check(f"{name}: 存在 Input 条目", False, "not found")
            continue
        check(f"{name}: Input 标志 = Data 且 Absolute",
              (not f["const"]) and (not f["rel"]),
              f"const={f['const']} relative={f['rel']} (应为 False/False)")

    pads = [x for x in fields if x["usage"] is None]
    check("填充位: Input 标志 = Constant",
          bool(pads) and all(p["const"] for p in pads),
          f"pads={[(p['bits'], p['const']) for p in pads]} (const 应全为 True)")

    # state 必须是第 1 字节
    first_byte_bits = fields[0]["bits"] + fields[1]["bits"] + fields[2]["bits"] if len(fields) >= 3 else 0
    check("state（Tip+InRange+pad）恰为第 1 字节", first_byte_bits == 8,
          f"bits={first_byte_bits}")

    # ---- 5: X/Y 16bit 且 Logical Max 10000 ----
    xy = [f for f in fields if f["usage"] in (0x30, 0x31)]
    check("X/Y 均为 16 bit", len(xy) == 2 and all(f["bits"] == 16 for f in xy),
          f"got {[f['bits'] for f in xy]}")
    check("Logical Max = 10000", log_max == 10000, f"got {log_max}")
    check("Physical Max = 10000", phys_max == 10000, f"got {phys_max}")

    fails = sum(1 for c in checks if not c["ok"])
    print()
    print(f"checks: {len(checks)}  passed: {len(checks) - fails}  fails: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
