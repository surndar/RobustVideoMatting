#!/usr/bin/env python3
"""nexacx edge cleanup v2.
Inputs : RVM foreground video + alpha video (same arguments as before).
Outputs: composite MP4 on a background image + transparent ProRes 4444 MOV.

What it does to every frame:
  1. Alpha cleanup   - tiny floating specks removed, optional light refine.
  2. Spill removal   - green spill reduced (strength --despill).
  3. Edge colours    - semi-transparent edge pixels get their colour from the
                       solid person pixels next to them, so no purple / blue /
                       green halo is left (strength --decontam).
Fully opaque pixels (inside the person) are left as they are."""
import argparse, json, subprocess, sys
import cv2
import numpy as np


def probe(path):
    out = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,r_frame_rate,nb_frames,duration', '-of', 'json', path])
    s = json.loads(out)['streams'][0]
    a, b = s['r_frame_rate'].split('/')
    fps = float(a) / float(b or 1)
    try:
        total = int(s.get('nb_frames') or 0)
    except ValueError:
        total = 0
    if not total:
        try:
            total = int(float(s.get('duration') or 0) * fps)
        except ValueError:
            total = 0
    return int(s['width']), int(s['height']), fps, total


def open_reader(path, w, h):
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', path,
           '-vf', f'scale={w}:{h}', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    return subprocess.Popen(cmd, stdout=subprocess.PIPE)


def read_frame(proc, w, h):
    buf = proc.stdout.read(w * h * 3)
    if len(buf) < w * h * 3:
        return None
    return np.frombuffer(buf, np.uint8).reshape(h, w, 3)


def open_writer(path, w, h, fps, rgba):
    pix = 'rgba' if rgba else 'rgb24'
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
           '-pix_fmt', pix, '-s', f'{w}x{h}', '-r', f'{fps:.4f}', '-i', '-']
    if rgba:
        cmd += ['-c:v', 'prores_ks', '-profile:v', '4444', '-pix_fmt', 'yuva444p10le', path]
    else:
        cmd += ['-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-c:v', 'libx264',
                '-crf', '14', '-preset', 'fast', '-pix_fmt', 'yuv420p', path]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def load_background(path, w, h):
    img = cv2.imread(path) if path else None
    if img is None:
        print(f'Background not found ({path}); using dark gray.', flush=True)
        return np.full((h, w, 3), 30, np.float32)
    ih, iw = img.shape[:2]
    scale = max(w / iw, h / ih)
    img = cv2.resize(img, (int(iw * scale) + 1, int(ih * scale) + 1))
    y, x = (img.shape[0] - h) // 2, (img.shape[1] - w) // 2
    return cv2.cvtColor(img[y:y + h, x:x + w], cv2.COLOR_BGR2RGB).astype(np.float32)


# ---------------------------------------------------------------- core
def clean_alpha(al, refine, min_island):
    """al: float32 0..1. Removes tiny floating specks and applies a light refine."""
    if refine > 0:
        r = min(refine, 0.45)
        al = np.clip((al - r) / (1 - 2 * r), 0, 1)
    if min_island > 0:
        solid = (al > 0.08).astype(np.uint8)
        n, lab, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
        if n > 2:
            areas = stats[1:, cv2.CC_STAT_AREA]
            limit = max(40, int(areas.max() * min_island))
            keep = np.zeros(n, np.uint8)
            keep[1:] = (areas >= limit).astype(np.uint8)
            mask = keep[lab]
            mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))   # keep soft borders
            al = al * mask
    return al


def remove_spill(f, strength, edge_mask=None):
    if strength <= 0:
        return f
    limit = (f[:, :, 0] + f[:, :, 2]) / 2.0
    g = f[:, :, 1]
    k = strength if edge_mask is None else strength * edge_mask
    f[:, :, 1] = g + (np.minimum(g, limit) - g) * k
    return f


def _smooth_est(f, core, sigma, d):
    """Blurred colour of the solid pixels, computed at 1/d size for speed."""
    h, w = core.shape
    sw, sh = max(1, w // d), max(1, h // d)
    c = cv2.resize(core, (sw, sh), interpolation=cv2.INTER_AREA)
    fc = cv2.resize(f * core[:, :, None], (sw, sh), interpolation=cv2.INTER_AREA)
    s = max(0.8, sigma / d)
    wgt = cv2.GaussianBlur(c, (0, 0), s)
    num = cv2.GaussianBlur(fc, (0, 0), s)
    est = num / np.maximum(wgt, 1e-4)[:, :, None]
    return (cv2.resize(est, (w, h), interpolation=cv2.INTER_LINEAR),
            cv2.resize(wgt, (w, h), interpolation=cv2.INTER_LINEAR))


def edge_zone(al, width):
    """~1 around soft (semi-transparent) edges, fading to 0 away from them."""
    soft = ((al > 0.02) & (al < 0.995)).astype(np.uint8)
    k = max(3, int(width) | 1)
    z = cv2.dilate(soft, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return cv2.GaussianBlur(z.astype(np.float32), (0, 0), 1.5)


def hue_cast_weight(f, al, width):
    """How strongly a pixel looks green/teal (G and B above R) near an edge.
    Natural skin and hair have R >= G >= B, so they score 0. Frames are RGB."""
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    cast = np.maximum(np.minimum(g, b) - r, g - np.maximum(r, b))
    return np.clip(cast / 8.0, 0, 1) * edge_zone(al, width)


def decontaminate(f, al, strength, radius):
    """Replace edge colours with colours from nearby solid person pixels."""
    if strength <= 0:
        return f
    core = (al > 0.92).astype(np.float32)
    if core.max() == 0:
        return f
    sig = max(2.0, radius * 0.6)
    est1, w1 = _smooth_est(f, core, sig, 2)
    est2, w2 = _smooth_est(f, core, sig * 3, 4)             # wider fallback
    t1 = np.clip(w1 / 0.15, 0, 1)[:, :, None]
    est = est1 * t1 + est2 * (1 - t1)
    valid = np.clip(w2 / 0.02, 0, 1)
    # the more transparent a pixel, the more of its colour is replaced
    t = strength * np.clip((0.995 - al) / 0.4, 0, 1) * valid
    # also replace edge pixels whose colour is clearly off (green/teal), even if
    # they are almost opaque
    t = np.maximum(t, strength * hue_cast_weight(f, al, radius) * valid)
    t = t[:, :, None]
    return f * (1 - t) + est * t


def temporal_smooth(f, al, state, strength, thr, width, ref):
    """Calm edge flicker by blending soft-edge pixels with the previous output frame.
    Motion is judged on the original footage (ref), dilated widely, so anything that
    really moves (hands, head turns, motion blur) is left completely untouched."""
    cur = cv2.GaussianBlur(ref, (0, 0), 2)
    if state is None:
        return f, al, (f, al, cur)
    pf, pa, pcur = state
    motion = np.abs(cur - pcur).mean(axis=2)
    motion = cv2.dilate(motion, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
    zone = np.maximum(edge_zone(al, width), edge_zone(pa, width))
    w = strength * np.clip(1.0 - motion / thr, 0, 1) * zone
    f2 = f * (1 - w[:, :, None]) + pf * w[:, :, None]
    al2 = al * (1 - w) + pa * w
    return f2, al2, (f2, al2, cur)


def cast_cleanup(f, al, width, strength):
    """Neutralise green/teal tint on dark pixels (hair) near edges. Natural hair and
    skin have R >= G >= B, so only a real green/teal cast is touched."""
    if strength <= 0:
        return f
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    cast = np.maximum(np.minimum(g, b) - r, g - np.maximum(r, b))
    v = f.max(axis=2) / 255.0
    dark = np.clip((0.6 - v) / 0.25, 0, 1)
    w = np.clip((cast - 2.0) / 6.0, 0, 1) * dark * edge_zone(al, max(15, 2 * int(width))) * strength
    lum = (0.299 * r + 0.587 * g + 0.114 * b)[:, :, None]
    return f * (1 - w[:, :, None]) + lum * w[:, :, None]


def guided_alpha(al, guide, strength, width, radius=4, eps=2e-3):
    """Snap soft alpha edges to the real picture edges (guided filter, gray guide)."""
    if strength <= 0:
        return al
    g = (guide[:, :, 0] * 0.299 + guide[:, :, 1] * 0.587 + guide[:, :, 2] * 0.114) / 255.0
    g = g.astype(np.float32)
    k = (2 * radius + 1, 2 * radius + 1)
    box = lambda x: cv2.boxFilter(x, -1, k)
    mg, ma = box(g), box(al)
    cov = box(g * al) - mg * ma
    var = box(g * g) - mg * mg
    aa = cov / (var + eps)
    bb = ma - aa * mg
    out = np.clip(box(aa) * g + box(bb), 0, 1)
    z = (edge_zone(al, width) * strength)
    return al * (1 - z) + out * z


def process(f, al, a):
    """f: float32 HxWx3 (0..255), al: float32 HxW (0..1). Returns f, al."""
    al = clean_alpha(al, a.refine, a.min_island)
    mask = None
    if a.despill_edge_only:
        k = max(1, a.edge_width)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        band = (cv2.dilate(al, kernel) - cv2.erode(al, kernel)) > 0.02
        mask = cv2.GaussianBlur(band.astype(np.float32), (0, 0), 1.5)
    f = remove_spill(f, min(max(a.despill, 0.0), 1.0), mask)
    f = decontaminate(f, al, min(max(a.decontam, 0.0), 1.0), max(2, a.edge_width))
    return np.clip(f, 0, 255), al


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--foreground', required=True)
    p.add_argument('--alpha', required=True)
    p.add_argument('--background', default='')
    p.add_argument('--source', default='', help='original footage; keeps its colours where the person is fully solid')
    p.add_argument('--output', default='')
    p.add_argument('--transparent', default='')
    p.add_argument('--despill', type=float, default=1.0)
    p.add_argument('--despill-edge-only', action='store_true')
    p.add_argument('--edge-width', type=int, default=9)
    p.add_argument('--refine', type=float, default=0.012)
    p.add_argument('--decontam', type=float, default=0.9,
                   help='0..1 strength of edge colour clean-up (0 = off)')
    p.add_argument('--temporal', type=float, default=0.6,
                   help='0..1 edge flicker smoothing in still areas (0 = off)')
    p.add_argument('--temporal-motion', type=float, default=20.0,
                   help='motion in the original footage (levels) at which smoothing switches off')
    p.add_argument('--cast', type=float, default=1.0,
                   help='0..1 removes green/teal tint from dark hair near edges (0 = off)')
    p.add_argument('--guided', type=float, default=0.0,
                   help='0..1 snaps soft alpha edges to the real picture edges (0 = off)')
    p.add_argument('--min-island', type=float, default=0.002,
                   help='remove floating specks smaller than this fraction of the person (0 = off)')
    a, unknown = p.parse_known_args()
    if unknown:
        print(f'Warning: ignoring unknown option(s): {" ".join(unknown)}', file=sys.stderr, flush=True)
    if not a.output and not a.transparent:
        p.error('give --output (preview video) and/or --transparent (transparent file)')

    w, h, fps, total = probe(a.foreground)
    bg = load_background(a.background, w, h) if a.output else None
    fr, ar = open_reader(a.foreground, w, h), open_reader(a.alpha, w, h)
    sr = open_reader(a.source, w, h) if a.source else None
    wc = open_writer(a.output, w, h, fps, rgba=False) if a.output else None
    wt = open_writer(a.transparent, w, h, fps, rgba=True) if a.transparent else None
    n = 0
    tstate = None
    while True:
        f, m = read_frame(fr, w, h), read_frame(ar, w, h)
        if f is None or m is None:
            break
        src = read_frame(sr, w, h) if sr else None
        f, al = process(f.astype(np.float32), m[:, :, 0].astype(np.float32) / 255.0, a)
        if src is not None:
            solid = np.clip((al - 0.97) / 0.025, 0, 1)[:, :, None]
            f = f * (1 - solid) + src.astype(np.float32) * solid
        guide = src.astype(np.float32) if src is not None else f
        f = cast_cleanup(f, al, a.edge_width, min(max(a.cast, 0.0), 1.0))
        al = guided_alpha(al, guide, min(max(a.guided, 0.0), 1.0), max(2, a.edge_width))
        if a.temporal > 0:
            f, al, tstate = temporal_smooth(f, al, tstate, min(max(a.temporal, 0.0), 0.9),
                                            max(a.temporal_motion, 1.0), max(2, a.edge_width), guide)
        if wc:
            comp = f * al[:, :, None] + bg * (1 - al[:, :, None])
            wc.stdin.write(comp.astype(np.uint8).tobytes())
        if wt:
            wt.stdin.write(np.dstack([f, al * 255.0]).astype(np.uint8).tobytes())
        n += 1
        if n == 1 or n % 15 == 0:
            pct = min(100.0, 100.0 * n / total) if total else 0.0
            print(f'FRAME_PROGRESS {n}/{total} {pct:.1f}', flush=True)
    writers = [x for x in (wc, wt) if x]
    for pr in writers:
        pr.stdin.close()
        pr.wait()
    fr.stdout.close(); ar.stdout.close()
    if sr:
        sr.stdout.close()
    if n == 0 or any(x.returncode for x in writers):
        print('Despill failed: no frames written or ffmpeg error', file=sys.stderr)
        sys.exit(1)
    print(f'done {n} frames', flush=True)


if __name__ == '__main__':
    main()
