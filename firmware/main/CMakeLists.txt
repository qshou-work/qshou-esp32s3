# main/CMakeLists.txt — 把 firmware 全部源码注册为 ESP-IDF 的 "main" 组件
#
# 接缝选择：PLATFORM_KIND
#   esp32 —— 真实实现（真机，唯一可烧写的选择）
#   host  —— 本机桩（无硬件也能编译，仅用于零硬件构建验证）
#
# ⚠️ 默认 esp32。历史教训：曾默认 host，导致 host 桩固件被烧上真机，
#    串口全是 "[NET] xxx (host stub)"，不连 WiFi / 无 mDNS / 无相机，
#    却被误判成"网络问题"，排查绕了很大一圈。
#    真要验证零硬件可编译，请显式 -DPLATFORM_KIND=host。
set(PLATFORM_KIND "esp32" CACHE STRING "Platform seam impl: host | esp32")

# 组件根 = firmware/（本文件所在目录的父目录）
set(FW_ROOT "${CMAKE_CURRENT_LIST_DIR}/..")

# ---- 纯逻辑源（不依赖硬件，host/esp32 共用） ----
set(FW_LOGIC_SRC
    "${FW_ROOT}/ble_hid/hid_report.c"
    "${FW_ROOT}/ble_hid/hid_descriptor.c"
    "${FW_ROOT}/ble_hid/ble_hid.c"
    "${FW_ROOT}/usb_hid/usb_hid_frame.c"
    "${FW_ROOT}/hid_router/hid_router.c"
    "${FW_ROOT}/camera/camera.c"
    "${FW_ROOT}/camera/mjpeg.c"
    "${FW_ROOT}/main/http_api.c"
    "${FW_ROOT}/main/routes.c"
)

# ---- 平台接缝实现（二选一） ----
if(PLATFORM_KIND STREQUAL "esp32")
    set(PLATFORM_SRC
        "${FW_ROOT}/platform/esp32/platform_ble.c"
        "${FW_ROOT}/platform/esp32/platform_camera.c"
        "${FW_ROOT}/platform/esp32/platform_health.c"
        "${FW_ROOT}/platform/esp32/platform_led.c"
        "${FW_ROOT}/platform/esp32/platform_log.c"
        "${FW_ROOT}/platform/esp32/platform_net.c"
        "${FW_ROOT}/platform/esp32/platform_mdns.c"
        "${FW_ROOT}/platform/esp32/platform_gpio.c"
        "${FW_ROOT}/platform/esp32/platform_usb.c"
    )
    set(PLATFORM_DEF PLATFORM_ESP32=1)
else()
    set(PLATFORM_SRC
        "${FW_ROOT}/platform/host/platform_ble.c"
        "${FW_ROOT}/platform/host/platform_camera.c"
        "${FW_ROOT}/platform/host/platform_health.c"
        "${FW_ROOT}/platform/host/platform_led.c"
        "${FW_ROOT}/platform/host/platform_log.c"
        "${FW_ROOT}/platform/host/platform_net.c"
        "${FW_ROOT}/platform/host/platform_gpio.c"
        "${FW_ROOT}/platform/host/platform_usb.c"
    )
    set(PLATFORM_DEF PLATFORM_HOST=1)
endif()

# ---- 入口（app_main 在 esp32 分支；host 分支为空 main） ----
set(ENTRY_SRC "${FW_ROOT}/main/main.c")

# ---- WiFi 备用凭据头（不入库；配置期按需生成） ----
# 真实 SSID/密码放在 platform/esp32/qshou_wifi_creds.h（.gitignore 已排除），
# 由 platform_net.c 无条件 #include。该文件缺失时从 .example 生成一份**空表**，
# 保证「全新克隆开箱可编译」，且仓库里永远没有明文密码。
#
# ⚠️ 为什么不用 #if __has_include(...)（2026-10-27 实测踩坑）：
#    可选包含的头文件不会出现在 gcc -MD 的依赖表里，ccache 直连模式于是命中
#    「空表版本」的旧 .obj —— 头文件补上后固件里仍然没有凭据，**编译零报错**，
#    只能靠翻 bin 里的字符串才发现。改成无条件包含 + 配置期生成后，
#    该头是正常依赖，改它就会正确触发重编。
set(WIFI_CREDS_H "${FW_ROOT}/platform/esp32/qshou_wifi_creds.h")
if(NOT EXISTS "${WIFI_CREDS_H}")
    configure_file("${WIFI_CREDS_H}.example" "${WIFI_CREDS_H}" COPYONLY)
    message(STATUS "qshou: 未找到 qshou_wifi_creds.h，已从 .example 生成空表（备用 WiFi 凭据为空）")
endif()

# ---- 组件依赖：仅依赖 ESP-IDF 自带组件，无需第三方库即可编译 ----
# esp_hid  —— HID over GATT 抽象层。在 CONFIG_BT_NIMBLE_ENABLED 时编译
#             nimble_hidd.c，即 **NimBLE 下官方支持 HID Device**，因此
#             不必手写 HID GATT 服务，也不必依赖 Arduino 生态的
#             Autfy/ESP32-BLE-HID（规避 R18 许可证不确定性）。
# esp_camera 为 esp32-camera（第三方托管组件），仅在 esp32 接缝需要真实
#             取帧时引入；host 分支不依赖它，保证零硬件可构建。
set(FW_REQUIRES
    esp_http_server
    nvs_flash
    esp_wifi
    esp_event
    esp_netif
    esp_timer
    esp_hid
    bt
    driver
    freertos
    log
    esp_coex          # 2.4G 共存仲裁 esp_coex_preference_set（A 方案）
    esp_driver_rmt    # WS2812 状态灯（platform_led.c 手写 RMT 字节编码器）
    esp_driver_gpio   # IO2 用户灯引脚（LEDC 之前的 GPIO 配置）
    esp_driver_ledc   # IO2 用户灯 PWM 调光（duty 60/255）
    mdns              # mDNS 广播：控制端用 qshou-0a0c.local 访问（IP 漂移免扫网段）
)

idf_component_register(
    SRCS ${FW_LOGIC_SRC} ${PLATFORM_SRC} ${ENTRY_SRC}
    INCLUDE_DIRS "${FW_ROOT}"
    REQUIRES ${FW_REQUIRES}
    EMBED_TXTFILES "ui.html"
)

# 编译宏：平台接缝 + 头文件根
target_compile_definitions(${COMPONENT_LIB} PRIVATE ${PLATFORM_DEF})
