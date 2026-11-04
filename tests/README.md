# tests/ — Python 自动化断言

## 运行

```bash
python run_tests.py           # 仓库根目录；同时跑本目录 unittest + firmware/tests 契约测试
python run_tests.py -v
```

⚠️ 需要 numpy。报 `ModuleNotFoundError: numpy` 是**环境缺依赖**（本机 managed Python 3.13
默认没装），不是代码问题 —— 换装过 numpy 的解释器，或先 `python -m pip install numpy`。

> 历史坑：本目录没有 `__init__.py`，过去只能逐个 `python tests/test_xxx.py` 跑，
> 容易漏跑；换个解释器就整片红。`run_tests.py` 已用 `unittest discover` 统一收口。

## 用例文件

| 文件 | 覆盖 |
|---|---|
| `test_coords.py` | 坐标换算、H 求解、report 打包/解包、`CursorState` 相对增量与 clamp |
| `test_calib.py` | 用 `tools/fake_h.py` 合成点校验标定精度与残差判据 |
| `test_controller.py` | 控制器端点：swipe 相对增量、起点=当前光标、tap/key 转发 |
| `test_broker.py` | 中转服务端：token 校验、透传、错误路径 |
| `test_transport.py` | 传输层：HTTP POST 注入、MJPEG 分帧解析 |
| `test_assertions.py` | `assertions.py` 的用例驱动骨架自测 |
| `assertions.py` | 端到端用例运行器（UC1–UC5），产出 `evidence/UC*_<时间戳>.json` |
| `evidence/` | 真机/联调用例证据（JSON，带时间戳，可回溯） |

固件侧契约测试在 `../firmware/tests/test_firmware_contract.py`（HID 描述符逐字节、
三个 Report ID 的长度、按键映射表、GESTURE_DIR 位移预算），由 `run_tests.py` 一并调用。
