"""
fake_h.py —— 合成单应矩阵与标定数据生成器（seam_h_matrix 的 mock 端）

用途：
  - 标定求解器自测：给定已知 H，反向投影生成带噪声的相机检测点，
    再喂给 shared/coords.calibrate()，验证能还原 H 且残差 < 阈。
  - 在实物到货前即可完整开发与测试坐标换算链路，无需真实 H 矩阵。

生成模型（贴近真实安装几何）：
  屏幕点 scr --H_true--> 相机点 cap，其中 H_true 由
  旋转(小角度) + 非均匀缩放 + 平移 + 轻微透视项 组成，
  并叠加 Gaussian 检测噪声模拟摄像头角点抖动。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Tuple

import numpy as np


def random_homography(rng: np.random.Generator, w: int, h: int) -> np.ndarray:
    """生成一个贴近真实安装几何的 3x3 单应矩阵（src=scr -> dst=cap）。"""
    scale_x = rng.uniform(0.6, 1.4)
    scale_y = rng.uniform(0.6, 1.4)
    theta = rng.uniform(-0.12, 0.12)          # 轻微旋转
    tx = rng.uniform(0.0, 0.15 * w)
    ty = rng.uniform(0.0, 0.15 * h)
    # 轻微透视项（远小于 1），模拟摄像头斜视
    vx = rng.uniform(-8e-5, 8e-5)
    vy = rng.uniform(-8e-5, 8e-5)
    H = np.array([
        [scale_x * np.cos(theta), -scale_y * np.sin(theta), tx],
        [scale_x * np.sin(theta),  scale_y * np.cos(theta), ty],
        [vx, vy, 1.0],
    ], dtype=float)
    return H / H[2, 2]


def generate(n: int = 9, w: int = 1080, h: int = 2400, noise: float = 0.3,
             seed: int = 0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    生成 n 个标定对应点（3x3 网格），返回 (H_true, scr_pts, cap_pts)。

    scr_pts : 屏幕网格点 (Nx2)
    cap_pts : 经 H_true^-1 投影 + 高斯噪声后的相机检测点 (Nx2)
    """
    rng = np.random.default_rng(seed)
    H = random_homography(rng, w, h)
    Hinv = np.linalg.inv(H)

    # 3x3 网格分布在屏幕内 10%~90% 区域
    xs = np.linspace(0.1 * w, 0.9 * w, 3)
    ys = np.linspace(0.1 * h, 0.9 * h, 3)
    scr = np.array([[x, y] for y in ys for x in xs], dtype=float)[:n]

    cap = []
    for (sx, sy) in scr:
        p = Hinv @ np.array([sx, sy, 1.0], dtype=float)
        cx, cy = p[0] / p[2], p[1] / p[2]
        cx += rng.normal(0.0, noise)
        cy += rng.normal(0.0, noise)
        cap.append([cx, cy])
    cap = np.asarray(cap, dtype=float)
    return H, scr, cap


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成合成标定数据 (JSON 输出)")
    ap.add_argument("--n", type=int, default=9)
    ap.add_argument("--w", type=int, default=1080)
    ap.add_argument("--h", type=int, default=2400)
    ap.add_argument("--noise", type=float, default=0.3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=str, default=None,
                    help="输出 JSON 路径；缺省则打印到 stdout")
    args = ap.parse_args(argv)

    H, scr, cap = generate(n=args.n, w=args.w, h=args.h,
                           noise=args.noise, seed=args.seed)
    payload = {
        "H_true": H.tolist(),
        "scr_pts": scr.tolist(),
        "cap_pts": cap.tolist(),
        "meta": {"w": args.w, "h": args.h, "noise": args.noise, "seed": args.seed},
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[OK] 写入 {args.out} (n={args.n})")
    else:
        print(text)


if __name__ == "__main__":
    main()
