# platform/esp32 — 真实硬件接缝实现（**已就绪，待真机核对引脚**）

本目录是 `platform.h` 接缝的 ESP32 实现。**全部函数体已写完并可编译**
（`tools/precheck-seams.sh` 13/13 通过）。到货后主要是**核对引脚 + 真机调参**，
而非从零实现。

## 文件与状态

| 文件 | 内容 | 当前状态 |
|---|---|---|
| `platform_ble.c` | BLE HID 发射（哑中继） | ✅ **已实现**：`esp_hidd_dev_init` + NimBLE transport，用真实 `esp_hidd.h` 通过预检 |
| `platform_camera.c` | OV2640 取帧 | ✅ **已实现**：esp32-camera 路线，DVP 引脚宏可覆写；待**拉组件 + 核对引脚** |
| `platform_health.c` | 遥测（uptime/heap/rssi/reset） | ✅ 已实现（ESP-IDF 原生 API）|
| `platform_log.c` | 日志 → `ESP_LOGx` | ✅ 已实现 |
| `platform_net.c` | 系统启动编排 | ✅ 已实现：NVS/event loop/WiFi STA/HTTP server（6 端点）/推流任务 |

## 接缝函数对照

| 接缝函数 | esp32 实现要点 | 状态 |
|---|---|---|
| `platform_ble_init` | `esp_hidd_dev_init(&cfg, ESP_HID_TRANSPORT_BLE, ...)`；HID 描述符来自 `ble_hid/hid_descriptor.c` | ✅ |
| `platform_ble_emit` | 未连接静默丢弃返 0；已连接 `esp_hidd_dev_input_set(...)` | ✅ |
| `platform_camera_capture` | `esp_camera_fb_get` → 拷入 PSRAM 缓冲 → 归还 | ✅（需组件）|
| `platform_uptime_ms` | `esp_timer_get_time()/1000` | ✅ |
| `platform_free_heap` | `esp_get_free_heap_size()`；🔴 **口径=内部 DRAM+PSRAM 总和**，与 `sysinfo.mem.heap_free`（仅内部）不同口径，恒等式 `health.free_heap ≈ sysinfo.mem.heap_free + psram_free` | ✅ |
| `platform_wifi_rssi` | `esp_wifi_sta_get_ap_info` | ✅ |
| `platform_reset_reason` | `esp_reset_reason()` → 字符串 | ✅ |
| `platform_log` | `ESP_LOGx` | ✅ |
| `platform_system_init` | `nvs_flash_init` + `esp_netif_init` + event loop | ✅ |
| `platform_wifi_init` | STA 连接（凭据从 NVS 读，**不硬编码**）| ✅ |
| `platform_http_start` | 注册 6 个 `/api/v1/` 端点 → `routes_handle` | ✅ |
| `platform_stream_start` | MJPEG 推流任务（调 `firmware_build_frame`）| ✅ |

## 到货后的补齐顺序（建议）

1. **核对板型与引脚**（**最高优先，先做**）：比对实际摄像头模组引脚与
   `platform_camera.c` 顶部宏（现为 ESP32-S3-EYE 参考分配），不符则用
   `-DQSHOU_CAM_PIN_XCLK=...` 覆写**或改宏**（不碰逻辑代码）。
2. **拉 esp32-camera 组件**：`idf.py add-dependency "espressif/esp32-camera^2.0.0"`
   （需联网；沙箱此前无法访问组件注册表）。
3. **换 esp32 接缝全量构建**：`idf.py -DPLATFORM_KIND=esp32 build`，
   此时 `platform_camera.c` 才真正参与编译（此前仅桩头预检）。
4. **烧写 + 冒烟**：先只验 camera 初始化（看串口是否报 DVP 引脚/PSRAM 错），
   再接 BLE HID。
5. **验证 WiFi**：NVS 灌入凭据后实测连接。
6. **键盘 HID**：新增按键报告特征（Report ID 与 digitizer 区分）。

## 关键边界（v1.9.1 决策，不可违反）

**SCR→HID 坐标换算在 Win10 控制端完成**，固件只接收已换算的 5 字节 report 并透传发射，
是"哑中继"。这满足 `R20` 密文约束（broker/固件不解析指令语义），也避免固件重复实现标定逻辑。

## 构建切换

```bash
idf.py -DPLATFORM_KIND=esp32 build     # 用 platform/esp32（真机）
idf.py -DPLATFORM_KIND=host  build     # 用 platform/host（零硬件验证）
```

切换只影响 `main/CMakeLists.txt` 里的 `PLATFORM_SRC`，纯逻辑模块一行不改。
