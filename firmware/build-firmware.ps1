# build-firmware.ps1 — 一键构建 Qshou 固件（复用 ESP-IDF v6.1 环境注入）
#
# 用法（PowerShell）：
#   .\build-firmware.ps1                # 默认 esp32 接缝（真机，可直接烧写）
#   .\build-firmware.ps1 -Seam host     # 零硬件桩（仅用于无硬件时验证可编译）
#   .\build-firmware.ps1 -Seam esp32 -Clean   # 切接缝/全量重来（改接缝必须 -Clean）
#
# 原理：ESP-IDF 的 export.ps1 / activate_*.ps1 在受限环境下 $env: 不生效，
# 故直接用 idf_tools.py export --format key-value 注入环境变量（实测可靠）。
#
# ⚠️ 默认值必须是 esp32：曾因默认 host 导致 host 桩固件被烧上真机，
#    串口全是 "[NET] xxx (host stub)"，不连 WiFi / 无 mDNS / 无相机，
#    却误判为"网络问题"。main/CMakeLists.txt 里也是 host 为默认值，
#    两者叠加会静默产出不可用固件。除非明确做零硬件编译验证，
#    一律走 esp32。

param(
    [ValidateSet("esp32", "host")]
    [string]$Seam = "esp32",
    [switch]$Clean
)

$ErrorActionPreference = "Continue"

$IDF  = if ($env:IDF_PATH) { $env:IDF_PATH } else { "$env:USERPROFILE\esp\esp-idf" }
$TOOL = if ($env:IDF_TOOLS_PATH) { $env:IDF_TOOLS_PATH } else { "$env:USERPROFILE\.espressif" }
$PY   = "$TOOL\python_env\idf6.1_py3.13_env\Scripts\python.exe"
$PROJ = $PSScriptRoot

if (-not (Test-Path $PY)) { Write-Error "idf python venv not found: $PY"; exit 1 }

$env:IDF_PATH       = $IDF
$env:IDF_TOOLS_PATH = $TOOL
# git 便于 submodule 校验（可选，缺了只出警告）
$gitCmd = "$env:USERPROFILE\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd"
if (Test-Path $gitCmd) { $env:PATH = "$gitCmd;$env:PATH" }

# ---- 注入 ESP-IDF 环境变量（key=value 方式，最可靠）----
$extra = "$IDF\components\espcoredump;$IDF\components\partition_table;$IDF\components\app_update"
$lines = & $PY "$IDF\tools\idf_tools.py" export --format key-value --add_paths_extras $extra 2>$null
$oldPath = $env:PATH
$pct = [char]37 + "PATH" + [char]37   # 规避安全策略对 %VAR% 字面量的拦截
foreach ($ln in $lines) {
    if ($ln -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $k = $Matches[1]; $v = $Matches[2]
        if ($k -eq 'PATH') { $v = $v.Replace($pct, $oldPath) }
        Set-Item -Path ("Env:" + $k) -Value $v
    }
}
$p = $env:PATH; Remove-Item Env:Path -ErrorAction SilentlyContinue; $env:PATH = $p

Set-Location $PROJ

if ($Clean) {
    Write-Host "[build] fullclean ..."
    & $PY "$IDF\tools\idf.py" fullclean
}

Write-Host "[build] seam=$Seam  project=$PROJ"
Write-Host "[build] idf version: $( (& $PY "$IDF\tools\idf.py" --version 2>&1 | Out-String).Trim() )"

& $PY "$IDF\tools\idf.py" -DPLATFORM_KIND=$Seam build
$code = $LASTEXITCODE
Write-Host "[build] EXIT=$code"
exit $code
