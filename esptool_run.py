#!/usr/bin/env python3
"""
esptool_run.py -- run esptool with a clean (MSYSTEM-scrubbed) environment.

The sandbox's Git Bash re-injects MSYSTEM=MINGW64 into child processes, which
makes some tooling abort. This wrapper pops those vars inside Python and then
invokes `python -m esptool ...` with the fixed environment.

Usage:  python esptool_run.py [esptool args...]
e.g.    python esptool_run.py -p COM7 --chip esp32s3 --after hard-reset run
"""
import os
import subprocess
import sys

for v in ("MSYSTEM", "MSYS", "MINGW_PREFIX", "MINGW_CHOST", "MSYS_NO_PATHCONV"):
    os.environ.pop(v, None)

_USERPROFILE = os.environ.get("USERPROFILE") or os.path.expanduser("~")
PYENV = os.environ.get("IDF_PYTHON_ENV", os.path.join(_USERPROFILE, ".espressif", "python_env", "idf6.1_py3.13_env"))

argv = [os.path.join(PYENV, "Scripts", "python.exe"), "-m", "esptool"] + sys.argv[1:]

sys.stderr.write("[esptool_run] cmd: %s\n" % " ".join(sys.argv[1:]))
sys.stderr.flush()

rc = subprocess.call(argv, env=os.environ)
sys.exit(rc)
