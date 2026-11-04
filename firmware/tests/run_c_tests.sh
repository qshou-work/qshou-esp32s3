#!/bin/sh
# firmware/tests/run_c_tests.sh — host 纯逻辑单元测试：编译 + 运行
#
# 背景：本机没有原生 gcc（只有 xtensa 交叉编译器，跑不了 PC 程序），
# 因此 Makefile（需要 make 和 gcc）在开发机上一直不可用。
# 本脚本用 ziglang（pip 包，自带 C 编译器，见 §24 契约测试说明）完成同样的事。
#
# 依赖（一次性）：
#   python -m pip install ziglang
#
# 用法：
#   ./run_c_tests.sh          # 编译并运行
#
# 注意两个 zig cc 的坑（踩过）：
#   ① 必须 -target x86_64-windows-gnu —— 不给 -target 时按 MSVC 链接，缺运行库
#   ② M_PI 不在 C11 标准里（ble_hid.c 已有 #ifndef 兜底），
#      gcc 默认 gnu11 会悄悄暴露它，严格的 clang/zig cc 下没有
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
PY=""
for CAND in \
    "$HOME/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe" \
    python python3; do
    if "$CAND" -c "import ziglang" >/dev/null 2>&1; then PY="$CAND"; break; fi
done
if [ -z "$PY" ]; then
    echo "找不到带 ziglang 的 python。先装： python -m pip install ziglang" >&2
    exit 1
fi
echo "using $PY (ziglang $("$PY" -m ziglang version 2>/dev/null))"

SRC="../ble_hid/hid_report.c ../ble_hid/ble_hid.c ../usb_hid/usb_hid_frame.c
     ../hid_router/hid_router.c ../camera/camera.c ../camera/mjpeg.c
     ../main/http_api.c ../main/routes.c
     ../platform/host/platform_ble.c ../platform/host/platform_camera.c
     ../platform/host/platform_gpio.c ../platform/host/platform_health.c
     ../platform/host/platform_led.c ../platform/host/platform_log.c
     ../platform/host/platform_net.c ../platform/host/platform_usb.c
     test_firmware_c.c"

# shellcheck disable=SC2086
"$PY" -m ziglang cc -target x86_64-windows-gnu -std=c11 \
    -DPLATFORM_HOST -Wall -Wextra -I.. -g $SRC -o test_firmware_c.exe
./test_firmware_c.exe
