/* BackdropAI RVM Studio - front end (no external libraries needed) */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const S = { jobId: null, poll: null, lastMsg: '' };

  /* ---------- helpers ---------- */
  function toast(msg, type) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast show ' + (type || '');
    clearTimeout(toast.t);
    toast.t = setTimeout(() => (t.className = 'toast'), 4000);
  }
  function setCardsEnabled(on) {
    ['cardBg', 'cardDespill', 'cardOutputs'].forEach((id) =>
      $(id).classList.toggle('disabled-card', !on));
    $('runBtn').disabled = !on;
  }

  /* ---------- server status (simple ping, no socket.io) ---------- */
  async function ping() {
    try {
      const r = await fetch('/api/ping', { cache: 'no-store' });
      if (!r.ok) throw new Error();
      $('statusDot').className = 'status-dot on';
      $('statusText').textContent = 'Server connected';
    } catch (e) {
      $('statusDot').className = 'status-dot off';
      $('statusText').textContent = 'Server offline';
    }
  }
  ping();
  setInterval(ping, 5000);

  /* ---------- hero start ---------- */
  $('heroStartBtn').addEventListener('click', () => {
    $('heroSection').style.display = 'none';
    $('appMain').style.display = 'grid';
    window.scrollTo(0, 0);
  });

  /* ---------- upload video ---------- */
  $('chooseVideoBtn').addEventListener('click', (e) => { e.stopPropagation(); $('fileInput').click(); });
  $('fileInput').addEventListener('change', (e) => { if (e.target.files[0]) uploadVideo(e.target.files[0]); });
  const dz = $('dropZone');
  dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
  dz.addEventListener('drop', (e) => {
    e.preventDefault();
    dz.classList.remove('dragover');
    const f = e.dataTransfer.files[0];
    if (f && f.type.startsWith('video/')) uploadVideo(f);
    else toast('Please drop a video file', 'error');
  });

  function uploadVideo(file) {
    const fd = new FormData();
    fd.append('video', file);
    toast('Uploading video…');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/upload');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) $('statusText').textContent = 'Uploading ' + Math.round((e.loaded / e.total) * 100) + '%';
    };
    xhr.onload = () => {
      ping();
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status !== 200 || !d.job_id) return toast(d.error || 'Upload failed', 'error');
      S.jobId = d.job_id;
      const i = d.info || {};
      $('viName').textContent = file.name;
      $('viRes').textContent = i.width ? i.width + ' × ' + i.height : '—';
      $('viDur').textContent = i.duration ? i.duration + ' s' : '—';
      $('viFps').textContent = i.fps || '—';
      dz.style.display = 'none';
      $('videoInfo').style.display = 'block';
      setCardsEnabled(true);
      toast('Video uploaded. Set options and click Run Pipeline.', 'success');
    };
    xhr.onerror = () => toast('Upload failed. Is the server running?', 'error');
    xhr.send(fd);
  }

  $('changeVideoBtn').addEventListener('click', () => {
    clearInterval(S.poll);
    S.jobId = null;
    $('fileInput').value = '';
    dz.style.display = '';
    $('videoInfo').style.display = 'none';
    setCardsEnabled(false);
    $('previewArea').style.display = 'none';
    $('progressPanel').style.display = 'none';
    $('emptyState').style.display = '';
  });

  /* ---------- background image ---------- */
  $('chooseBgBtn').addEventListener('click', () => $('bgInput').click());
  $('bgInput').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    if (!f || !S.jobId) return;
    const fd = new FormData();
    fd.append('bg', f);
    fd.append('job_id', S.jobId);
    try {
      const r = await fetch('/api/upload-bg', { method: 'POST', body: fd });
      if (!r.ok) throw new Error();
      $('bgThumb').src = URL.createObjectURL(f);
      $('bgThumb').style.display = 'block';
      $('bgPlaceholder').style.display = 'none';
      toast('Background uploaded', 'success');
    } catch (err) { toast('Background upload failed', 'error'); }
  });

  /* ---------- settings ---------- */
  const el = { despill: $('despillSlider'), edgeW: $('edgeWidthSlider'), refine: $('refineSlider'), edgeOnly: $('edgeOnlyCheck') };
  function syncLabels() {
    $('despillVal').textContent = (el.despill.value / 100).toFixed(2);
    $('edgeWidthVal').textContent = el.edgeW.value;
    $('refineVal').textContent = (el.refine.value / 100).toFixed(3);
    $('edgeWidthRow').style.opacity = el.edgeOnly.checked ? 1 : 0.5;
  }
  [el.despill, el.edgeW, el.refine, el.edgeOnly].forEach((x) => x.addEventListener('input', syncLabels));
  syncLabels();

  const PRESETS = {
    'pale': { d: 50, eo: false, w: 9, r: 1 },
    'green-clothing': { d: 100, eo: true, w: 9, r: 1 },
    'edge-outline': { d: 100, eo: true, w: 15, r: 1 },
    'default': { d: 50, eo: true, w: 7, r: 0 },
  };
  document.querySelectorAll('.hint-apply').forEach((b) =>
    b.addEventListener('click', () => {
      const p = PRESETS[b.dataset.action];
      if (!p) return;
      el.despill.value = p.d; el.edgeOnly.checked = p.eo; el.edgeW.value = p.w; el.refine.value = p.r;
      syncLabels();
      toast('Settings applied', 'success');
    }));

  /* ---------- run pipeline ---------- */
  $('runBtn').addEventListener('click', async () => {
    if (!S.jobId) return toast('Upload a video first', 'error');
    const body = {
      job_id: S.jobId,
      despill: el.despill.value / 100,
      despill_edge: el.edgeOnly.checked,
      edge_width: +el.edgeW.value,
      refine: el.refine.value / 100,
      want_alpha: $('chkAlpha').checked,
      want_green: $('chkGreen').checked,
      want_transparent: $('chkTrans').checked,
      want_composite: $('chkComposite').checked,
      device: $('deviceSelect').value,
    };
    try {
      const r = await fetch('/api/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await r.json();
      if (!r.ok) return toast(d.error || 'Could not start', 'error');
    } catch (e) { return toast('Server not reachable', 'error'); }

    $('emptyState').style.display = 'none';
    $('previewArea').style.display = 'none';
    $('progressPanel').style.display = 'block';
    $('ppLog').textContent = '';
    $('ppSpinner').classList.remove('stop');
    $('ppTitle').textContent = 'Processing…';
    S.lastMsg = '';
    $('runBtn').disabled = true;
    clearInterval(S.poll);
    S.poll = setInterval(pollJob, 1000);
  });

  function setSteps(p, done) {
    const st = [['stepRvm', 'stepRvmSt', 0, 65], ['stepDespill', 'stepDespillSt', 65, 75], ['stepEncode', 'stepEncodeSt', 75, 101]];
    st.forEach(([row, lab, a, b]) => {
      const r = $(row), l = $(lab);
      r.classList.remove('active', 'done');
      if (done || p >= b) { r.classList.add('done'); l.textContent = 'Done'; }
      else if (p >= a) { r.classList.add('active'); l.textContent = 'Running'; }
      else l.textContent = 'Waiting';
    });
  }

  async function pollJob() {
    let j;
    try {
      const r = await fetch('/api/job/' + S.jobId, { cache: 'no-store' });
      if (!r.ok) return;
      j = await r.json();
    } catch (e) { return; }
    const p = j.progress || 0;
    $('ppFill').style.width = p + '%';
    $('ppPct').textContent = p + '%';
    if (j.message && j.message !== S.lastMsg) {
      S.lastMsg = j.message;
      const log = $('ppLog');
      log.textContent += j.message + '\n';
      log.scrollTop = log.scrollHeight;
    }
    setSteps(p, j.status === 'done');
    if (j.status === 'done') {
      clearInterval(S.poll);
      $('runBtn').disabled = false;
      $('ppSpinner').classList.add('stop');
      $('ppTitle').textContent = 'Complete';
      showOutputs(j.outputs || {});
    } else if (j.status === 'error') {
      clearInterval(S.poll);
      $('runBtn').disabled = false;
      $('ppSpinner').classList.add('stop');
      $('ppTitle').textContent = 'Failed';
      $('ppLog').textContent += '\nERROR: ' + (j.message || '') + '\n' + (j.traceback || '');
      toast('Processing failed. See the log below the progress bar.', 'error');
    }
  }

  /* ---------- outputs ---------- */
  function showOutputs(o) {
    const map = [
      ['composite', 'preview', 'vidComposite', 'dlComposite', 'tabComposite', 'vtComposite', 'Composite'],
      ['alpha', 'alpha', 'vidAlpha', 'dlAlpha', 'tabAlpha', 'vtAlpha', 'Alpha Matte'],
      ['green', 'green', 'vidGreen', 'dlGreen', 'tabGreen', 'vtGreen', 'Green Screen'],
      ['transparent', 'transparent', 'vidTransparent', 'dlTransparent', 'tabTransparent', 'vtTransparent', 'Transparent WebM'],
    ];
    const all = $('dlAllBtns');
    all.innerHTML = '';
    let first = null;
    map.forEach(([tab, key, vid, dl, tabBtn, panel, name]) => {
      const url = o[key];
      $(tabBtn).style.display = url ? '' : 'none';
      if (!url) return;
      const v = $(vid);
      v.src = url + '?t=' + Date.now();
      v.load();
      $(dl).href = url + '?dl=1';
      const a = document.createElement('a');
      a.href = url + '?dl=1';
      a.textContent = '↓ ' + name;
      all.appendChild(a);
      if (!first) first = tab;
    });
    $('cpCode').textContent =
      'ffmpeg -i alpha.mp4 -c:v libx264 -crf 10 -preset fast -pix_fmt yuv420p alpha.mp4\n' +
      'ffmpeg -i transparent.mov -c:v libvpx-vp9 -pix_fmt yuva420p -crf 20 -b:v 0 transparent.webm';
    $('previewArea').style.display = 'block';
    if (first) selectTab(first);
    else toast('Finished, but no outputs were selected.', 'error');
  }

  function selectTab(name) {
    document.querySelectorAll('.ptab').forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
    const ids = { composite: 'vtComposite', alpha: 'vtAlpha', green: 'vtGreen', transparent: 'vtTransparent' };
    Object.entries(ids).forEach(([k, id]) => {
      $(id).style.display = k === name ? '' : 'none';
      if (k !== name) { const v = $(id).querySelector('video'); if (v) v.pause(); }
    });
  }
  $('previewTabs').addEventListener('click', (e) => {
    const b = e.target.closest('.ptab');
    if (b) selectTab(b.dataset.tab);
  });

  console.log('BackdropAI RVM Studio loaded');
})();
