# BackdropAI edge-cleanup patch (experimental v2)

## Installation on Windows
Back up your existing `server.py`, `nexacx_edge_cleanup.py`, and the three `backdrop-ai` front-end files. Copy these patched files into the equivalent paths in your RobustVideoMatting folder (do not replace `inference.py`, model weights, `uploads`, or `outputs`). Restart `python server.py` and hard refresh Chrome (`Ctrl+Shift+R`).

## Changes
- Unmatting floor changed from 0.025 to 0.15 to limit amplified compression noise in near-transparent pixels.
- Matte-background color sampled per frame rather than only on first frame.
- Original input RGB preserved in nearly opaque foreground (>0.97 alpha) when available; soft edges still use RVM's green foreground.
- Default despill reduced to 0.5 and restricted to edge zone; default width 7.
- Suppress residual alpha below 0.012 to avoid tiny colored stray pixels.
- Maintain ProRes 4444 MOV intermediate and WebM via existing server.

## Local 5-second test with already-generated green.mp4 and alpha.mp4
Activate venv, then run:

    python nexacx_edge_cleanup.py --foreground green5.mp4 --alpha alpha5.mp4 --background studio_background.png --output preview_v2.mp4 --transparent transparent_v2.mov --despill 0.5 --despill-edge-only --edge-width 7 --refine 0.005

Optional: add `--source original5.mp4` if original5 is frame- and time-aligned with green5/alpha5. Full BackdropAI pipeline supplies its input automatically.

## Caveats
- Not yet tested on the user's actual green/alpha clips. Results may improve or worsen depending on the footage. Compare matching frames against baseline on white, black and saturated color.
- Green preview and alpha MP4 are both lossy intermediate formats. More significant improvements require adapting `inference.py` to output lossless foreground and alpha, ideally direct RGBA.
- This is not a temporal matting algorithm; flicker from the source alpha may remain.
- Existing job pipeline always performs the cleanup and ProRes render even when outputs are unchecked, which may waste time/disk space.
