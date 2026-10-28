# BackdropAI V3 — direct BGRA streaming to FFmpeg

## What changed
- No temporary PNG frame directory. Each processed frame is piped directly as BGRA into FFmpeg's ProRes 4444 encoder, alongside the BGR preview encoder.
- `FRAME_PROGRESS` reports frames processed while the stream is being generated; `server.py` maps that to browser progress from 65% to 74%.
- If Transparent output is unchecked, the ProRes encoder is skipped.
- Retains V2 cleanup logic, settings and original-source preservation; **does not yet eliminate the intermediate green/alpha MP4s created by RVM**.
- Transparent WebM is still encoded after ProRes, as before. ProRes may require time for final flushing and WebM encoding still adds time.

## Installation (Windows)
1. Let any currently running jobs finish or stop them safely. Never overwrite active processing scripts.
2. Back up existing `server.py`, `nexacx_edge_cleanup.py` and `backdrop-ai` folder.
3. Copy this package's `server.py`, `nexacx_edge_cleanup.py` and the `backdrop-ai` folder to `C:\\Users\\suren\\RobustVideoMatting`, preserving subfolders.
4. In that folder: `venv\\Scripts\\activate` then `python server.py`.
5. Refresh `http://localhost:5050/` with Ctrl+Shift+R and use a short 5-second input first.

## Expected console output
`FRAME_PROGRESS 15/150 10.0`
`FRAME_PROGRESS 30/150 20.0`
The website should update Despill progress after every ~15 frames.

## Verification
`ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pix_fmt,nb_frames -of default=noprint_wrappers=1 outputs\\<jobid>\\transparent_despill.mov`
Look for `codec_name=prores` and a `yuva...` pixel format. Test over contrasting backgrounds; file size or pixel format alone cannot prove good matting quality.

## Cautions
- Direct encoding avoids PNG disk I/O but can still be CPU-bound or block while FFmpeg encodes; it is not guaranteed to be faster on every computer.
- Two independent FFmpeg encoders write simultaneously; disk/CPU throughput still matters.
- RVM-to-MP4 intermediates remain lossy. Improving their quality is a separate change.
- This patch was smoke-tested using eight synthetic frames; not validated against your original tutor video yet.
