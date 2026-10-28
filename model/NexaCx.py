#!/usr/bin/env python3
"""Nexa CX alpha compositing with green-screen unmatting (OpenCV/NumPy).

Input FG must be an image sequence or video where a subject has already been
composited onto a solid RGB background, and alpha is a frame-aligned grayscale
mask. This script removes that *known compositing background* before placing the
subject on a new background; just using alphamerge on FG may cause a green halo.

Example:
  python nexacx_edge_cleanup.py --foreground fg.mp4 --alpha alpha.mp4 \
    --background nexacx_matte_test/studio_background.png \
    --output NexaCX_Edge_Cleaned_Preview.mp4 \
    --transparent NexaCX_Edge_Cleaned_Transparent.mov

Requires Python, numpy, opencv-python and FFmpeg. Video output is video-only;
original narration should be mapped from source in production after alignment.
"""
import argparse, subprocess, pathlib, tempfile
import cv2
import numpy as np

def snap_fg_bg(frame, mask, threshold=0.008):
    low = (mask < threshold)
    if low.sum() < 100:
        raise ValueError('Insufficient clear background pixels for sampling.')
    return np.median(frame[low], axis=0).astype(np.float32)

def load_bg(path, shape):
    bg = cv2.imread(str(path))
    if bg is None: raise ValueError('Provide a background image (png or jpg).')
    h, w=shape[:2]
    scale=max(w/bg.shape[1],h/bg.shape[0]); nh,nw=round(bg.shape[0]*scale),round(bg.shape[1]*scale)
    bg=cv2.resize(bg,(nw,nh),interpolation=cv2.INTER_AREA if scale<1 else cv2.INTER_LINEAR)
    x=max(0,(nw-w)//2); y=max(0,(nh-h)//2)
    return bg[y:y+h,x:x+w].astype(np.float32)

def composite(fg,ma,bg,back_color,refine=0.0):
    a=(cv2.cvtColor(ma,cv2.COLOR_BGR2GRAY)/255.0).astype(np.float32)
    # Recover original foreground RGB from an FG already laid onto a solid BG.
    # In linear-looking byte domain: fg = a * subject + (1-a) * back_color.
    # Single-step composite avoids dividing by small alpha near soft boundaries.
    base = fg.astype(np.float32)-(1-a[...,None])*back_color[None,None,:]
    if refine:
        aa=np.clip((a-refine)/(1-refine),0,1)
        orig=np.clip(base/np.maximum(a[...,None],0.025),0,255)
        out=orig*aa[...,None]+bg*(1-aa[...,None])
        return np.uint8(np.clip(out,0,255)+0.5), orig, aa
    return np.uint8(np.clip(base+bg*(1-a[...,None]),0,255)+0.5), None, a

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--foreground',required=True)
    ap.add_argument('--alpha',required=True)
    ap.add_argument('--background',required=True)
    ap.add_argument('--output',default='NexaCX_Edge_Cleaned_Preview.mp4')
    ap.add_argument('--transparent',default=None)
    ap.add_argument('--refine',type=float,default=0.012,help='mild alpha threshold, 0 to disable')
    args=ap.parse_args()
    f=cv2.VideoCapture(args.foreground);m=cv2.VideoCapture(args.alpha)
    assert f.isOpened() and m.isOpened(), 'Could not read input video(s)'
    w,h=int(f.get(cv2.CAP_PROP_FRAME_WIDTH)),int(f.get(cv2.CAP_PROP_FRAME_HEIGHT))
    assert (w,h)==(int(m.get(cv2.CAP_PROP_FRAME_WIDTH)),int(m.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    fps=f.get(cv2.CAP_PROP_FPS)
    assert abs(fps-m.get(cv2.CAP_PROP_FPS)) < .001
    # losslessly feed raw frames to ffmpeg; image sequence PNGs for transparent master.
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-y', '-f','rawvideo','-pix_fmt','bgr24','-s',f'{w}x{h}','-r',str(fps),'-i','pipe:0','-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p',args.output]
    writer=subprocess.Popen(cmd,stdin=subprocess.PIPE)
    tmp = tempfile.TemporaryDirectory(prefix='nexacx_edge_alpha_') if args.transparent else None
    bg=None;back_color=None;frames=0
    while True:
        ok,fg=f.read();okm,am=m.read()
        if not ok or not okm:break
        a=(cv2.cvtColor(am,cv2.COLOR_BGR2GRAY)/255.0).astype(np.float32)
        if bg is None:
            bg=load_bg(args.background,fg.shape)
            back_color=snap_fg_bg(fg,a)
            print('Detected original green background (BGR):',back_color)
        out,subject,alpha=composite(fg,am,bg,back_color,refine=args.refine)
        writer.stdin.write(out.tobytes())
        if args.transparent:
            # Straight RGBA into PNGs, then lossless alpha-capable MOV with FFmpeg.
            if subject is None:
                base=fg.astype(np.float32)-(1-a[...,None])*back_color
                subject=np.clip(base/np.maximum(a[...,None],0.025),0,255)
            rgba=np.uint8(np.clip(np.dstack([subject,alpha*255]),0,255)+0.5)
            cv2.imwrite(f'{tmp.name}/frame_{frames:05d}.png',rgba)
        frames+=1
    writer.stdin.close();return_code=writer.wait();f.release();m.release()
    if return_code:raise RuntimeError('FFmpeg failed')
    if args.transparent:
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-framerate',str(fps),'-i',f'{tmp.name}/frame_%05d.png','-c:v','prores_ks','-profile:v','4','-pix_fmt','yuva444p10le',args.transparent],check=True)
    if tmp: tmp.cleanup()
    print('Processed',frames,'frames, output:',args.output)

if __name__=='__main__':main()