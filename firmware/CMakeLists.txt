# firmware/CMakeLists.txt — ESP-IDF 工程顶层（标准结构）
#
# 用法（硬件到货前后均可构建）：
#   cd firmware
#   idf.py set-target esp32s3      # 首次
#   idf.py build
#
# 结构：本目录是 ESP-IDF 工程根；源码作为名为 "main" 的组件注册
#（见 main/CMakeLists.txt）。纯逻辑模块（hid_report / mjpeg / http_api /
# ble_hid / routes）与平台接缝（platform/host | platform/esp32）分离。
#
# 到货后只需：把 main/CMakeLists.txt 里的 PLATFORM_DIR 由 host 切成 esp32，
# 并补 platform/esp32/*.c 的实现；纯逻辑一行不改。

cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(qshou)
