# tools/ — 离线工具与标定探针

## 标定 / 测量

- `pointer_gain_probe.py` — **指针增益标定探针**（count ↔ 屏幕像素）。子命令：

  | 子命令 | 作用 |
  |---|---|
  | `discover` | 并发扫同网段 80/81 端口定位设备（无 mDNS；按 MAC OUI 找不可靠） |
  | `selfcheck` | 纯数学自检：合成位移 → 拟合，检验能还原已知增益（无设备） |
  | `simulate` | 内置模拟设备，全自动跑通 anchor→scan→fit 并还原已知增益（无设备） |
  | `anchor` | 角饱和归零：向左上猛推超量程 counts，把光标钉在 (0,0)（可见角） |
  | `scan` | 全自动增益扫描（相机像素）；**需相机对准手机屏幕**，否则会被视觉预检拦下 |
  | `manual` | 人工读数模式（屏幕像素）；不需要相机，用安卓「开发者选项 → 指针位置」读数 |
  | `fit` | 用 `scan`/`manual` 产出的 JSON 拟合增益 |

  关键设计见文件头注释：角饱和归零、自包含的帧差分位移测量，以及**两条物理约束
  的工具侧对策**（2026-10-02 真机定案）：

  1. **手机屏无输入即转暗 → 移动后必须尽快截屏。** 亮屏窗口实测只有约 10s，所以
     不做「等连续 N 帧全局稳定」（等到的只会是变暗）；改成：每次取样前先做**净零
     抖动** `wake()`（+N 再 −N，光标回原位）把屏钉在亮态 → **抢拍** `before` →
     移动 → **立刻抢拍** `after`（`SNAP_MARGIN=0.05`）且**先抓图再读 health** →
     用「前后帧亮度是否一致」（`--pair-tol`，默认 15 灰阶）判定这组能否用，
     不一致就整组重来（`CAPTURE_TRIES=3`，重试前必重新归零）。
     ⚠️ 净零抖动的正确性已实测：残留差分约为真实位移的 1/250。
  2. **位移过大会把光标推出可视范围 → 先归零、再小心移动。** `anchor` 用 −25000
     把光标钳到左上角；单档位移按**屏幕可见宽 × 0.75** 自适应钳位（另有
     `--max-counts` 硬上限）；测不到光标时**重新归零 + 位移减半**重试。

  ⚠️ `settle`/亮度门限必须与实际成像条件匹配：稳定亮态本身就有 ~17% 像素过曝
  （手机屏白底），过曝门限不能按 5% 卡；`anchor` 里**亮度不一致的轮次不得作为
  收敛依据**（手势会把变暗的屏点醒，那一瞬的亮度跳变会被读成 14003 px 的假斑块）。

  实测数据与完整推导见 `docs/evidence/2026-10-02_pointer_gain.md`。

- `gen_calib_grid.py` — 生成全屏标定网格（android_prep 用）
- `fake_h.py` — 给定已知 H 生成合成标定点（标定求解器自测用，seam_h_matrix 的 mock 端）

## 描述符

- `parse_hid_descriptor.py` — 解析 HID report map
- `verify_hid_descriptor.py` — 校验描述符字节与契约一致

## 串口 / 时序

- `watch_serial.py`、`reset_and_watch.py`、`trace_and_watch.py` — 串口观察与复位
  ⚠️ **用户做 BLE 测试期间禁开串口监听**：`ser.close()` 的 DTR 下降沿会复位板子、踢掉 BLE。
- `send_trace.py` — 下发并跟踪
- `precheck_syntax.py` — 语法预检

## 数据

- `wifi_credentials.nvs.csv` / `.bin` — WiFi 凭据 NVS 镜像（分区 `0x9000`，大小 `0x6000`）
  - ⚠️ **真实凭据不入库**：`wifi_credentials.nvs.csv` 与 `.bin` 均已在 `.gitignore` 中。
    模板见 `wifi_credentials.nvs.csv.example`（命名空间必须是 `qshou`，与固件
    `nvs_open("qshou", …)` 一致）。复制成 `.csv` 填真实值后，用
    `python $IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py generate <csv> <bin> 0x6000` 生成镜像。
