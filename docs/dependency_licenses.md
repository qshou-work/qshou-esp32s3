# 依赖许可证清点（任务 C4 / R18）

> 状态：**部分完成**。Python 侧已确认；固件侧核心依赖 `Autfy/ESP32-BLE-HID` 许可证待确认（R18 gap）。
> 本清单为开源分发前置动作（纯调研，不阻塞跑通）。

## 1. 已确认依赖（Python 控制/测试侧）

| 组件 | 用途 | 许可证 | 分发影响 |
|---|---|---|---|
| CPython 3.13 标准库 | 传输/HTTP/MJPEG 解析/broker | PSF License | 无附加义务 |
| numpy 2.5.1 | 单应矩阵求解 | BSD-3-Clause | 宽松，仅需保留版权声明 |
| Pillow 12.3.0 | **仅 dev mock 视频源**，非最终分发产物 | MIT / HPND | 不影响固件/控制端分发 |

> 注：控制端最终形态若是纯前端（HTML/JS）或打包二进制，Python 侧依赖不构成分发负担。
> Pillow 仅用于本地 mock 视频，不会进入发布包。

## 2. 固件侧预期依赖（到货后接入，需逐项复核）

| 组件 | 用途 | 已知许可证 | 状态 |
|---|---|---|---|
| Arduino-ESP32 核心 | ESP32-S3 运行时 | LGPL-2.1 + 部分 Apache-2.0 | 需确认具体版本条款 |
| esp32-camera | OV2640 采集 / MJPEG | MIT | 宽松 |
| WiFi / 网络库 | 图传 / broker 连接 | 随核心分发 | 同核心 |
| **Autfy/ESP32-BLE-HID** | BLE HID（键盘+绝对触控） | **待确认（R18 gap）** | ⚠️ 未清点 |
| T-vK ESP32-BLE-Keyboard（Autfy 上游） | 键盘能力 | 需确认 | Autfy 已整合 |

## 3. 风险（关联 risk_register）

- **R11**：`Autfy/ESP32-BLE-HID` 为单人维护项目，可能停更或引入破坏性变更；`asterics/ESP32-BLE-HID` 已 404 先例。
- **R18**：核心依赖许可证**尚未清点**，无法确认与本项目分发意图（含未来 AGPL/SSPL broker）兼容。

## 4. 行动项（前置，待执行）

- [ ] 到货后读取 `Autfy/ESP32-BLE-HID` 仓库 LICENSE 文件，填入上表第 2 节
- [ ] 确认其与 Arduino-ESP32 / esp32-camera 许可证组合无冲突
- [ ] **fork 到自有仓库并锁定 commit**（R11 mitigation），不直连上游 `main`
- [ ] 将 descriptor 规格保留在 `Qshou_Spec.yaml` 内（不依赖链接），脱离库也能自行实现
- [ ] 若未来 broker 采用 AGPL/SSPL，复核固件侧许可证与之不构成「视为衍生作品」的传染风险

## 5. 结论

当前可放心推进「跑通第一」：Python 侧依赖零风险，固件侧唯一缺口是 Autfy 许可证清点，属**开放前置条件**（不阻塞 MVP 内测），但必须在官方 broker 承载真实用户前结清（见 `execution_policy.guardrails` 精神）。
