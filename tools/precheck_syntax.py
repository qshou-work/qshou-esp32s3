#!/usr/bin/env python3
"""
precheck_syntax.py — 单文件语法预检（秒级），替代全量编译去做「改完代码先验证」。

背景：ESP-IDF 全量重编译很慢，改动 C 代码后不应动辄整编。本脚本从既有
build/compile_commands.json 里取出该目标文件的真实编译 flag（-D / -I / -std），
再用同一交叉编译器跑 `-fsyntax-only`，几秒内抓出拼写错误、漏 include、
隐式函数声明等问题，**不生成任何 .o、不触碰 build 产物**。

关键取舍：
  - 加 `-Werror=implicit-function-declaration`：堵住「函数名写错 → 被隐式声明
    → 链接阶段才炸」这一 IDF 项目里最常见的坑。
  - `-fsyntax-only` 只做词法/语法/语义分析，不做代码生成，速度最快。

用法（在 qshou/ 下执行）：
    python tools/precheck_syntax.py firmware/platform/esp32/platform_net.c
    python tools/precheck_syntax.py file1.c file2.c --db firmware/build/compile_commands.json

局限：只能静态检查语法/类型，无法验证链接与运行期行为；真机验证仍需要真构建。
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys

# 编译器不一定在 PATH 里，补几条常见位置；shlex 解析出的 argv[0] 若不可执行再试这些
TOOLCHAIN_HINTS = [
    r"C:\Users\<username>\.espressif\tools\xtensa-esp-elf\esp-15.2.0_20251204\xtensa-esp-elf\bin",
    r"C:\Users\<username>\.espressif\tools\riscv32-esp-elf\esp-13.2.0_20240530\riscv32-esp-elf\bin",
]

# 「后面不跟引号的反斜杠」＝ Windows 路径分隔符，需归一为正斜杠
PATH_BSLASH = re.compile(r"\\(?!['\"])")

# 预检额外告警。刻意【不加】-Wall/-Wextra：模板里已经带 IDF 自己的告警集且带 -Werror，
# 额外加严会把系统头文件里的既有告警升级成 error，掩盖我们真正关心的报错。
WARN_FLAGS = [
    "-Werror=implicit-function-declaration",  # 堵住「函数名写错 → 链接期才炸」
    "-fsyntax-only",
]


def load_db(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pick_template(db, src):
    """找一个编译 flag 模板：优先同名条目，其次同目录条目，最后第一条。"""
    base = os.path.basename(src).lower()
    for e in db:
        if os.path.basename(e.get("file", "")).lower() == base:
            return e
    d = os.path.dirname(os.path.abspath(src)).lower()
    for e in db:
        if os.path.dirname(os.path.abspath(e.get("file", ""))).lower() == d:
            return e
    return db[0] if db else None


def to_argv(e):
    cmd = e.get("command") or e.get("arguments")
    if isinstance(cmd, list):
        return list(cmd)
    # CMake 生成的 command 是「shell 转义串」，里面反斜杠有两种语义：
    #   1) Windows 路径分隔符   C:\Users\...\xtensa-esp32s3-elf-gcc.exe
    #   2) 转义引号             -DIDF_VER=\"v6.1-dirty\"
    # 直接 shlex(posix=True) 会把 \U / \t 当转义吃掉（路径变成 C:Users<username>...）；
    # 直接 shlex(posix=False) 又会保留 \" 让 gcc 报 "missing terminating \" character"。
    # 解法：先把「后面不是引号的反斜杠」统一换成 /（Windows 接受正斜杠），再按 posix 解析。
    return shlex.split(PATH_BSLASH.sub("/", cmd), posix=True)


def strip_output_and_src(argv):
    """移除 -o <obj>、-c、-MMD 等产出相关的 flag，只保留 -D / -I / -std。"""
    out, skip_next = [], False
    drop_exact = {"-c", "-MD", "-MT"}
    drop_prefix = ("-MF", "-MQ", "-MMD", "-MP")
    for i, tok in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if tok == "-o":
            skip_next = True
            continue
        if tok in drop_exact or tok.startswith(drop_prefix):
            # -MF xxx 这类二元组也要跟随跳过
            if tok in ("-MF", "-MQ", "-MT"):
                skip_next = True
            continue
        out.append(tok)
    return out


def resolve_compiler(argv0):
    if os.path.exists(argv0):
        return argv0
    for d in TOOLCHAIN_HINTS:
        cand = os.path.join(d, os.path.basename(argv0))
        if os.path.exists(cand):
            return cand
    return argv0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="要预检的 C/C++ 源文件")
    ap.add_argument("--db", default=None, help="compile_commands.json 路径")
    ap.add_argument("--host", action="store_true",
                    help="按 host 接缝预检（把 -DPLATFORM_ESP32=1 换成 -DPLATFORM_HOST=1）")
    args = ap.parse_args()

    db_path = args.db
    if db_path is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(here, "firmware", "build", "compile_commands.json")
    if not os.path.exists(db_path):
        sys.stderr.write("找不到 %s（需要先有过一次真机构建）\n" % db_path)
        return 2

    db = load_db(db_path)
    if not db:
        sys.stderr.write("compile_commands.json 为空\n")
        return 2

    rc = 0
    for src in args.files:
        src = src.replace("\\", "/")
        if not os.path.exists(src):
            print("✗ %s —— 文件不存在" % src)
            rc = 1
            continue
        e = pick_template(db, src)
        if e is None:
            print("✗ %s —— 找不到编译模板" % src)
            rc = 1
            continue

        argv = strip_output_and_src(to_argv(e))
        # 去掉模板里原来的源文件（任意 .c/.cpp/.S），换成待检文件
        argv = [t for t in argv if not t.lower().endswith((".c", ".cpp", ".cc", ".s", ".S"))]
        compiler = resolve_compiler(argv[0])
        if args.host:
            argv = [t for t in argv if t != "-DPLATFORM_ESP32=1"] + ["-DPLATFORM_HOST=1"]
        # cwd 用的是 compile_commands 里记录的构建目录，所以源文件必须给绝对路径
        full = [compiler] + argv[1:] + WARN_FLAGS + [os.path.abspath(src)]

        print("─── 预检 %s" % src)
        p = subprocess.run(full, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           cwd=e.get("directory") or None)
        if p.returncode == 0 and not p.stderr.strip():
            print("    ✓ 语法通过（fsyntax-only）")
        else:
            rc = 1
            print("    ✗ 失败 rc=%d" % p.returncode)
            sys.stdout.write(p.stderr or p.stdout or "")
    return rc


if __name__ == "__main__":
    sys.exit(main())
