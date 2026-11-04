#!/usr/bin/env python3
"""idf_build_flash.py — 用 Python 驱动 idf.py：接缝校验 → 编译 → 烧写。

为什么不用 firmware/flash_build_only.ps1
---------------------------------------
那个 PowerShell 版本踩过三个环境坑（脚本里已各自加注释修正，但 PS 的环境块
操作本身仍不可靠）：

1. **UTF-8 无 BOM 的 .ps1 会被 Windows PowerShell 5.1 按 GBK 解码** → 中文注释
   让语句边界错乱。实测症状：`Test-Path` 抛 ParameterArgumentValidationError
   ("参数是空值")、`$PSScriptRoot` 取空、接缝脏缓存检测**静默失效**（最坏结果
   是烧上 host 桩固件）。修复 = 文件加 BOM，但这类坑防不胜防。
2. **进程环境块里同时存在 `Path` / `PATH` / `path` 三个同名键**
   （PowerShell 的 Env: 提供程序因此报「已添加了具有相同键的项」），
   而 `$env:PATH = x` 只改其中一个 → 子进程（ninja）可能拿到**另一个**旧值。
   症状极具误导性：工具明明在 PATH 里，ninja 却报
       CreateProcess failed: The system cannot find the file specified.
   本项目实际踩到的是 ccache：CMake 的 find_program 能找到它（把编译器命令
   写成裸名 `ccache <gcc> ...`），ninja 执行时却找不到。
3. **MSYSTEM 被 Git Bash 反复注入**，`unset`/`env -u` 都拦不住 → idf.py 直接
   abort（见 tools/idf.py: `if 'MSYSTEM' in os.environ`）。

Python 侧 `os.environ` 没有大小写别名、是唯一真源，配合 idf_run.py 同款环境
拼装最稳。故本脚本作为**推荐路径**；.ps1 保留作备用。

用法
----
    python idf_build_flash.py                  # 编译 + 烧写 COM7
    python idf_build_flash.py --build-only     # 只编译
    python idf_build_flash.py --port COM3
    python idf_build_flash.py --no-ccache      # 关 ccache（会触发 CMake 重配 → 全量）
    python idf_build_flash.py --flash-only --port COM4
                                               # 跳过编译，直接烧已有产物
                                               # （给第二块板用：固件相同，无需重编）

⚠️ 前台运行
----------
本脚本必须**前台同步执行**，不要丢到后台（`nohup ... &` / 后台任务）。
实测后台托管时进程树会被冻结：表现为构建静默停在某个 `[n/N]` 不再前进、
`.ninja_log` 行数不增长、**日志无任何报错**。曾分别冻结在 `[751/1278]` 与
`[523/530]`。诊断确认为 ninja→ccache→gcc 链上一条平凡查询命令零 CPU 阻塞
（Δkernel=Δuser=0.0000s / 10s）。前台运行（沙箱开或关）均可正常跑完。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

_USERPROFILE = os.environ.get("USERPROFILE") or os.path.expanduser("~")
IDF = os.environ.get("IDF_PATH", os.path.join(_USERPROFILE, "esp", "esp-idf"))
TOOLS = os.environ.get("IDF_TOOLS_PATH", os.path.join(_USERPROFILE, ".espressif"))
PYENV = os.environ.get("IDF_PYTHON_ENV_PATH", os.path.join(TOOLS, "python_env", "idf6.1_py3.13_env"))
CCACHE_DIR = os.path.join(
    TOOLS, r"tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64")

PROJECT_DIR = os.getenv(
    "IDF_PROJECT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__))))

TOOL_DIRS = [
    r"tools\xtensa-esp-elf-gdb\17.1_20260402\xtensa-esp-elf-gdb\bin",
    r"tools\riscv32-esp-elf-gdb\17.1_20260402\riscv32-esp-elf-gdb\bin",
    r"tools\xtensa-esp-elf\esp-15.2.0_20251204\xtensa-esp-elf\bin",
    r"tools\riscv32-esp-elf\esp-15.2.0_20251204\riscv32-esp-elf\bin",
    r"tools\esp32ulp-elf\2.38_20240113\esp32ulp-elf\bin",
    r"tools\cmake\4.0.3\bin",
    r"tools\openocd-esp32\v0.12.0-esp32-20260703\openocd-esp32\bin",
    r"tools\ninja\1.12.1",
    r"tools\idf-exe\1.0.3",
    r"tools\ccache\4.12.1\ccache-4.12.1-windows-x86_64",
    r"tools\dfu-util\0.11\dfu-util-0.11-win64",
    r"tools\esp-clangd\esp-21.1.3_20260408\esp-clangd\bin",
    r"python_env\idf6.1_py3.13_env\Scripts",
]

MINGW_VARS = ("MSYSTEM", "MSYS", "MINGW_PREFIX", "MINGW_CHOST",
              "MSYS_NO_PATHCONV", "MINGW_HOME", "MSYS2_PATH_TYPE")


def build_env(no_ccache: bool) -> dict:
    env = dict(os.environ)
    for v in MINGW_VARS:
        env.pop(v, None)
    env["IDF_PATH"] = IDF
    env["IDF_TOOLS_PATH"] = TOOLS
    env["IDF_PYTHON_ENV_PATH"] = PYENV
    env["ESP_IDF_VERSION"] = "6.1"
    env["ESP_ROM_ELF_DIR"] = os.path.join(
        TOOLS, "tools", "esp-rom-elfs", "20241011") + "\\"
    env["OPENOCD_SCRIPTS"] = os.path.join(
        TOOLS, "tools", "openocd-esp32", "v0.12.0-esp32-20260703",
        "openocd-esp32", "share", "openocd", "scripts")
    env["PYTHONUTF8"] = "1"
    # ccache：保持启用才能吃到热缓存（本机缓存 45.9MB / 历史命中率 64%）。
    # 但必须先把它的 bin 目录放进 PATH —— rules.ninja 里写的是裸名 `ccache`。
    # 显式 --no-ccache 会反过来让 idf.py 传 -DCCACHE_ENABLE=0 → CMake 重配
    # → 所有编译命令变化 → ninja 判定全量重建（多花 20+ 分钟）。
    env["IDF_CCACHE_ENABLE"] = "0" if no_ccache else "1"

    paths = [os.path.join(TOOLS, d) for d in TOOL_DIRS]
    paths += [
        os.path.join(IDF, "tools"),
        os.path.join(IDF, "components", "espcoredump"),
        os.path.join(IDF, "components", "partition_table"),
        os.path.join(IDF, "components", "app_update"),
        os.path.join(_USERPROFILE, ".workbuddy-ai", "binaries", "PortableGit", "versions", "1.2.0", "cmd"),
        r"C:\Windows\System32",
        r"C:\Windows",
    ]
    # 去重但保序（PATH 里重复项会让某些工具解析到意外版本）
    seen, ordered = set(), []
    for p in paths + env.get("PATH", "").split(os.pathsep):
        if not p:
            continue
        k = p.rstrip("\\").lower()
        if k in seen:
            continue
        seen.add(k)
        ordered.append(p)
    env["PATH"] = os.pathsep.join(ordered)
    return env


def run(argv, env, label):
    print("\n>>> %s" % label, flush=True)
    print("    %s" % " ".join(argv), flush=True)
    return subprocess.call(argv, env=env, cwd=PROJECT_DIR)


def seam_ok() -> bool:
    cache = os.path.join(PROJECT_DIR, "build", "CMakeCache.txt")
    if not os.path.exists(cache):
        print("[gate] CMakeCache 不存在 → 无历史产物，可安全构建")
        return True
    with open(cache, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("PLATFORM_KIND:"):
                val = line.strip().split("=", 1)[-1]
                print("[gate] %s" % line.strip())
                if val != "esp32":
                    print("[gate] ⛔ PLATFORM_KIND=%r ≠ esp32 → 会编出 host 桩固件，中止" % val)
                    return False
                return True
    print("[gate] ⛔ CMakeCache 里找不到 PLATFORM_KIND → 无法证明接缝正确，中止")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="idf.py 构建 + 烧写（Python 驱动）")
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--flash-only", action="store_true",
                    help="跳过编译，直接烧写 build/ 下已有产物（给第二块板用）")
    ap.add_argument("--no-ccache", action="store_true")
    args = ap.parse_args()

    env = build_env(args.no_ccache)
    py = os.path.join(PYENV, "Scripts", "python.exe")
    idf_py = os.path.join(IDF, "tools", "idf.py")
    if not os.path.exists(py):
        print("[FATAL] 找不到 IDF python venv: %s" % py)
        return 1

    # ccache 可达性自检：rules.ninja 用裸名调用它，不可达就会在编到一半时炸
    if not args.no_ccache:
        cc = os.path.join(CCACHE_DIR, "ccache.exe")
        print("[env] ccache: %s (%s)" % (cc, "存在" if os.path.exists(cc) else "缺失"))

    print("[env] MSYSTEM scrubbed; IDF_PATH=%s" % IDF)
    print("[env] cwd: %s" % PROJECT_DIR)

    if not seam_ok():
        return 2

    if args.flash_only:
        # 第二块板（B 板）用：固件与 A 板完全相同，直接烧已有产物，省掉编译。
        # 仍要过 seam_ok() —— 确保这份产物是 esp32 接缝（不是 host 桩固件）。
        binp = os.path.join(PROJECT_DIR, "build", "qshou.bin")
        if not os.path.exists(binp):
            print("[FATAL] --flash-only 但找不到已有产物：%s" % binp)
            print("        先跑一次不带 --flash-only 的构建。")
            return 11
        print("[build] --flash-only：跳过编译，直接烧写已有产物")
        print("        %s" % binp)
    else:
        rc = run([py, idf_py, "-DPLATFORM_KIND=esp32", "build"], env,
                 "build (PLATFORM_KIND=esp32)")
        print("[build] EXIT=%d" % rc)
        if rc != 0:
            print("⛔ 编译失败，放弃烧写（避免把半成品/旧镜像烧进去）")
            return 10
        if args.build_only:
            return 0

    if not seam_ok():   # 构建后复核：CMake 重配可能改掉接缝
        return 2

    rc = run([py, idf_py, "-p", args.port, "flash"], env, "flash %s" % args.port)
    print("[flash] EXIT=%d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
