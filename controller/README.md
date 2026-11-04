# controller/ — Win10 控制器（A6 交付物，零硬件可跑）

MVP 控制台：托管 UI + 指令转发，复用 `shared/coords`（SCR→HID 换算）与 `shared/transport`（RelayBackend）。

## 文件

- `app.py` — `ControllerApp`：线程内 HTTP 服务
  - `GET /`：托管 `static/index.html`（注入 stream_url / scr 尺寸）
  - `GET /config`：返回 {scr_w, scr_h, stream_url}
  - `GET /api/v1/health`：透传设备 `/api/v1/health`
  - `POST /api/v1/tap|swipe|key|calibrate/point|calibrate/commit`：换算 → 打包 5 字节 BLE report → 经 RelayBackend 下发设备
- `mock_device.py` — `MockDevice`：模拟 ESP32 设备（MJPEG 测试流 + 指令回显），作为 `seam_video_source` 与设备 API 的 mock 端
- `static/index.html` — UI：视频区（点击=点击、拖拽=滑动）、标定网格叠加层、虚拟按键、健康面板

## 与 A3/A5 的衔接

- 指令经 `shared/transport.RelayBackend` 下发；mock 模式直连 `MockDevice`，真实部署改连 broker URL 即可（broker 接口契约不变，见 A5）
- 视频源是 `seam_video_source` 的约定端点：现在 = `MockDevice.stream_url`，到货后 = ESP32 `/api/v1/stream`（均走同一 MJPEG 形态）

## 运行

```bash
# 一键联调：启动 MockDevice + Controller，浏览器打开打印的 Controller 地址
python -m controller.app
```

## 测试

```bash
python -m unittest tests.test_controller
```
