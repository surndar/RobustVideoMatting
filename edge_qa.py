#!/usr/bin/env python3
"""BackdropAI edge QA. Usage: python edge_qa.py transparent_despill.webm [more.webm ...]
Needs ffmpeg/ffprobe on PATH, plus numpy and opencv-python (already used by your project).
Prints edge colour / width / speck numbers to paste back."""
import sys, os, json, subprocess
import numpy as np, cv2

def probe(p):
    o = subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
        '-show_entries','stream=width,height,r_frame_rate:format=duration,size,bit_rate',
        '-of','json',p])
    d = json.loads(o); s = d['streams'][0]
    a, b = s['r_frame_rate'].split('/')
    return int(s['width']), int(s['height']), float(a)/float(b or 1), d['format']

def frames(p, w, h, step):
    # libvpx-vp9 decoder is required to keep the alpha channel.
    # Newer ffmpeg uses -fps_mode, older uses -vsync: try both.
    for opt in (['-fps_mode', 'passthrough'], ['-vsync', '0']):
        cmd = ['ffmpeg','-v','error','-c:v','libvpx-vp9','-i',p,
               '-vf',f'select=not(mod(n\\,{step}))', *opt,
               '-f','rawvideo','-pix_fmt','rgba','-']
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        n = w*h*4
        got = 0
        while True:
            b = pr.stdout.read(n)
            if len(b) < n: break
            got += 1
            yield np.frombuffer(b, np.uint8).reshape(h, w, 4)
        pr.stdout.close(); pr.wait()
        if got:
            return

def analyse(p, max_frames=12):
    w, h, fps, fmt = probe(p)
    dur = float(fmt.get('duration') or 0)
    total = max(1, int(dur*fps))
    step = max(1, total//max_frames)
    faint, widths, specks, flick = [], [], [], []
    prev = None; nfr = 0
    for fr in frames(p, w, h, step):
        nfr += 1
        a = fr[:,:,3].astype(np.float32)/255.0
        rgb = fr[:,:,:3].astype(np.float32)
        m = (a > 0.02) & (a < 0.30)
        if m.sum() > 50:
            faint.append(rgb[m].mean(axis=0))
        # soft-edge width: soft pixels per unit of boundary length
        soft = ((a > 0.05) & (a < 0.95)).astype(np.uint8)
        solid = (a >= 0.95).astype(np.uint8)
        er = solid - cv2.erode(solid, np.ones((3,3), np.uint8))
        perim = int(er.sum())
        if perim > 50:
            widths.append(soft.sum()/perim)
        # specks: tiny components well away from the main body
        nlab, lab, st, _ = cv2.connectedComponentsWithStats((a > 0.05).astype(np.uint8), connectivity=8)
        if nlab > 1:
            ar = st[1:, cv2.CC_STAT_AREA]
            specks.append(int(((ar <= 10)).sum()))
        if prev is not None:
            flick.append(float(np.abs(a-prev)[soft.astype(bool)].mean()) if soft.any() else 0)
        prev = a
    f = np.mean(faint, axis=0) if faint else None
    return dict(file=os.path.basename(p), size_mb=round(int(fmt['size'])/1e6,1),
        mbps=round(int(fmt.get('bit_rate') or 0)/1e6,2), res=f'{w}x{h}', fps=round(fps,2),
        frames_checked=nfr,
        faint_edge_rgb=None if f is None else [int(round(x)) for x in f],
        soft_edge_width_px=round(float(np.median(widths)),1) if widths else None,
        tiny_specks_per_frame=round(float(np.mean(specks)),1) if specks else 0)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for p in sys.argv[1:]:
        r = analyse(p)
        print(json.dumps(r))
        rgb = r['faint_edge_rgb']
        if rgb:
            bright = min(rgb) > 150
            print('  ->', 'HALO: faint edge pixels are bright (backdrop colour left in edges)' if bright
                  else 'OK: faint edge pixels are dark/person-coloured')
