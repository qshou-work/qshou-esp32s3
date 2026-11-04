# BLE HID Descriptor 复核清单（任务 C3）

> 任务：复核 `Autfy/ESP32-BLE-HID` 的 descriptor 是否与规格一致。
> 状态：**规格侧已固化，待固件库接入时逐项核对**（本清单为离线前置产物，不依赖硬件）。
> 来源：规格 `input.ble_hid.coordinate_mode`（`correct_descriptor_spec` 与 `resolved_answer`）。

## 1. 规格期望的 descriptor（已固化，不可自行编造其它上界）

| 项 | 规格值 |
|---|---|
| Usage Page | `Digitizer (0x0D)` |
| Usage | `Touch Screen (0x04)` |
| X / Y | `X(0x30) / Y(0x31)`，各 16 bit，`Logical Max = Physical Max = 10000` |
| Report ID | `0x01` |
| Report 长度 | `5 字节` |
| Report 布局 | `[state:1B][X_MSB][X_LSB][Y_MSB][Y_LSB]` |
| State 编码 | 按下 `= 0x03`（Tip Switch + In Range 同时置位）；抬起 `= 0x00` |
| Coordinate Mode | absolute（**必须**走 Digitizer 触摸屏描述符，而非「鼠标描述符 + Absolute 标志位」） |

## 2. 复核步骤（固件接入 Autfy 库后执行）

- [ ] 从库源码中定位实际 HID report descriptor，确认 `Usage Page 0x0D / Usage 0x04`
- [ ] 确认 X/Y 为 16 bit 且 `Logical Maximum = 10000`（不是 0x7FFF 或 32767）
- [ ] 确认 Report ID = `0x01`、单帧 `5 字节`、布局 `[state][X_MSB][X_LSB][Y_MSB][Y_LSB]`
- [ ] 确认 `setScreenSize(1080, 2400)` 类的像素→逻辑坐标换算接口存在且上界正确
- [ ] 确认 `setCalibrationOffset(offsetX, offsetY)` 存在（对应规格 `residual_offset`）
- [ ] 发送一次 tap，抓取空中包/日志，验证 `state=0x03` 按下、`0x00` 抬起
- [ ] 验证 BLE 与 WiFi 图传共存不再断连（对应 R5 / Q3；库 README 宣称支持，但需实测量化）

## 3. 偏差处置（决策规则，已收口 2026-09-28）

- 若库实际 descriptor **完全符合**规格 → 直接使用，无需修改。
- 若仅上界/单位不同（如 0x7FFF） → 在控制器换算层做映射，**不**改动库内部，保持规格唯一真源。
- 若库**不支持** Digitizer 绝对坐标（仅相对鼠标） → 触发回退：`relative + 归零`（仅在 Y71t 实测确实不接受触摸屏描述符时才启用）。
- 关键护栏：descriptor 规格**必须写在规格文件内**（见上第 1 节），不依赖外部链接——`asterics/ESP32-BLE-HID` 已 404 先例（R11）证明引用链接本身是技术债。

## 4. 当前已完成（前置）

- `shared/coords.py` 已按上述规格实现 `hid_to_report` / `report_to_hid`（5 字节布局、0-10000 上界、0x03/0x00 状态）。
- `tests/test_coords.py` 已覆盖 report 打包/解析往返。
- 因此即便库的 descriptor 需微调，控制器侧换算逻辑已是规格真源，改动收敛在单点。
