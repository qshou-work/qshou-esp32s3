/*
 * platform/platform_camera.h — 相机参数调节（传感器级，真实生效，非预览 CSS 滤镜）
 *
 * 独立头文件：只被 platform/esp32/platform_camera.c、platform/host/platform_camera.c
 * 与 main/routes.c 包含；**不进入 platform.h**，以免触发全量重编译。
 *
 * 取值范围（OV2640 适用）：
 *   exposure_auto   : 1=自动曝光(AEC on)；0=手动(用 exposure_value)
 *   exposure_value  : 手动曝光值 0..1200（仅 auto=0 时生效）
 *   brightness/contrast/saturation : -2..2（传感器 DSP，符号级）
 * 这些参数直接改 OV2640 传感器，整路 MJPEG 推流都随之改变；预览端 CSS filter 近似
 * 仅作为「旧固件/离线」的兜底。
 */
#ifndef QSHOU_PLATFORM_CAMERA_H
#define QSHOU_PLATFORM_CAMERA_H

/* 设置曝光模式。auto_on=1 自动；=0 手动（aec_value 生效）。返回 0。 */
int platform_camera_set_exposure(int auto_on, int aec_value);

/* 设置亮度/对比度/饱和度（传感器 DSP，范围 -2..2；超出静默夹紧）。返回 0。 */
int platform_camera_set_image_adj(int brightness, int contrast, int saturation);

/* 自动功能开关（真实改 OV2640 传感器，整路 MJPEG 推流随之生效）：
 *   on=1 开启自动；=0 关闭（进入手动 / 冻结状态）。返回 0。
 *   auto_wb  : 自动白平衡 AWB  → set_whitebal
 *   auto_gain: 自动增益   AGC  → set_gain_ctrl（注意：本版本 esp32-camera 命名为
 *                             set_gain_ctrl，而非 set_gainctrl；曾因凭记忆写错名而编译失败）。 */
int platform_camera_set_auto_wb(int on);
int platform_camera_set_auto_gain(int on);

/* 读取当前生效值（NULL 指针不写入）。返回 0。
 * auto_wb / auto_gain 为新增的自动功能开关状态。 */
int platform_camera_get_settings(int *auto_on, int *aec_value, int *brightness,
                                int *contrast, int *saturation,
                                int *auto_wb, int *auto_gain);

/* 运行时切换分辨率（免重烧）。size = framesize_t 枚举值（OV2640 上限 UXGA=13，
 * 见 esp32-camera sensor.h）。内部调用 sensor->set_framesize 重新分配 framebuffer，
 * 改后会重新套用其它传感器参数。返回 0；参数非法或传感器未就绪返回 -1。 */
int platform_camera_set_framesize(int size);

/* 读取当前分辨率枚举值（NULL 不写入）。返回 0。 */
int platform_camera_get_framesize(int *out);

#endif /* QSHOU_PLATFORM_CAMERA_H */
