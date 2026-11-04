# 2026-10-15 换机修复 + 全量重编 + 烧录 + 基线复验

> 承接 `HANDOVER.md`。本文只记录**本次换机后实际发生了什么**，以及**与交接文档假设不符之处**。
> 基线四层已全部复验一致，环境已可正常开发。

---

## 0. 结论速览

| 项 | 状态 |
|---|---|
| ESP-IDF v6.1 + 工具链 + Python venv | ✅ 已装齐（版本与脚本硬编码完全吻合） |
| 交接包内 205MB `firmware/build/` | ❌ **换机后不可用**（绝对路径写死），已删并全量重编 |
| 全量重编 | ✅ 完成，产物 `qshou.bin` = 1,433,632 B (0x15E020) |
| 增量编译 | ✅ 可用（改 1 个 `.c` → 9 个目标 / 81s） |
| 烧录 | ✅ COM3 成功，**NVS 未擦**，设备自动重连 |
| 第 1 层 离线单测 | ✅ `214/214 passed, failures=0 errors=0 \| firmware contract rc=0` |
| 第 2 层 工具清单 | ✅ `29 个工具 / 26 个端点` |
| 第 3 层 真机 MCP 端到端 | ✅ `17/17 passed` |
| 第 4 层 设备在线 | ✅ `192.168.2.160` HTTP 200 |

**四层全部与交接文档所述基线一致** → 可以开始推进 §5 未完成项。

---

## 1. 环境搭建（原机不存在，全部新装）

| 组件 | 位置 / 版本 |
|---|---|
| ESP-IDF | `C:\Users\<username>\esp\esp-idf`，git tag `v6.1`（commit `4dc1c655`） |
| 工具链根 | `C:\Users\<username>\.espressif` |
| IDF python env | `idf6.1_py3.13_env`（与 `idf_run.py` / `idf_build_flash.py` 硬编码**完全一致**） |
| Python venv（宿主侧） | `~/.workbuddy-ai/binaries/python/envs/default`（py3.13.14） |
| 依赖 | numpy 2.5.4 / pyyaml 6.0.3 / httpx 0.28.1 / websockets 17.2 / **mcp 2.3.0** / pyserial 3.5 |

工具版本全部吻合脚本预期：cmake 4.0.3 / ninja 1.12.1 / ccache 4.12.1 /
xtensa-esp-elf `esp-15.2.0_20251204` / openocd `v0.12.0-esp32-20260703` /
esp-clangd `esp-21.1.3_20260408` / esp-rom-elfs `20241011`。

**踩坑 1 — 下载源**：`dl.espressif.com` 本机**不可达**（HTTP 000）。
改用国内镜像 `IDF_GITHUB_ASSETS=dl.espressif.cn/github_assets`（实测 200）。

**踩坑 2 — 子模块未对齐**：`git clone --shallow-submodules` 会把子模块拉到
**分支头而非固定提交**（`git submodule status` 带 `+` 前缀）。
已逐个 `git fetch --depth 1 origin <sha>` + `git checkout --detach <sha>` 拉齐
（nimble / lwip / mbedtls / esp_phy / esp_wifi / esp_coex 等 19 个）。
剩余 2 个未对齐但**本项目用不到**：`components/openthread/openthread`（Thread 未启用）、
`components/cmock/CMock/vendor/*`（IDF 自带单测框架）。

---

## 2. 🔴 重大发现 A：交接包的 `firmware/build/` 换机后不可用

**交接文档假设**（§1.2 / §2.2）：保留 `build/` 目录即可免去 ~44 分钟全量编译。

**实测结论：该假设在换机场景下不成立。**

- ESP-IDF 硬报错：
  ```
  Build directory '...' configured for project
  'C:\Users\<username>\WorkBuddy\2026-09-28-12-02-05\qshou\firmware'
  not '...'. Run 'idf.py fullclean' to start again.
  ```
- 根因：`build.ninja` / `CMakeCache.txt` / `project_description.json` 里
  **写死了旧机器的绝对路径**（源码路径 + 旧 PortableGit 路径）。
  CMake 检测到 `CMAKE_HOME_DIRECTORY` 不符即拒绝复用。
- **目录联接（junction）方案已验证无效**：把旧路径联接到工程新位置后，
  IDF 确实不再报错、CMake 接受该 build 目录（CMakeCache 仅 4 行内部簿记差异，
  `CMAKE_HOME_DIRECTORY` 未变），**但 ninja 仍判定全量 1272 目标**
  （ccache 无热缓存：185 次调用 0 命中）。故联接无收益，已移除。
- **裁定**：工程移到工作区根目录，删掉失效 `build/`，全量重编一次。
  此后改 `.c` 才是真增量。

> 交接文档 §2.2 本身也提示过这个风险，本次实测确认了它。

---

## 3. 🔴🔴 重大发现 B：构建**必须前台运行**，后台托管会被冻结

**这是本次最耗时的坑，且完全不报错，极易误判为"编译卡死"。**

### 现象

用 `nohup ... &` 或后台任务方式跑 `idf_build_flash.py`，构建会**静默冻结**：

| 第几次 | 冻结位置 | 当时进度 |
|---|---|---|
| 第 1 次 | `[751/1278] Completed 'bootloader'` 之后 | 823 个 `.obj` 已生成 |
| 第 2 次 | `[523/530] Linking ... libmain.a` 之后 | 1258 个 `.obj` 已生成 |

表现：`.ninja_log` 行数不再增长，日志**无任何报错**，进程仍在但毫无进展。

### 诊断链（全部实测，非推测）

1. 卡住时进程树为 `ninja → ccache → xtensa-esp32s3-elf-gcc`，**只有这一个编译子进程**。
2. 读该进程 PEB 拿到完整命令行 —— 是一条**平凡查询命令**：
   ```
   gcc "-print-file-name=.../firmware/build/specs/picolibc.specs"
   ```
3. 该命令**单独跑**、**经 ccache 跑**都**瞬间返回 rc=0**（15s 超时未触发）。
4. `GetProcessTimes` 采样 10 秒：**Δkernel=0.0000s，Δuser=0.0000s**
   → 进程**零 CPU、被阻塞**，不是在计算。
5. ccache 自身健康（`--version` / `-s` 正常；1537 次调用 100% miss）；
   ccache 锁目录为空 → **排除 ccache 锁**。
6. 沙箱开 / 关**都能前台跑通** → **沙箱不是原因**。

### 结论与解法

**不是编译错误、不是 ccache、不是代码问题 —— 是后台托管的进程树被冻结。**

**解法：构建一律前台同步运行**（不要用 `run_in_background`，不要 `nohup ... &`），
给足超时即可。实测前台全量跑完 + 前台增量 81s 均正常。

> ⚠️ 推论：本机 CI / 自动化若要把 IDF 构建丢到后台 job，会踩同一个坑。

---

## 4. 全量重编结果

```
[8/10]  Linking CXX executable qshou.elf
[9/10]  Generating binary image from built executable
Generated .../build/qshou.bin
qshou.bin binary size 0x15e020 bytes.
Smallest app partition is 0x400000 bytes. 0x2a1fe0 bytes (66%) free.
Project build complete.
```

| 产物 | 大小 |
|---|---|
| `qshou.bin` | **1,433,632 B (0x15E020)**，app 分区余量 **66%** |
| `qshou.elf` | 13,043,884 B |
| `bootloader/bootloader.bin` | 22,608 B |
| `partition_table/partition-table.bin` | 3,072 B |

`flash_args`：
```
--flash-mode dio --flash-freq 80m --flash-size 16MB
0x0     bootloader/bootloader.bin
0x8000  partition_table/partition-table.bin
0x10000 qshou.bin
```

### 增量验证（关键：证明后续改代码不再全量）

`touch main/routes.c` 后重编 → **仅 9 个目标 / 81 秒**：

```
[1/9] Building C object .../__idf_main.dir/routes.c.obj
[3/9] Linking C static library esp-idf\main\libmain.a
[6/9] Generating esp-idf/esp_system/ld/sections.ld
[7/9] Linking CXX executable qshou.elf
[8/9] Generating binary image from built executable
[9/9] check_sizes.py ... partition --type app
```

✅ 改 `.c` 已是真增量。

---

## 5. 烧录（COM3）

- 板子串口：**COM3 = USB-Enhanced-SERIAL CH343**（`VID:PID=1A86:55D3`，LOCATION=1-11.2）
- 命令：`python idf_build_flash.py --port COM3`（前台，全程 54s）
- 写入了 `0x0 bootloader` + `0x8000 分区表` + `0x10000 app`；
  **`0x9000 NVS` 不在 `flash_args` 内 → 未被擦除**（符合纪律 #3）

```
Wrote 1433632 bytes (867009 compressed) at 0x00010000 in 21.5 seconds
Verifying written data...
Hash of data verified.
Hard resetting via RTS pin...
Done
```

**NVS 存活确认**：复位后 `/api/v1/sysinfo` 返回
`nvs.valid=1, used_entries=205, namespace_count=6`
→ WiFi 凭据 + BLE bonding 均保留，设备**自动重连回 `192.168.2.160`**。

---

## 6. 四层基线复验（对刚烧录的设备）

```
第 1 层  run_tests.py
  → python 214/214 passed, failures=0 errors=0 | firmware contract rc=0

第 2 层  controller.mcp_adapter.server --host 192.168.2.160 --list-tools
  → 已注册 29 个工具（设备 192.168.2.160 自描述 26 个端点）

第 3 层  tools/verify_mcp_live.py 192.168.2.160
  → 17/17 passed

第 4 层  设备在线
  → http://192.168.2.160/api/v1/sysinfo  HTTP 200 / 857 B
```

**四层全部与交接文档基线一致。**
新装的 IDF v6.1 工具链（含子模块重新对齐）产出的固件**不仅能编译，而且能跑**。

### 本机运行注意

- 跑第 2/3 层**必须** `export NO_PROXY=192.168.2.160`。
  本机有 **Mihomo(Clash) TUN 适配器 `198.18.0.1`**，会劫持跨网段 TCP →
  连任意端口都返回"连接成功"造成**假连通**。
- `verify.sh` 内设备探测写的是旧 IP `192.168.110.111`；
  本机需 `HOST=192.168.2.160 bash verify.sh` 强制指定。
- **未跑**变异验证（`tests/mutation_check_*.py`）：约 40 分钟，且会**原地改写源文件**。
  当前源码干净，需要时再跑，跑完须独立复跑 `run_tests.py` 确认 rc=0。

---

## 7. 本机脚本适配（已改）

| 文件 | 改动 |
|---|---|
| `idf_run.py` | PortableGit 路径 `.workbuddy` → `.workbuddy-ai`；`PROJECT_DIR` 默认值改为**从 `__file__` 推导** |
| `firmware/idf_build_flash.py` | 同上 PortableGit 路径修正 |
| `Qshou_KVM_HANDOVER/verify.sh` | `PROJ` 优先 `$HERE/../qshou`、回退旧路径；python 探测补上 `.workbuddy-ai` 路径 |

---

## 8. 下一步（待定）

`HANDOVER.md` §5 未完成项：

| 项 | 说明 | 可自主推进？ |
|---|---|---|
| **5.1 鉴权** | `routes.c` 里 `g_control_auth` 是编译期常量 `false`，`require_auth()` 仅骨架 | ✅ **唯一可完全自主推进的上市阻断项** |
| 5.2 counts↔像素标定 | 需要人看屏 | ❌ 需人工 |
| 5.3 远程访问 | 需穿透/公网采购 | ❌ 外部依赖 |
| 5.4 TLS | 需证书方案 | ❌ 外部依赖 |
| 5.5 延迟优化 | 之前卡在"需全量重编才能验证" | ⚠️ 现已可增量，具备条件 |
| 5.6 单应矩阵 H | 需人标定 | ❌ 需人工 |
