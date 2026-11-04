# firmware/tests/Makefile — host 纯逻辑单元测试（需 gcc + make）
#
# 本机构建与回归（pre_arrival 阶段，无需 ESP32 / ESP-IDF）：
#   make            # 编译
#   make run        # 编译并运行
#   make clean
#
# ⚠️ 本机实际入口：开发机没有原生 gcc，也没有 make，本文件**当前跑不了**。
#    请用同目录 run_c_tests.sh（基于 pip 包 ziglang 的 zig cc，已实测通过，
#    102 passed / 0 failed）。本 Makefile 保留作文档：装好 gcc 后即可直接
#    回归固件纯逻辑。算法契约的“立即验证”另由 test_firmware_contract.py 承担。
#
#    历史教训（2026-10-17）：这两套构建入口从未在本机跑通过，导致
#    ① SRC 漏了 platform_led/gpio/usb 三个文件（链接期才炸，见契约 §24）
#    ② 测试按“改造前不经路由器”的拓扑写（USB 接入后 emit 已走 hid_router）
#    首次真正运行即暴露两个固件真 bug（select_backend 空指针写、
#    GESTURE_TAP 重复 down 帧）—— 长期不跑的测试等于没有测试。

FIRMWARE := ..

CC      ?= gcc
CFLAGS  := -std=c11 -DPLATFORM_HOST -Wall -Wextra -I$(FIRMWARE) -g

SRC := \
  $(FIRMWARE)/ble_hid/hid_report.c \
  $(FIRMWARE)/ble_hid/ble_hid.c \
  $(FIRMWARE)/usb_hid/usb_hid_frame.c \
  $(FIRMWARE)/hid_router/hid_router.c \
  $(FIRMWARE)/camera/camera.c \
  $(FIRMWARE)/camera/mjpeg.c \
  $(FIRMWARE)/main/http_api.c \
  $(FIRMWARE)/main/routes.c \
  $(FIRMWARE)/platform/host/platform_ble.c \
  $(FIRMWARE)/platform/host/platform_camera.c \
  $(FIRMWARE)/platform/host/platform_gpio.c \
  $(FIRMWARE)/platform/host/platform_health.c \
  $(FIRMWARE)/platform/host/platform_led.c \
  $(FIRMWARE)/platform/host/platform_log.c \
  $(FIRMWARE)/platform/host/platform_net.c \
  $(FIRMWARE)/platform/host/platform_usb.c \
  tests/test_firmware_c.c

# 注意：不编译 main/main.c —— 它定义了 main()，会与测试入口冲突。
# 固件入口逻辑（app_main_entry）另由 tests/test_main_entry.py 之类的
# 契约测试覆盖，或在 host 上单独链接验证。

TEST_BIN := test_firmware_c

.PHONY: all run clean
all: $(TEST_BIN)

$(TEST_BIN): $(SRC)
	$(CC) $(CFLAGS) $(SRC) -o $@

run: $(TEST_BIN)
	./$(TEST_BIN)

clean:
	rm -f $(TEST_BIN)
