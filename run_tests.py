#!/usr/bin/env python3
"""
run_tests.py — 一条命令跑全部测试（不再逐个文件 python tests/test_xxx.py）。

覆盖两组：
  A) Python 侧 unittest  —— tests/test_*.py（broker / controller / coords / calib /
     transport / assertions，需 numpy）
  B) 固件契约测试        —— firmware/tests/test_firmware_contract.py（自己在末尾
     sys.exit()，故用子进程跑）

历史坑（2026-10-01）：本机默认的 managed Python 3.13 里没装 numpy，跑起来报
ModuleNotFoundError，看上去像代码坏了其实是环境缺依赖。本脚本会先探明 numpy
可用性并给出明确的修复命令，避免再往「代码是不是改坏了」的方向误判。

用法：
    python run_tests.py            # 跑全部
    python run_tests.py -v         # 透传 unittest 详细度
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(ROOT, "tests")
FW_CONTRACT = os.path.join(ROOT, "firmware", "tests", "test_firmware_contract.py")


def check_numpy() -> None:
    try:
        import numpy  # noqa: F401
    except ModuleNotFoundError:
        print("[FATAL] 缺少 numpy —— Python 侧测试跑不了（不是代码问题，是环境缺依赖）")
        print("        修复：%s -m pip install numpy" % sys.executable)
        print("        若当前解释器不是你装过 numpy 的那个，请显式指定它来跑本脚本。")
        sys.exit(2)


def run_python_suite(verbosity: int) -> unittest.TestResult:
    for sub in ("shared", "tools", "broker", "controller", TESTS_DIR):
        if sub not in sys.path:
            sys.path.insert(0, sub)
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=TESTS_DIR, pattern="test_*.py",
                            top_level_dir=TESTS_DIR)
    if suite.countTestCases() == 0:
        print("[FATAL] tests/ 下没发现任何用例，检查 pattern 与目录")
        sys.exit(2)
    runner = unittest.TextTestRunner(verbosity=verbosity)
    return runner.run(suite)


def run_firmware_contract(verbosity: int) -> int:
    print("\n" + "=" * 68)
    print("firmware contract（HID 描述符 / 线帧 / 位移预算 契约）")
    print("=" * 68)
    proc = subprocess.run([sys.executable, FW_CONTRACT], cwd=ROOT)
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="Qshou 全量测试入口")
    ap.add_argument("-v", "--verbose", action="store_true", help="unittest 详细输出")
    args = ap.parse_args()
    verbosity = 2 if args.verbose else 1

    check_numpy()
    print("python: %s" % sys.executable)
    print("=" * 68)
    print("Python side unittest")
    print("=" * 68)
    result = run_python_suite(verbosity)

    rc = run_firmware_contract(verbosity)

    failed = len(result.failures) + len(result.errors)
    print("\n" + "=" * 68)
    print("summary: python %d/%d passed, failures=%d errors=%d | firmware contract rc=%d"
          % (result.testsRun - failed, result.testsRun,
             len(result.failures), len(result.errors), rc))
    print("=" * 68)
    return 1 if (failed or rc) else 0


if __name__ == "__main__":
    sys.exit(main())
