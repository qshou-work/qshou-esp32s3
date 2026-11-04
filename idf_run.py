#!/usr/bin/env python3
"""
idf_run.py -- launch ESP-IDF's idf.py with a clean Windows environment.

Why this exists
---------------
This sandbox's Bash tool (Git Bash / MINGW64) *re-injects* the env vars
`MSYSTEM= MINGW64` (and MSYS / MINGW_PREFIX / MINGW_CHOST) into every child
process -- `unset` and even `env -u` are silently neutralised. idf.py aborts
hard when it sees MSYSTEM set:

    tools/idf.py:1483   if 'MSYSTEM' in os.environ: <abort>

Workaround: drop those vars from os.environ *inside* Python (which works,
because the injection happens at the shell layer, not the Python layer) and
then exec idf.py as a subprocess with the cleaned environment.

Usage
-----
    python idf_run.py [idf.py args...]
e.g.
    python idf_run.py -DPLATFORM_KIND=esp32 build
    python idf_run.py -p COM7 flash
    python idf_run.py monitor
"""
import os
import subprocess
import sys

_USERPROFILE = os.environ.get("USERPROFILE") or os.path.expanduser("~")
IDF = os.environ.get("IDF_PATH", os.path.join(_USERPROFILE, "esp", "esp-idf"))
TOOLS = os.environ.get("IDF_TOOLS_PATH", os.path.join(_USERPROFILE, ".espressif"))
PYENV = os.environ.get("IDF_PYTHON_ENV_PATH", os.path.join(TOOLS, "python_env", "idf6.1_py3.13_env"))

# ---- 1. Scrub the MINGW markers that Git Bash keeps re-injecting ----------
for v in ("MSYSTEM", "MSYS", "MINGW_PREFIX", "MINGW_CHOST", "MSYS_NO_PATHCONV",
          "MINGW_HOME", "MSYS2_PATH_TYPE"):
    os.environ.pop(v, None)

# ---- 2. Build a complete ESP-IDF v6.1 environment ------------------------
os.environ["IDF_PATH"] = IDF
os.environ["IDF_TOOLS_PATH"] = TOOLS
os.environ["IDF_PYTHON_ENV_PATH"] = PYENV
os.environ["ESP_IDF_VERSION"] = "6.1"
os.environ["ESP_ROM_ELF_DIR"] = os.path.join(TOOLS, "tools", "esp-rom-elfs", "20241011") + "\\"
os.environ["OPENOCD_SCRIPTS"] = os.path.join(
    TOOLS, "tools", "openocd-esp32", "v0.12.0-esp32-20260703",
    "openocd-esp32", "share", "openocd", "scripts")
# ccache：必须与 firmware/idf_build_flash.py 保持同值（默认 "1"）。
# 历史坑（2026-10-25）：本脚本曾写死 "0"，而 idf_build_flash.py 默认 "1"，
# 两个脚本交替用 → idf.py 传给 CMake 的 CCACHE_ENABLE 翻转 → CMake 静默重配
# → rules.ninja 里 135 条编译命令前缀在 `ccache ...` 与裸命令之间来回变
# → ninja 命令哈希全部失配 → 近全量重建（实测 957/1308 个目标，多花 20+ 分钟）。
os.environ["IDF_CCACHE_ENABLE"] = "1"

tool_dirs = [
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
paths = [os.path.join(TOOLS, d) for d in tool_dirs]
paths += [
    os.path.join(IDF, "tools"),
    os.path.join(IDF, "components", "espcoredump"),
    os.path.join(IDF, "components", "partition_table"),
    os.path.join(IDF, "components", "app_update"),
    os.path.join(_USERPROFILE, ".workbuddy-ai", "binaries", "PortableGit", "versions", "1.2.0", "cmd"),
    r"C:\Windows\System32",
    r"C:\Windows",
]
paths += os.environ.get("PATH", "").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join(p for p in paths if p)

# ---- 3. Run idf.py --------------------------------------------------------
argv = [os.path.join(PYENV, "Scripts", "python.exe"),
        os.path.join(IDF, "tools", "idf.py")] + sys.argv[1:]

# idf.py must run from the project dir (the one containing CMakeLists.txt).
# 默认取本脚本所在目录（本文件位于 firmware/ 下），避免硬编码绝对路径。
PROJECT_DIR = os.getenv(
    "IDF_PROJECT_DIR",
    os.path.dirname(os.path.abspath(__file__)))

sys.stderr.write("[idf_run] MSYSTEM scrubbed; IDF_PATH=%s\n" % IDF)
sys.stderr.write("[idf_run] cwd: %s\n" % PROJECT_DIR)
sys.stderr.write("[idf_run] cmd: %s\n" % " ".join(sys.argv[1:]))
sys.stderr.flush()

rc = subprocess.call(argv, env=os.environ, cwd=PROJECT_DIR)
sys.exit(rc)
