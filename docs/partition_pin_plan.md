# PSRAM 分区表与引脚预案（任务 C5）

> 状态：**预案**。板型为 `ESP32-S3-N16R8`（16MB Flash + 8MB Octal PSRAM），但**具体 GPIO 分配以到货后 datasheet / 板载丝印为准**。
> 本文件为到货当天即可套用的 checklist，不依赖实物即可预先规划。

## 1. 板载资源（ESP32-S3-N16R8）

| 资源 | 规格 |
|---|---|
| SoC | ESP32-S3 (dual-core LX7, 240MHz) |
| Flash | 16 MB (QSPI) |
| PSRAM | 8 MB Octal (OPI PSRAM，内置) |
| 无线 | WiFi 2.4G + BLE 5.0（支持 HID over GATT） |

> N16R8 的 8MB PSRAM 是本项目能跑 MJPEG + 帧缓冲的关键；若到货为 N8R8 同理，N4 系列无 PSRAM 需改方案。

## 2. OV2640（DVP 接口）引脚预案

以下为 `ESP32-S3-EYE` 参考分配（最常用 S3 摄像头板），到货后比对实际板型：

| 信号 | 参考 GPIO | 说明 |
|---|---|---|
| XCLK | 15 | 相机主时钟（由 ESP32 输出） |
| PCLK | 13 | 像素时钟（输入） |
| VSYNC | 6 | 帧同步 |
| HREF | 7 | 行同步 |
| D0–D7 | 11,9,8,10,12,18,17,16 | 8 位数据总线 |
| SDA / SCL | 4 / 5 | SCCB（相机寄存器配置） |
| PWDN | -1 (不使用) | 关断脚，多数模块可悬空 |
| RESET | -1 (不使用) | 复位脚，可悬空或拉高 |

> 若采用 OV2640 模块而非 S3-EYE 整板，需确认模块是否自带 XCLK 晶振——通常仍需 ESP32 提供。

## 3. PSRAM / 分区表预案

- **menuconfig 必开**：`CONFIG_ESP32S3_SPIRAM_SUPPORT=y`、`CONFIG_SPIRAM_MODE_OCT=y`（Octal）、
  `CONFIG_SPIRAM_USE_MEMMAP=y` 或 `CONFIG_SPIRAM_USE_CAPS_ALLOC`（按需）。
- **分区表**：ESP32-S3 N16R8 建议基于 `partitions/partitions_16MB.csv` 或 `default_8MB` 变体，
  预留：
  - `factory` app 区 ≥ 2 MB（含 CameraWebServer + BLE HID + broker 客户端）
  - `spiffs` / `fatfs` 数据区（标定 JSON、设备证书）
  - 大块 PSRAM 运行时分配（帧缓冲 1–2 帧 + MJPEG 编码缓冲），**不**进 Flash 分区
- **帧缓冲预算**（VGA 640×480 JPEG 约 20–60KB/帧）：8MB PSRAM 足够双缓冲；若上 UXGA 需复核。

## 4. BLE + WiFi 共存注意

- BLE HID（HOGP）与 WiFi 图传共享 2.4G 射频，需按 `reliability` 章节做共存调优（优先级/时隙）。
- `Autfy/ESP32-BLE-HID` 默认栈可能为 Bluedroid；能否切 NimBLE 需实测（R 章节已记录）。

## 5. 到货后核对清单

- [ ] 确认板型丝印的 Flash/PSRAM 容量（N16R8 vs N8R8）
- [ ] 比对实际摄像头模组引脚与第 2 节预案，更新 GPIO 定义
- [ ] 烧录空固件（仅 `esp_camera` 初始化）验证引脚无误后再集成 BLE HID
- [ ] 用 `idf.py partition_table` / Arduino 分区工具验证分区表烧录成功
- [ ] 跑通 `step_3` CameraWebServer 浏览器看流（M0 里程碑）

## 6. 当前已完成（前置）

- `workspace_layout` 已规定 `firmware/{main,ble_hid,camera}` 分区，固件侧直接引用 `shared/coords.*` 常量。
- 坐标换算与 HID 打包已在 `shared/coords.py` 落地，与引脚/分区无关，可独立开发。
