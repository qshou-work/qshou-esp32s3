#!/usr/bin/env python3
# 严格等价复刻 ESP-IDF v6.1 components/esp_hid/src/esp_hid_common.c 的
# esp_hid_parse_report_map 状态机，用于验证我们的 125 字节复合描述符会被
# 解析成几个 INPUT 报告、各自的 Report ID 与长度。
# 仅用于本地验证，不改任何固件。

import re, sys

# ---- HID_ITEM 参数映射（hid_descriptor.c / .h 里的命名常量）----
TAG = {
    'HID_TAG_USAGE_PAGE':0, 'HID_TAG_LOGICAL_MIN':1, 'HID_TAG_LOGICAL_MAX':2,
    'HID_TAG_REPORT_SIZE':7, 'HID_TAG_REPORT_ID':8, 'HID_TAG_REPORT_COUNT':9,
    'HID_TAG_USAGE':0, 'HID_TAG_USAGE_MIN':1, 'HID_TAG_USAGE_MAX':2,
    'HID_TAG_INPUT':8, 'HID_TAG_COLLECTION':10, 'HID_TAG_END_COLLECT':12,
}
TYPE = {'HID_TYPE_MAIN':0, 'HID_TYPE_GLOBAL':1, 'HID_TYPE_LOCAL':2}

# 数据字节里的命名常量（来自 hid_descriptor.h / hid_report.h）
DATA = {
    'QSHOU_HID_USAGE_PAGE_DESKTOP':'0x01', 'QSHOU_HID_USAGE_PAGE_KEYBOARD':'0x07',
    'QSHOU_HID_USAGE_PAGE_CONSUMER':'0x0C', 'QSHOU_HID_USAGE_PAGE_BUTTON':'0x09',
    'QSHOU_HID_USAGE_MOUSE':'0x02', 'QSHOU_HID_USAGE_KEYBOARD':'0x06',
    'QSHOU_HID_USAGE_CONSUMER_CONTROL':'0x01', 'QSHOU_HID_USAGE_POINTER':'0x01',
    'QSHOU_HID_USAGE_X':'0x30', 'QSHOU_HID_USAGE_Y':'0x31',
    'HID_REPORT_ID':'0x01', 'HID_REPORT_ID_KEYBOARD':'0x02', 'HID_REPORT_ID_CONSUMER':'0x03',
    'HID_COLLECTION_APPLICATION':'0x01', 'HID_COLLECTION_PHYSICAL':'0x00',
    'HID_INPUT_DATA_VAR_ABS':'0x02', 'HID_INPUT_CONST_PAD':'0x03', 'HID_INPUT_DATA_VAR_REL':'0x06',
}

# HID_RM_* 命令字节 = item_byte & 0xFC（与 esp_hid_common.c 的 parse_cmd 一致）
def item_byte(tag, typ, size):
    return (tag << 4) | (typ << 2) | size

HID_RM_USAGE_PAGE        = item_byte(0,1,1) & 0xFC   # 0x04
HID_RM_USAGE             = item_byte(0,2,1) & 0xFC   # 0x08
HID_RM_COLLECTION        = item_byte(10,0,1) & 0xFC  # 0xA0
HID_RM_REPORT_ID         = item_byte(8,1,1) & 0xFC   # 0x84
HID_RM_REPORT_SIZE       = item_byte(7,1,1) & 0xFC   # 0x74
HID_RM_REPORT_COUNT      = item_byte(9,1,1) & 0xFC   # 0x94
HID_RM_INPUT             = item_byte(8,0,1) & 0xFC   # 0x80
HID_RM_OUTPUT            = item_byte(9,0,1) & 0xFC   # 0x90
HID_RM_FEATURE           = item_byte(11,0,1) & 0xFC  # 0xB0
HID_RM_END_COLLECTION    = item_byte(12,0,0) & 0xFC  # 0xC0
HID_RM_USAGE_MIN         = item_byte(1,2,1) & 0xFC   # 0x18
HID_RM_USAGE_MAX         = item_byte(2,2,1) & 0xFC   # 0x28
HID_RM_LOGICAL_MIN       = item_byte(1,1,1) & 0xFC   # 0x14
HID_RM_LOGICAL_MAX       = item_byte(2,1,1) & 0xFC   # 0x24

# handle_report 里用到的 usage（与 esp_hid_common.h 一致）
GENERIC_DESKTOP = 0x01
USAGE_KEYBOARD  = 0x06
USAGE_MOUSE     = 0x02
CONSUMER_PAGE   = 0x0C
CONSUMER_CTRL   = 0x01

# ---- 解析 hid_descriptor.c，提取真实字节序列 ----
src = open('firmware/ble_hid/hid_descriptor.c', encoding='utf-8').read()
m = re.search(r'qshou_hid_report_map\[\]\s*=\s*\{(.*?)\};', src, re.S)
block = m.group(1)
block = re.sub(r'/\*.*?\*/', '', block, flags=re.S)  # 剥注释，避免误抓
# 按 HID_ITEM( 切分；每段首个 ')' 之后到段尾为该条数据字节
chunks = re.split(r'HID_ITEM\(', block)[1:]
descriptor = []
for ch in chunks:
    head, rest = ch.split(')', 1)
    args = [a.strip() for a in head.split(',')]
    tag = TAG[args[0]]; typ = TYPE[args[1]]; size = int(args[2])
    b = item_byte(tag, typ, size)
    # 抽数据 token：十六进制字面量或已知命名常量
    toks = re.findall(r'0x[0-9A-Fa-f]+|[A-Za-z_][A-Za-z0-9_]*', rest)
    vals = []
    for t in toks:
        try:
            v = int(t, 0)
        except ValueError:
            if t in DATA:
                v = int(DATA[t], 0)
            else:
                print(f"  [warn] 未知数据 token: {t} (跳过)")
                continue
        vals.append(v & 0xFF)
    descriptor.append((b, vals))

# ---- 复刻状态机 ----
WAIT_USAGE_PAGE, WAIT_USAGE, WAIT_COLLECTION_APPLICATION, WAIT_END_COLLECTION = 0,1,2,3

reports = []  # (usage, report_id, report_type, protocol_mode, value_len)
usage_mask = 0
HID_USAGE_KEYBOARD_M = 1 << 0
HID_USAGE_MOUSE_M    = 1 << 1
HID_USAGE_CCONTROL_M = 1 << 4

def add_report(usage, rid, rtype, pmode, vlen):
    reports.append((usage, rid, rtype, pmode, vlen))

def handle_report(rp):
    global usage_mask
    if rp['usage_page'] == GENERIC_DESKTOP and rp['usage'] == USAGE_KEYBOARD:
        usage_mask |= HID_USAGE_KEYBOARD_M
        if rp['input_len'] > 0:
            add_report(USAGE_KEYBOARD, rp['report_id'], 'INPUT', 'REPORT', rp['input_len']//8)
            add_report(USAGE_KEYBOARD, rp['report_id'], 'INPUT', 'BOOT', 8)
    elif rp['usage_page'] == GENERIC_DESKTOP and rp['usage'] == USAGE_MOUSE:
        usage_mask |= HID_USAGE_MOUSE_M
        if rp['input_len'] > 0:
            add_report(USAGE_MOUSE, rp['report_id'], 'INPUT', 'REPORT', rp['input_len']//8)
            add_report(USAGE_MOUSE, rp['report_id'], 'INPUT', 'BOOT', 3)
    else:
        cusage = 'GENERIC'
        if rp['usage_page'] == GENERIC_DESKTOP and rp['usage'] == 0x04:  # joystick
            cusage = 'JOYSTICK'
        elif rp['usage_page'] == CONSUMER_PAGE and rp['usage'] == CONSUMER_CTRL:
            usage_mask |= HID_USAGE_CCONTROL_M
            cusage = 'CCONTROL'
        if rp['input_len'] > 0:
            add_report(cusage, rp['report_id'], 'INPUT', 'REPORT', rp['input_len']//8)

step = WAIT_USAGE_PAGE
depth = 0
new_map = True
rp = {'usage_page':0,'usage':0,'inner_usage_page':0,'inner_usage':0,
      'report_id':0,'input_len':0,'output_len':0,'feature_len':0}

for (b, vals) in descriptor:
    cmd = b & 0xFC
    value = 0
    for i, v in enumerate(vals):
        value |= (v << (8*i))
    # parse_cmd 等效：cmd=byte&0xFC, value=小端
    if step == WAIT_USAGE_PAGE:
        if cmd != HID_RM_USAGE_PAGE:
            print(f"  [parse] expected USAGE_PAGE at byte 0x{b:02x}, got cmd 0x{cmd:02x}"); break
        rp = {'usage_page':value,'usage':0,'inner_usage_page':0,'inner_usage':0,
              'report_id':0,'input_len':0,'output_len':0,'feature_len':0}
        step = WAIT_USAGE
    elif step == WAIT_USAGE:
        if cmd != HID_RM_USAGE:
            print(f"  [parse] expected USAGE, got cmd 0x{cmd:02x}"); step=WAIT_USAGE_PAGE; continue
        rp['usage'] = value
        step = WAIT_COLLECTION_APPLICATION
    elif step == WAIT_COLLECTION_APPLICATION:
        if cmd != HID_RM_COLLECTION or value != 1:
            print(f"  [parse] expected COLLECTION(APP), got cmd 0x{cmd:02x} val {value}"); step=WAIT_USAGE_PAGE; continue
        rp['report_id'] = 0
        depth = 1
        step = WAIT_END_COLLECTION
    elif step == WAIT_END_COLLECTION:
        if cmd == HID_RM_REPORT_ID:
            if rp['report_id'] and rp['report_id'] != value:
                # 收尾上一个 report id
                if rp['input_len'] & 7: print("  INPUT not byte-aligned"); 
                else: handle_report(rp); 
                new_map = False
                rp['input_len']=rp['output_len']=rp['feature_len']=0
                rp['usage']=rp['inner_usage']; rp['usage_page']=rp['inner_usage_page']
            rp['report_id'] = value
        elif cmd == HID_RM_USAGE_PAGE:
            rp['inner_usage_page'] = value
        elif cmd == HID_RM_USAGE:
            rp['inner_usage'] = value
        elif cmd == HID_RM_REPORT_SIZE:
            rp['_size'] = value
        elif cmd == HID_RM_REPORT_COUNT:
            rp['_count'] = value
        elif cmd == HID_RM_INPUT:
            add = rp['_size'] * rp['_count']
            rp['input_len'] += add
        elif cmd == HID_RM_OUTPUT:
            rp['output_len'] += rp['_size'] * rp['_count']
        elif cmd == HID_RM_FEATURE:
            rp['feature_len'] += rp['_size'] * rp['_count']
        elif cmd == HID_RM_COLLECTION:
            depth += 1
        elif cmd == HID_RM_END_COLLECTION:
            depth -= 1
            if depth == 0:
                if rp['input_len'] & 7: print("  INPUT not byte-aligned"); 
                else: handle_report(rp)
                new_map = False
                step = WAIT_USAGE_PAGE

print("=== 描述符解析结果（等价 ESP-IDF 解析器）===")
print(f"字节数: {len(descriptor)}  usage_mask={usage_mask:#x}")
print(f"解析出报告数: {len(reports)}")
for i,(u,rid,rt,pm,vl) in enumerate(reports):
    print(f"  [{i}] usage={u} report_id={rid} type={rt} mode={pm} value_len={vl}")

# 模拟 create_hid_db：REPORT-mode 报告各自注册一个特征
print("\n=== create_hid_db 将注册的特征（REPORT-mode 报告）===")
reg = [r for r in reports if r[3]=='REPORT']
for i,(u,rid,rt,pm,vl) in enumerate(reg):
    print(f"  rpt[{i}] id={rid} type={rt} len={vl}")
print(f"\n注册特征数 = {len(reg)}  (MAX_REPORTS=3)")
if len(reg) > 3:
    print("  ⚠️ 超过 MAX_REPORTS → 会被截断！")
else:
    print("  ✓ 不超过上限，3 个 report 全部注册。")
