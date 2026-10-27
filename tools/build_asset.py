#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
绿幕视频 → dsh-pet 规范的透明 WebM。

把豆包/即梦等工具生成的绿幕视频，一步做成银月能直接用的素材：
抠像 → 按插件基准归一化 → VP9 + Alpha 编码。

用法：
  python build_asset.py 素材.mp4 --name 待机-呼吸
  python build_asset.py 素材.mp4 --name 待机-呼吸 --loop 6
  python build_asset.py 帧目录/ --name 小动作-托腮 --fps 24
  python build_asset.py 素材.mp4 --name 试做 --dry-run      # 只测量，不写盘

常用选项：
  --name      动画名，= 输出文件名（不含扩展名），也是注册器认的键
  --out       输出目录，默认 ../pet/yinyue-animation
  --like X    对齐基准动画，默认「待机」：量它的身高/脚底/中心/亮度，新素材照它来
  --feet/--height/--center  手动指定对齐目标（覆盖 --like 的量测值）
  --loop N    尾部 N 帧交叉淡化到第 2 帧，做无缝循环
  --trim a,b  只取第 a~b 帧（1 起，含两端）
  --no-match  不做亮度对齐
  --crf N     VP9 质量，默认 30（越小越清晰越大）
  --dry-run   只打印测量结果
  --keep      保留中间帧目录（排查抠像问题用）

为什么要归一化：dsh-pet 的基准画布是 640×360，脚底固定在 y=330（91.7%），
点击命中区也按 640 宽的比例缩放。素材不按这个基准构图，宠物就会悬空、
点不中、或者和别的动画切换时「跳一下」。

依赖：numpy、Pillow、ffmpeg（imageio-ffmpeg 自带一份也行）
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess

import numpy as np
from PIL import Image

# ---- dsh-pet 基准（640×360 的 2 倍）----
CANVAS_W, CANVAS_H = 1280, 720
FEET_Y = 660          # 330 / 360 × 720
CHAR_H = 546          # 与现有动画的中位身高一致
CENTER_X = 640
FPS = 24

# ---- 抠像参数（针对带渐变的橄榄绿背景调过，纯 #00FF00 也适用）----
T0, T1 = 0.030, 0.090     # 软边过渡区间（归一化绿度 E）
T_BG = 0.090              # 背景硬判定阈值
GAIN = 0.05               # alpha 轻微收缩，去 1px 光晕


# ---------------- 抠像 ----------------

def shift(m, dy, dx):
    out = np.zeros_like(m)
    h, w = m.shape
    ys = slice(max(0, dy), h + min(0, dy))
    yd = slice(max(0, -dy), h + min(0, -dy))
    xs = slice(max(0, dx), w + min(0, dx))
    xd = slice(max(0, -dx), w + min(0, -dx))
    out[yd, xd] = m[ys, xs]
    return out


def dilate(m, r=1):
    o = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy or dx:
                o |= shift(m, dy, dx)
    return o


def erode(m, r=1):
    o = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy or dx:
                o &= shift(m, dy, dx)
    return o


def box_mean(x, r=2):
    """(2r+1)² 均值滤波，用积分图实现。"""
    h, w = x.shape
    p = np.pad(x, r, mode="edge").astype(np.float64)
    ii = p.cumsum(0).cumsum(1)
    ii = np.pad(ii, ((1, 0), (1, 0)))
    k = 2 * r + 1
    s = ii[k:k + h, k:k + w] - ii[0:h, k:k + w] - ii[k:k + h, 0:w] + ii[0:h, 0:w]
    return (s / (k * k)).astype(np.float32)


def flood_from_border(cand, scale=4):
    """从画面边界在 cand 内泛洪，返回与边界连通的部分（降采样加速）。"""
    h, w = cand.shape
    small = cand[::scale, ::scale]
    m = np.zeros_like(small)
    m[0, :] = small[0, :]
    m[-1, :] = small[-1, :]
    m[:, 0] = small[:, 0]
    m[:, -1] = small[:, -1]
    m &= small
    for _ in range(small.shape[0] + small.shape[1]):
        prev = m.sum()
        g = m | shift(m, 1, 0) | shift(m, -1, 0) | shift(m, 0, 1) | shift(m, 0, -1)
        m = g & small
        if m.sum() == prev:
            break
    up = np.repeat(np.repeat(m, scale, axis=0), scale, axis=1)
    return up[:h, :w]


def flood(seed, allowed, max_iter=400):
    m = seed & allowed
    for _ in range(max_iter):
        prev = m.sum()
        g = m | shift(m, 1, 0) | shift(m, -1, 0) | shift(m, 0, 1) | shift(m, 0, -1)
        m = g & allowed
        if m.sum() == prev:
            break
    return m


def label_components(mask, maxn=60):
    """4 邻域连通域，返回 [(mask, area, cy, cx)]，超过 maxn 个就只留最大的。"""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pts = []
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(pts) < 20:
            continue
        arr = np.array(pts)
        m = np.zeros_like(mask)
        m[arr[:, 0], arr[:, 1]] = True
        out.append((m, len(pts), float(arr[:, 0].mean()), float(arr[:, 1].mean())))
        if len(out) >= maxn:
            break
    return out


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def key_frame(rgb):
    """绿幕 → RGBA。返回 (rgba uint8, alpha float, char mask)。"""
    a = rgb.astype(np.float32)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = np.maximum(R, B)
    E = (G - mx) / np.maximum(G, 1.0)     # 归一化绿度，对光照不敏感
    Es = box_mean(E, 2)

    cand = Es > T_BG
    bg = flood(flood_from_border(cand, 4) & cand, cand)

    # 被毛发/尾巴围住的绿色：面积大且紧邻肤色 → 眼睛（保留），否则当背景缝（抠掉）
    inner = cand & ~bg
    skin = (R - B > 30) & (R > 140) & (E < 0.02)
    keep_green = np.zeros_like(bg)
    for m, area, _cy, _cx in label_components(inner):
        near_skin = (dilate(m, 5) & skin).sum() / max(area, 1)
        if area >= 300 and near_skin >= 0.20:
            keep_green |= m
            continue
        bg |= m
    char0 = ~bg

    # 主连通域：从人物内部泛洪，剔掉地面孤立绿斑
    seed = erode(char0, 8) & (E < 0.02)
    if int(seed.sum()) < 500:
        seed = erode(char0, 4) & (E < 0.02)
    char = flood(seed, char0)

    alpha = 1.0 - smoothstep(T0, T1, E)
    alpha[erode(char, 2)] = 1.0
    alpha[keep_green] = 1.0
    alpha[~dilate(char, 2)] = 0.0
    alpha = np.clip((alpha - GAIN) / (1.0 - GAIN), 0, 1)

    # despill：压掉绿色超额，白发/尾巴边缘不留绿边
    gex = G - mx
    w8 = np.where(keep_green, 0.0, 0.9).astype(np.float32)
    G2 = G - np.maximum(gex, 0) * w8

    rgba = np.dstack([R, G2, B, alpha * 255.0])
    return np.clip(rgba, 0, 255).astype(np.uint8), alpha, char


# ---------------- 尺寸归一化 ----------------

def measure(frames):
    """统计每一帧人物 bbox，给出中位身高 / 中位脚底 / 中位中心。"""
    heights, bottoms, centers = [], [], []
    ys0, ys1, xs0, xs1 = 10 ** 9, -1, 10 ** 9, -1
    for im in frames:
        al = np.asarray(im)[:, :, 3]
        m = al > 89
        if not m.any():
            continue
        ys, xs = np.nonzero(m)
        heights.append(float(ys.max() - ys.min() + 1))
        bottoms.append(float(ys.max()))
        centers.append(float(np.median(xs)))
        ys0, ys1 = min(ys0, int(ys.min())), max(ys1, int(ys.max()))
        xs0, xs1 = min(xs0, int(xs.min())), max(xs1, int(xs.max()))
    if not heights:
        raise SystemExit("✗ 抠像后什么都没剩下，检查视频是不是绿幕")
    return {
        "med_h": float(np.median(heights)),
        "med_bottom": float(np.median(bottoms)),
        "med_x": float(np.median(centers)),
        "union": (ys0, ys1, xs0, xs1),
        "frames": len(heights),
    }


def resize_premul(im, w, h):
    """预乘 alpha 再缩放 —— 直接缩 RGBA 会让透明边缘渗灰。"""
    a = np.asarray(im).astype(np.float32)
    al = a[:, :, 3:4] / 255.0
    pre = np.dstack([a[:, :, :3] * al, a[:, :, 3]])
    small = Image.fromarray(np.clip(pre, 0, 255).astype(np.uint8), "RGBA").resize(
        (max(1, w), max(1, h)), Image.LANCZOS)
    b = np.asarray(small).astype(np.float32)
    al2 = np.maximum(b[:, :, 3:4] / 255.0, 1e-6)
    rgb = np.clip(b[:, :, :3] / al2, 0, 255)
    out = np.dstack([rgb, b[:, :, 3]])
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def layout(frames, feet_y, char_h, center_x, cw=CANVAS_W, ch=CANVAS_H):
    M = measure(frames)
    scale = char_h / M["med_h"]
    oy = int(round(feet_y - M["med_bottom"] * scale))
    ox = int(round(center_x - M["med_x"] * scale))
    out = []
    for im in frames:
        w, h = im.size
        rs = resize_premul(im, int(round(w * scale)), int(round(h * scale)))
        c = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        c.paste(rs, (ox, oy), rs)
        out.append(c)
    M.update({"scale": scale, "offset": [ox, oy], "out_h": M["med_h"] * scale})
    return out, M


def make_loop(frames, k):
    """尾部 k 帧交叉淡化回第 2 帧，让循环接缝不跳。"""
    if k <= 0 or len(frames) <= k + 2:
        return frames
    head = frames[1]
    cut = len(frames) - 1 - k
    out = frames[2:cut + 1]
    for j in range(k):
        out.append(Image.blend(frames[cut + 1 + j], head, (j + 1) / k))
    return out


def match_brightness(frames, ref_path, ffmpeg):
    """把平均 RGB 对齐到参考动画，避免切换状态时亮度跳变。"""
    ref_im = load_reference(ref_path, ffmpeg)
    if ref_im is None:
        print("  警告：读不到参考动画，跳过亮度对齐")
        return frames, [1.0, 1.0, 1.0]

    def mean_rgb(im):
        a = np.asarray(im).astype(np.float32)
        m = a[:, :, 3] > 200
        return a[:, :, :3][m].mean(0) if m.any() else None

    r = mean_rgb(ref_im)
    acc, n = np.zeros(3), 0
    for im in frames:
        a = np.asarray(im).astype(np.float32)
        m = a[:, :, 3] > 200
        if m.any():
            acc += a[:, :, :3][m].sum(0)
            n += int(m.sum())
    if r is None or n == 0:
        return frames, [1.0, 1.0, 1.0]
    cur = acc / n
    gain = np.clip(r / np.maximum(cur, 1.0), 0.80, 1.25)
    out = []
    for im in frames:
        a = np.asarray(im).astype(np.float32)
        a[:, :, :3] *= gain[None, None, :]
        out.append(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA"))
    print("  亮度对齐：当前 %s → 参考 %s  增益 %s"
          % (np.round(cur, 1).tolist(), np.round(r, 1).tolist(), np.round(gain, 3).tolist()))
    return out, [round(float(g), 4) for g in gain]


def load_reference(path, ffmpeg):
    if os.path.isdir(path):
        pngs = sorted(f for f in os.listdir(path) if f.lower().endswith(".png"))
        if not pngs:
            return None
        return Image.open(os.path.join(path, pngs[0])).convert("RGBA")
    tmp = tempfile.mkdtemp(prefix="ref_")
    try:
        # 必须显式指定 libvpx-vp9 解码器，否则拿不到 alpha 通道，
        # 透明区域会被当成黑色算进平均色（亮度对齐直接偏到 0.8 下限）
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-c:v", "libvpx-vp9", "-i", path, "-frames:v", "1", "-c:v", "png",
                        os.path.join(tmp, "f.png")], check=True)
        p = os.path.join(tmp, "f.png")
        return Image.open(p).convert("RGBA") if os.path.exists(p) else None
    except subprocess.CalledProcessError:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def calibrate(path, ffmpeg, max_frames=24):
    """从已有的一个动画量出「身高 / 脚底 / 中心」，作为新素材的对齐基准。

    比写死常数靠谱：包里现在的构图是多少，新的就对齐到多少，
    换素材、改基准之后不用改代码。
    """
    tmp = tempfile.mkdtemp(prefix="cal_")
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-c:v", "libvpx-vp9", "-i", path,
                        "-frames:v", str(max_frames),
                        os.path.join(tmp, "%04d.png")], check=True)
        pngs = sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".png"))
        if not pngs:
            return None
        M = measure(load_frames(pngs))
        return M
    except (subprocess.CalledProcessError, SystemExit):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- IO ----------------

def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise SystemExit(
            "✗ 找不到 ffmpeg。装一个：winget install Gyan.FFmpeg，"
            "或者 pip install imageio-ffmpeg")


def extract(video, dst, fps):
    os.makedirs(dst, exist_ok=True)
    subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                    "-i", video, "-vf", "fps=%d" % fps,
                    os.path.join(dst, "%04d.png")], check=True)
    return sorted(os.path.join(dst, f) for f in os.listdir(dst) if f.endswith(".png"))


def load_frames(paths):
    return [Image.open(p).convert("RGBA") for p in paths]


def key_all(paths, verbose=True):
    out = []
    for i, p in enumerate(paths):
        rgb = np.asarray(Image.open(p).convert("RGB"))
        rgba, _alpha, _char = key_frame(rgb)
        out.append(Image.fromarray(rgba, "RGBA"))
        if verbose and (i + 1) % 24 == 0:
            print("  抠像 %d/%d" % (i + 1, len(paths)))
    return out


def encode(frames, dst, crf):
    tmp = tempfile.mkdtemp(prefix="stage_")
    try:
        for n, im in enumerate(frames, 1):
            im.save(os.path.join(tmp, "%04d.png" % n))
        subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                        "-framerate", str(FPS), "-i", os.path.join(tmp, "%04d.png"),
                        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                        "-b:v", "0", "-crf", str(crf),
                        "-auto-alt-ref", "0", "-row-mt", "1", "-threads", "8",
                        "-an", dst], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def opt(name, default=None, cast=str):
    if name not in sys.argv:
        return default
    i = sys.argv.index(name)
    if i + 1 >= len(sys.argv):
        raise SystemExit("✗ %s 后面缺一个值" % name)
    return cast(sys.argv[i + 1])


def main():
    opts_with_value = {"--name", "--out", "--feet", "--height", "--center", "--like",
                       "--loop", "--trim", "--match", "--crf", "--fps"}
    src = None
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a in opts_with_value:
            skip_next = True
            continue
        if not a.startswith("--"):
            src = a
            break

    name = opt("--name")
    if not src:
        raise SystemExit("用法：python build_asset.py <视频或帧目录> --name <动画名> [--loop N]")
    if not name:
        raise SystemExit("✗ 缺 --name（动画名 = 输出文件名，注册器靠它识别）")

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.abspath(opt("--out", os.path.join(here, "..", "pet", "yinyue-animation")))
    feet_y = opt("--feet", None, float)
    char_h = opt("--height", None, float)
    center_x = opt("--center", None, float)

    # 对齐基准：优先量包里已有的动画，量不到再退回基准常量
    like = opt("--like", "待机")
    ref_anim = None
    if like:
        cand = like if os.path.exists(like) else None
        if cand is None:
            for base in (out_dir, os.path.join(here, "..", "pet", "yinyue-animation")):
                p = os.path.join(base, like if like.endswith(".webm") else like + ".webm")
                if os.path.exists(p):
                    cand = p
                    break
        if cand:
            ref_anim = cand
            if None in (feet_y, char_h, center_x):
                Mref = calibrate(cand, find_ffmpeg())
                if Mref:
                    feet_y = feet_y if feet_y is not None else Mref["med_bottom"]
                    char_h = char_h if char_h is not None else Mref["med_h"]
                    center_x = center_x if center_x is not None else Mref["med_x"]
                    print("对齐基准：%s（身高 %.0f / 脚底 %.0f / 中心 %.0f）"
                          % (os.path.basename(cand), Mref["med_h"], Mref["med_bottom"], Mref["med_x"]))
    feet_y = FEET_Y if feet_y is None else feet_y
    char_h = CHAR_H if char_h is None else char_h
    center_x = CENTER_X if center_x is None else center_x
    loop_k = opt("--loop", 0, int)
    crf = opt("--crf", 30, int)
    fps = opt("--fps", FPS, int)
    dry = "--dry-run" in sys.argv
    keep = "--keep" in sys.argv

    trim = None
    if opt("--trim"):
        a, b = opt("--trim").split(",")
        trim = (int(a), int(b))

    print("源    :", src)
    print("动画名:", name)
    print("输出  :", os.path.join(out_dir, name + ".webm"))
    print()

    tmp = tempfile.mkdtemp(prefix="asset_")
    try:
        print("[1/4] 取帧")
        if os.path.isdir(src):
            paths = sorted(os.path.join(src, f) for f in os.listdir(src)
                           if f.lower().endswith(".png"))
        else:
            paths = extract(src, tmp, fps)
        if trim:
            paths = paths[max(0, trim[0] - 1): trim[1]]
        print("  %d 帧（%.2f 秒 @ %dfps）" % (len(paths), len(paths) / fps, fps))
        if not paths:
            raise SystemExit("✗ 没取到帧")

        print("[2/4] 抠像")
        frames = key_all(paths)

        print("[3/4] 归一化到 dsh-pet 基准")
        print("  目标：脚底 y=%d  身高 %.0f px  中心 x=%d  画布 %dx%d"
              % (feet_y, char_h, center_x, CANVAS_W, CANVAS_H))
        laid, M = layout(frames, feet_y, char_h, center_x)
        print("  原始：身高 %.0f  脚底 %.0f  中心 %.0f"
              % (M["med_h"], M["med_bottom"], M["med_x"]))
        print("  缩放 %.4f  偏移 (%d,%d)" % (M["scale"], M["offset"][0], M["offset"][1]))

        if loop_k:
            laid = make_loop(laid, loop_k)
            print("  循环处理：尾部 %d 帧淡化回第 2 帧 → %d 帧" % (loop_k, len(laid)))

        gain = [1.0, 1.0, 1.0]
        ref = opt("--match")
        if ref is None and ref_anim and "--no-match" not in sys.argv:
            ref = ref_anim      # 默认跟着基准动画的亮度走，避免切换时跳亮
        if ref:
            laid, gain = match_brightness(laid, ref, find_ffmpeg())

        # 落地后复测，确认真的对上了
        M2 = measure(laid)
        print("  复测：身高 %.0f  脚底 %.0f  中心 %.0f"
              % (M2["med_h"], M2["med_bottom"], M2["med_x"]))
        dx = M2["med_x"] - center_x
        dy = M2["med_bottom"] - feet_y
        if abs(dx) > 6 or abs(dy) > 6:
            print("  ⚠ 偏差偏大（中心 %+.1f / 脚底 %+.1f），素材可能有多帧姿态变化太大" % (dx, dy))

        if dry:
            print("\n（--dry-run，未写盘）")
            return 0

        print("[4/4] 编码 VP9 + Alpha")
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, name + ".webm")
        encode(laid, dst, crf)
        size = os.path.getsize(dst)
        print("  -> %s  %.2f MB  %.2f 秒" % (dst, size / 1048576, len(laid) / FPS))

        meta = {"name": name, "frames": len(laid), "fps": FPS, "seconds": round(len(laid) / FPS, 2),
                "scale": round(M["scale"], 5), "offset": M["offset"], "loop": loop_k,
                "feet_y": feet_y, "char_h": char_h, "center_x": center_x, "gain": gain,
                "source": os.path.abspath(src)}
        if "--meta" in sys.argv:      # 默认不写，免得素材目录里多出 .webm.json
            with open(dst + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)

        print("\n完成。接下来：")
        print("  python register_actions.py --pet-dir %s" % os.path.dirname(out_dir))
        print("  生效：刷新 DSH 页面 / 桌面窗口右键 →「重载配置」")
        return 0
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print("\n中间帧保留在：", tmp)


if __name__ == "__main__":
    sys.exit(main())
