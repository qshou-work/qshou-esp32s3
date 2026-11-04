"""生成全屏标定网格 fixtures/calib_grid.png（android_prep step_7 准备用）。

用法（从仓库根 qshou/ 运行）：
    python tools/gen_calib_grid.py            # 默认 1080x2400 白底黑线
    python tools/gen_calib_grid.py 1080 2400 # 指定分辨率
    python tools/gen_calib_grid.py 1080 2400 0 0 0  # 黑底（用于浅色主题屏验证）

网格为 3x3（9 交点），与规格标定章节的 9 点标定一致。
若深色背景下交点取不到特征，改用黑底版本或调亮 fg。
"""
import os
import sys
from PIL import Image, ImageDraw

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "fixtures", "calib_grid.png")


def gen(w: int = 1080, h: int = 2400, bg=(255, 255, 255), fg=(0, 0, 0), n: int = 3) -> Image.Image:
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    # 内部网格线
    for i in range(1, n):
        x = int(w * i / n)
        y = int(h * i / n)
        d.line([(x, 0), (x, h)], fill=fg, width=max(2, w // 360))
        d.line([(0, y), (w, y)], fill=fg, width=max(2, w // 360))
    # 外边框
    d.rectangle([0, 0, w - 1, h - 1], outline=fg, width=max(3, w // 240))
    # 交点：外圈 + 实心中心点，便于角点检测
    r = max(12, w // 60)
    for i in range(n):
        for j in range(n):
            cx = int(w * i / n)
            cy = int(h * j / n)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=fg, width=max(2, w // 360))
            d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=fg)
    return img


def main():
    args = sys.argv[1:]
    w = int(args[0]) if len(args) > 0 else 1080
    h = int(args[1]) if len(args) > 1 else 2400
    if len(args) >= 5:
        bg = (int(args[2]), int(args[3]), int(args[4]))
    else:
        bg = (255, 255, 255)
    out = DEFAULT_OUT
    os.makedirs(os.path.dirname(out), exist_ok=True)
    gen(w, h, bg=bg).save(out)
    print(f"wrote {out}  ({w}x{h}, bg={bg})")


if __name__ == "__main__":
    main()
