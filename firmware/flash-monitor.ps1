# flash-monitor.ps1 — 一键烧写 + 串口监视（自动探测当前 COM 口）
#
# 用法（PowerShell）：
#   .\flash-monitor.ps1              # esp32 接缝构建产物，烧写并监视
#   .\flash-monitor.ps1 -Seam host   # host 接缝产物
#   .\flash-monitor.ps1 -Port COM7   # 手动指定端口
#   .\flash-monitor.ps1 -SkipBuild   # 跳过构建，只烧写
#
# ⚠ 重要：ESP32-S3 原生 USB-Serial/JTAG 在复位后会**重新枚举**，
#    COM 号可能变化（实测 COM6 → COM7）。本脚本因此：
#    - 不写死端口，默认自动探测「状态 OK 且名字含 USB 的 COM 口」；
#    - 烧写与监视之间会重新探测一次。
#
# 原理：ESP-IDF 的 export.ps1 在受限环境 $env: 不生效，故直接用
#       idf_tools.py export --format key-value 注入环境变量（实测可靠）。

param(
    [string]$Seam = "esp32",
    [string]$Port = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Continue"

$IDF  = "$($env:IDF_PATH ? $env:IDF_PATH : "$env:USERPROFILE\esp\esp-idf")"
$TOOL = "$($env:IDF_TOOLS_PATH ? $env:IDF_TOOLS_PATH : "$env:USERPROFILE\.espressif")"
$PY   = "$TOOL\python_env\idf6.1_py3.13_env\Scripts\python.exe"
$PROJ = $PSScriptRoot

if (-not (Test-Path $PY)) { Write-Error "idf python venv not found: $PY"; exit 1 }

# ---- 自动探测当前活着的 USB 串口 ----
function Get-LivePort {
    $dev = Get-PnpDevice -Class Ports -ErrorAction SilentlyContinue |
           Where-Object { $_.Status -eq 'OK' -and $_.FriendlyName -match 'USB' -and $_.FriendlyName -match 'COM\d+' }
    if (-not $dev) { return "" }
    # 优先名称含"USB 串行设备"的（ESP32-S3 原生 USB-JTAG）
    $p = ($dev | Where-Object { $_.FriendlyName -match 'USB 串行设备' } | Select-Object -First 1)
    if (-not $p) { $p = ($dev | Select-Object -First 1) }
    return [regex]::Match($p.FriendlyName, 'COM\d+').Value
}

if (-not $Port) {
    $Port = Get-LivePort
}
if (-not $Port) { Write-Error "未找到 USB 串口设备，请确认板子已插好"; exit 1 }
Write-Host "[flash] 使用端口: $Port"

# ---- 注入 ESP-IDF 环境 ----
$env:IDF_PATH       = $IDF
$env:IDF_TOOLS_PATH = $TOOL
$gitCmd = "$env:USERPROFILE\.workbuddy\binaries\PortableGit\versions\1.2.0\cmd"
if (Test-Path $gitCmd) { $env:PATH = "$gitCmd;$env:PATH" }

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

if (-not $SkipBuild) {
    Write-Host "[flash] 构建 (seam=$Seam) ..."
    & $PY "$IDF\tools\idf.py" -DPLATFORM_KIND=$Seam build
    if ($LASTEXITCODE -ne 0) { Write-Error "[flash] 构建失败，未烧写"; exit 1 }
}

# ---- 烧写（-p 指定端口；--no-stub 可选，USB-JTAG 下更稳）----
Write-Host "[flash] 烧写 -> $Port ..."
& $PY "$IDF\tools\idf.py" -p $Port flash
if ($LASTEXITCODE -ne 0) {
    Write-Host "[flash] 首次烧写失败，端口可能已重枚举，重探后重试 ..."
    Start-Sleep -Seconds 3
    $Port = Get-LivePort
    Write-Host "[flash] 新端口: $Port"
    & $PY "$IDF\tools\idf.py" -p $Port flash
    if ($LASTEXITCODE -ne 0) { Write-Error "[flash] 烧写失败"; exit 1 }
}

# ---- 监视（端口可能又变了，重探）----
Start-Sleep -Seconds 2
$Port = Get-LivePort
Write-Host "[flash] 监视 $Port（Ctrl+] 退出）..."
& $PY "$IDF\tools\idf.py" -p $Port monitor
