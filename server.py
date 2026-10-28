#!/usr/bin/env python3
"""
BackdropAI — Flask + SocketIO server
Wraps RobustVideoMatting inference and nexacx despill pipeline.
Serves the backdrop-ai web app and exposes REST + WebSocket APIs.

Usage:
    python server.py
Then open http://localhost:5050
"""

import os, sys, json, shutil, subprocess, tempfile, threading, time, uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, abort, send_file
from flask_socketio import SocketIO, emit

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
STATIC    = BASE_DIR / 'backdrop-ai'
UPLOADS   = BASE_DIR / 'uploads'
OUTPUTS   = BASE_DIR / 'outputs'
CHECKPOINT= BASE_DIR / 'rvm_mobilenetv3.pth'

# ── Quality settings ──────────────────────────────────────────────────────────
# Use resnet50 for finer hair detail if rvm_resnet50.pth is next to server.py.
_want_variant    = os.environ.get('BACKDROP_VARIANT', 'resnet50')
# Fall back to the small model if the ResNet-50 weights are not next to server.py.
RVM_VARIANT      = _want_variant if (BASE_DIR / f'rvm_{_want_variant}.pth').exists() else 'mobilenetv3'
RVM_DOWNSAMPLE   = os.environ.get('BACKDROP_DOWNSAMPLE', '')   # e.g. 0.4 for 1080p; '' = auto
RVM_MBPS         = '50'    # RVM intermediate bitrate (was 8: lossy intermediates blur edges)
DECONTAM         = 1.0     # edge colour clean-up strength (script default 0.9)
WEBM_CRF         = '30'    # VP9 quality; ~3 Mbps at 1080p, close to the reference
HOST             = os.environ.get('BACKDROP_HOST', '127.0.0.1')  # set 0.0.0.0 to share on LAN

for d in (UPLOADS, OUTPUTS):
    d.mkdir(exist_ok=True)

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=str(STATIC))
app.config['SECRET_KEY'] = os.environ.get('BACKDROP_SECRET', uuid.uuid4().hex)
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024 * 1024  # 4 GB

socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading',
                    max_http_buffer_size=4 * 1024 * 1024 * 1024)

# ── Job registry ──────────────────────────────────────────────────────────────
jobs: dict = {}   # job_id -> {status, progress, message, outputs}
jobs_lock = threading.Lock()


def update_job(job_id: str, **kwargs):
    with jobs_lock:
        if job_id not in jobs:
            jobs[job_id] = {}
        jobs[job_id].update(kwargs)
    socketio.emit('job_update', {'job_id': job_id, **jobs[job_id]})


# ═══════════════════════════════════════════════════════════════════════════════
# STATIC ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/ping')
def ping():
    return jsonify(ok=True)


@app.route('/api/job/<job_id>')
def job_status(job_id):
    with jobs_lock:
        j = jobs.get(job_id)
        if j is None:
            abort(404)
        hidden = ('input', 'job_dir', 'background')
        return jsonify({k: v for k, v in j.items() if k not in hidden})


@app.route('/api/download/<job_id>/<path:filename>')
def download(job_id, filename):
    folder = OUTPUTS / job_id
    return send_from_directory(str(folder), filename,
                               as_attachment=bool(request.args.get('dl')))


@app.route('/')
def index():
    return send_from_directory(str(STATIC), 'index.html')


@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(str(STATIC), filename)


# ═══════════════════════════════════════════════════════════════════════════════
# UPLOAD INPUT VIDEO
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'video' not in request.files:
        return jsonify(error='No video field'), 400
    f = request.files['video']
    if not f.filename:
        return jsonify(error='Empty filename'), 400

    job_id  = str(uuid.uuid4())[:8]
    job_dir = UPLOADS / job_id
    job_dir.mkdir()

    ext  = Path(f.filename).suffix.lower() or '.mp4'
    dest = job_dir / f'input{ext}'
    f.save(str(dest))

    info = probe_video(str(dest))

    with jobs_lock:
        jobs[job_id] = {
            'status'  : 'uploaded',
            'input'   : str(dest),
            'job_dir' : str(job_dir),
            'info'    : info,
            'progress': 0,
            'message' : 'Upload complete',
            'outputs' : {},
        }

    return jsonify(job_id=job_id, info=info)


def probe_video(path: str) -> dict:
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', path]
    try:
        raw  = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
        data = json.loads(raw)
        for s in data.get('streams', []):
            if s.get('codec_type') == 'video':
                fps_raw = s.get('r_frame_rate', '30/1')
                try:
                    a, b = fps_raw.split('/')
                    fps = float(a) / float(b)
                except Exception:
                    fps = 30.0
                dur = s.get('duration')
                if dur in (None, 'N/A'):
                    dur = data.get('format', {}).get('duration', 0)
                try:
                    dur = float(dur)
                except (TypeError, ValueError):
                    dur = 0.0
                return {
                    'width'   : s.get('width', 0),
                    'height'  : s.get('height', 0),
                    'fps'     : round(fps, 2),
                    'duration': round(dur, 2),
                    'frames'  : int(s.get('nb_frames') or 0),
                }
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# UPLOAD BACKGROUND IMAGE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/upload-bg', methods=['POST'])
def upload_bg():
    if 'bg' not in request.files:
        return jsonify(error='No bg field'), 400
    f = request.files['bg']
    job_id = request.form.get('job_id')
    if not job_id or job_id not in jobs:
        return jsonify(error='Unknown job_id'), 400

    job_dir = Path(jobs[job_id]['job_dir'])
    ext  = Path(f.filename).suffix.lower() or '.png'
    dest = job_dir / f'background{ext}'
    f.save(str(dest))

    with jobs_lock:
        jobs[job_id]['background'] = str(dest)

    return jsonify(ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# RUN PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/run', methods=['POST'])
def run_pipeline():
    body   = request.json or {}
    job_id = body.get('job_id')
    if not job_id or job_id not in jobs:
        return jsonify(error='Unknown job'), 400

    if jobs[job_id].get('status') == 'running':
        return jsonify(error='Job already running'), 409

    params = {
        'despill'          : float(body.get('despill', 0.5)),
        'despill_edge'     : bool(body.get('despill_edge', True)),
        'edge_width'       : int(body.get('edge_width', 7)),
        'refine'           : float(body.get('refine', 0.005)),
        'want_alpha'       : bool(body.get('want_alpha', True)),
        'want_green'       : bool(body.get('want_green', True)),
        'want_transparent' : bool(body.get('want_transparent', True)),
        'want_composite'   : bool(body.get('want_composite', True)),
        'trim_start'       : float(body.get('trim_start', 0)),
        'trim_end'         : float(body.get('trim_end', 0)),
        'device'           : body.get('device', 'cpu'),
    }

    update_job(job_id, status='running', progress=0,
               message='Starting pipeline…', params=params, outputs={})

    t = threading.Thread(target=pipeline_thread, args=(job_id, params), daemon=True)
    t.start()

    return jsonify(ok=True, job_id=job_id)


# ── Full pipeline ─────────────────────────────────────────────────────────────

def pipeline_thread(job_id: str, params: dict):
    job     = jobs[job_id]
    input_v = job['input']
    job_dir = Path(job['job_dir'])
    out_dir = OUTPUTS / job_id
    out_dir.mkdir(exist_ok=True)

    try:
        # Step 0: optional trim
        trim_start = params['trim_start']
        trim_end   = params['trim_end']
        if trim_start or trim_end:
            update_job(job_id, progress=2, message='Trimming video…')
            trimmed = str(job_dir / 'trimmed.mp4')
            ta = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
            if trim_start:
                ta += ['-ss', str(trim_start)]
            ta += ['-i', input_v]
            if trim_end:
                # -t is a duration; -to after -i would count from the new start
                length = trim_end - trim_start
                if length <= 0:
                    raise ValueError('Trim end must be after trim start')
                ta += ['-t', str(length)]
            ta += ['-c:v', 'libx264', '-crf', '15', '-preset', 'fast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', trimmed]
            subprocess.run(ta, check=True)
            input_v = trimmed

        # Step 1: RVM inference → alpha.mp4 + green.mp4
        alpha_mp4 = str(job_dir / 'alpha.mp4')
        fgr_mp4   = str(job_dir / 'green.mp4')

        ckpt = BASE_DIR / f'rvm_{RVM_VARIANT}.pth'
        if not ckpt.exists():
            raise RuntimeError(f'Checkpoint not found: {ckpt.name}')
        if not run_rvm(input_v, alpha_mp4, fgr_mp4,
                       str(ckpt), params['device'], job_id):
            raise RuntimeError(f'RVM inference failed — check that {ckpt.name} is present')

        # Step 2: nexacx despill + composite → preview_despill.mp4 + transparent_despill.mov
        background   = job.get('background', str(BASE_DIR / 'studio_background.png'))
        preview_mp4  = str(out_dir / 'preview_despill.mp4')
        trans_mov    = str(out_dir / 'transparent_despill.mov')

        if params['want_composite'] or params['want_transparent']:
            update_job(job_id, progress=65, message='Applying despill & compositing…')
            run_nexacx(fgr_mp4, alpha_mp4, background,
                       preview_mp4 if params['want_composite'] else None,
                       trans_mov if params['want_transparent'] else None,
                       params['despill'], params['despill_edge'],
                       params['edge_width'], params['refine'], job_id, source=input_v)

        # Step 3: encode final outputs
        outputs = {}

        if params['want_alpha']:
            update_job(job_id, progress=75, message='Encoding alpha matte…')
            dst = str(out_dir / 'alpha.mp4')
            enc_h264(alpha_mp4, dst)
            outputs['alpha'] = f'/api/download/{job_id}/alpha.mp4'

        if params['want_green']:
            update_job(job_id, progress=80, message='Encoding green screen…')
            dst = str(out_dir / 'green.mp4')
            enc_h264(fgr_mp4, dst)
            outputs['green'] = f'/api/download/{job_id}/green.mp4'

        if params['want_composite']:
            update_job(job_id, progress=83, message='Finalising composite…')
            outputs['preview'] = f'/api/download/{job_id}/preview_despill.mp4'

        if params['want_transparent']:
            update_job(job_id, progress=87, message='Encoding transparent WebM (VP9)…')
            webm = str(out_dir / 'transparent_despill.webm')
            enc_webm_alpha(trans_mov, webm, audio_src=input_v)
            outputs['transparent'] = f'/api/download/{job_id}/transparent_despill.webm'
            update_job(job_id, progress=95, message='Checking output quality…')
            checks = quality_check(webm, input_v)
        else:
            checks = []

        done_msg = 'Pipeline complete!'
        if checks:
            done_msg += ' Check: ' + ' | '.join(checks)
        update_job(job_id, status='done', progress=100,
                   message=done_msg, outputs=outputs, checks=checks)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        update_job(job_id, status='error', message=str(e), traceback=tb)


# ── RVM subprocess ────────────────────────────────────────────────────────────

def run_rvm(input_v, alpha_out, fgr_out, checkpoint, device, job_id) -> bool:
    py  = str(Path(sys.executable))
    cmd = [
        py, str(BASE_DIR / 'inference.py'),
        '--variant', RVM_VARIANT,
        '--checkpoint', checkpoint,
        '--device', device,
        '--input-source', input_v,
        '--output-type', 'video',
        '--output-alpha', alpha_out,
        '--output-foreground', fgr_out,
        '--output-video-mbps', RVM_MBPS,
        '--seq-chunk', '1',
    ]
    if RVM_DOWNSAMPLE:
        cmd += ['--downsample-ratio', RVM_DOWNSAMPLE]
    update_job(job_id, progress=8, message=f'Running RVM inference ({RVM_VARIANT})…')

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(BASE_DIR),
    )
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        # tqdm: "  5%|██  | 25/500 [00:12…]"
        if '|' in line and '%' in line:
            try:
                pct_s = line.strip().split('%')[0]
                pct   = int(pct_s.strip().split()[-1])
                mapped = 8 + int(pct * 0.54)
                update_job(job_id, progress=mapped, message=f'RVM: {pct}% complete')
            except Exception:
                update_job(job_id, message=f'RVM: {line[:100]}')
        else:
            update_job(job_id, message=f'RVM: {line[:100]}')

    proc.wait()
    return proc.returncode == 0


# ── nexacx despill ────────────────────────────────────────────────────────────

def run_nexacx(foreground, alpha, background, output, transparent,
               despill, despill_edge_only, edge_width, refine, job_id, source=None):
    py  = str(Path(sys.executable))
    cmd = [
        py, str(BASE_DIR / 'nexacx_edge_cleanup.py'),
        '--foreground', foreground,
        *(['--source', source] if source else []),
        '--alpha', alpha,
        '--background', background,
        *(['--output', output] if output else []),
        *(['--transparent', transparent] if transparent else []),
        '--decontam', str(DECONTAM),
        '--despill', str(despill),
        '--edge-width', str(edge_width),
        '--refine', str(refine),
    ]
    if despill_edge_only:
        cmd.append('--despill-edge-only')

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=str(BASE_DIR),
    )
    for line in proc.stdout:
        line = line.strip()
        if line.startswith('FRAME_PROGRESS '):
            try:
                pct = float(line.split()[-1])   # 'FRAME_PROGRESS n/total pct'
                update_job(job_id, progress=65 + min(9, int(pct * 0.09)),
                           message=f'Despill: {line.split()[1]} frames ({pct:.1f}%)')
            except (ValueError, IndexError):
                pass
        elif line:
            update_job(job_id, message=f'Despill: {line[:120]}')
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'nexacx_edge_cleanup.py failed (code {proc.returncode})')



# ── Quality check of the finished transparent WebM ───────────────────────────
def _duration(path) -> float:
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', path])
        return float(out.strip())
    except Exception:
        return 0.0


def quality_check(webm_path: str, source_path: str) -> list:
    """Return a list of plain-language notes. Never raises; empty list = all fine."""
    notes = []
    try:
        p = Path(webm_path)
        if not p.exists() or p.stat().st_size < 2_000:
            return ['WebM file is missing or empty']

        # 1. duration should match the source
        d_out, d_src = _duration(webm_path), _duration(source_path)
        if d_src and abs(d_out - d_src) > 0.6:
            notes.append(f'Duration differs: output {d_out:.1f}s vs input {d_src:.1f}s')

        # 2. decode one frame from the middle with the VP9 decoder (it keeps alpha)
        import numpy as np
        info = json.loads(subprocess.check_output(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'json', webm_path]))['streams'][0]
        w, h = int(info['width']), int(info['height'])
        raw = subprocess.check_output(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-c:v', 'libvpx-vp9',
             '-ss', f'{max(d_out / 2, 0):.2f}', '-i', webm_path, '-frames:v', '1',
             '-pix_fmt', 'rgba', '-f', 'rawvideo', '-'])
        if len(raw) < w * h * 4:
            return notes + ['Could not read a frame to check transparency']
        a = np.frombuffer(raw[:w * h * 4], np.uint8).reshape(h, w, 4)[:, :, 3]
        clear, solid = float((a < 10).mean()), float((a > 245).mean())
        if clear < 0.02:
            notes.append('No transparent area found: the background may not have been removed')
        if solid < 0.005:
            notes.append('Almost nothing is opaque: the person may be missing from the output')
        if clear + solid > 0.999:
            notes.append('Edges are completely hard (no soft pixels): hair detail may be lost')
    except Exception as e:  # a failed check must never fail the job
        notes.append(f'Quality check could not run: {e}')
    return notes


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def enc_h264(src, dst, crf=10):
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
         '-i', src, '-c:v', 'libx264', '-crf', str(crf), '-preset', 'fast',
         '-pix_fmt', 'yuv420p', dst],
        check=True,
    )


def has_audio(path) -> bool:
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', path])
        return bool(out.strip())
    except Exception:
        return False


def enc_webm_alpha(src, dst, audio_src=None):
    """ProRes 4444 .mov → VP9 WebM with alpha channel (+ Opus audio if present)."""
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', src]
    with_audio = bool(audio_src) and has_audio(audio_src)
    if with_audio:
        cmd += ['-i', audio_src, '-map', '0:v:0', '-map', '1:a:0',
                '-c:a', 'libopus', '-b:a', '128k', '-shortest']
    cmd += ['-c:v', 'libvpx-vp9',
            '-pix_fmt', 'yuva420p',
            '-crf', WEBM_CRF, '-b:v', '0',
            '-row-mt', '1',
            '-auto-alt-ref', '0',
            dst]
    subprocess.run(cmd, check=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SOCKET.IO
# ═══════════════════════════════════════════════════════════════════════════════

@socketio.on('connect')
def on_connect():
    emit('connected', {'msg': 'BackdropAI server ready'})


@socketio.on('subscribe')
def on_subscribe(data):
    job_id = data.get('job_id')
    if job_id and job_id in jobs:
        emit('job_update', {'job_id': job_id, **jobs[job_id]})


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print('\n' + '─' * 62)
    print('  BackdropAI Server  ·  http://localhost:5050')
    print('─' * 62)
    print(f'  Model      : {RVM_VARIANT}  ({BASE_DIR / ("rvm_" + RVM_VARIANT + ".pth")})')
    print(f'  Uploads    : {UPLOADS}')
    print(f'  Outputs    : {OUTPUTS}')
    print('─' * 62 + '\n')
    socketio.run(app, host=HOST, port=5050, debug=False,
                 allow_unsafe_werkzeug=True)
