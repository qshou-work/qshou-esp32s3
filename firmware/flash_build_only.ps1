# flash_build_only.ps1 — 构建(esp32 接缝) + 烧写，不进 monitor
#
# ⚠️ 本脚本现在是**薄包装**，真正的逻辑在 idf_build_flash.py。
#
# 为什么不在这里自己拼环境（血泪史，2026-10-02）：
#   1. .ps1 若是 UTF-8 无 BOM，Windows PowerShell 5.1 会按 GBK 解码 → 中文注释
#      让语句边界错乱。实测症状：Test-Path 抛「参数是空值」、$PSScriptRoot 取空、
#      接缝脏缓存检测**静默失效**。修复是给文件加 BOM，但这类坑防不胜防。
#   2. 本机进程环境块里同时存在 Path / PATH / path 三个同名键（PowerShell 的
#      Env: 提供程序会报「已添加了具有相同键的项」），而 `$env:PATH = x` 只改
#      其中一个 → 子进程（ninja）拿到**另一个**旧值。症状极具误导性：
#      工具明明在 PATH 里，ninja 却报
#          CreateProcess failed: The system cannot find the file specified.
#      （实际踩到的是 ccache —— CMake 的 find_program 能找到它，ninja 却找不到。）
#   3. 接缝脏缓存那次 fullclean 被沙箱安全策略中途拦下，反而把 ninja 的增量
#      日志删残，把一次 13 目标的增量编译放大成 1104 目标的全量重编。
#   Python 侧 os.environ 没有大小写别名、是唯一真源，故把逻辑搬过去。
#
# 用法（保持与原脚本一致）：
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File flash_build_only.ps1 -Port COM7

param(
    [string]$Port = "COM7",
    [switch]$BuildOnly,
    [switch]$NoCcache
)

$ErrorActionPreference = "Continue"

# $PSScriptRoot 在嵌套 powershell -File 场景下可能为空串，必须有回退
$PROJ = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $PROJ) { $PROJ = (Get-Location).Path }

$PY = "$($env:IDF_TOOLS_PATH ? $env:IDF_TOOLS_PATH : "$env:USERPROFILE\.espressif")\python_env\idf6.1_py3.13_env\Scripts\python.exe"
$DRIVER = Join-Path $PROJ "idf_build_flash.py"
if (-not (Test-Path -Path $PY)) { Write-Error "idf python venv not found: $PY"; exit 1 }
if (-not (Test-Path -Path $DRIVER)) { Write-Error "driver not found: $DRIVER"; exit 1 }

$argv = @($DRIVER, "--port", $Port)
if ($BuildOnly) { $argv += "--build-only" }
if ($NoCcache)  { $argv += "--no-ccache" }

& $PY @argv
exit $LASTEXITCODE
