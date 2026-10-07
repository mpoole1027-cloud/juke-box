/* ================================================================
   camera.js  –  Disposable camera for guests

   Shoot blind: the viewfinder is live, but a shot is never shown back.
   Each shot goes into an outbox (IndexedDB, so it survives a closed tab
   or a dead connection) and is uploaded from there, retrying until the
   jukebox has it. app.js calls Camera.update() with every status poll.
   ================================================================ */

const Camera = (() => {
  const $ = id => document.getElementById(id);

  let enabled = false;
  let serverShotsLeft = 0;
  let stream = null;
  let facing = 'environment';
  let winding = false;

  // ----------------------------------------------------------------
  // Outbox: shots waiting to reach the jukebox
  // ----------------------------------------------------------------
  // IndexedDB keeps unsent shots across reloads. If it's unavailable
  // (private mode on some browsers) shots live in memory for this visit.
  const memoryOutbox = new Map();
  let dbPromise = null;

  function openDb() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise(resolve => {
      try {
        const req = indexedDB.open('jukebox-camera', 1);
        req.onupgradeneeded = () => req.result.createObjectStore('outbox', { keyPath: 'id' });
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => resolve(null);
        req.onblocked = () => resolve(null);
      } catch (e) {
        resolve(null);
      }
    });
    return dbPromise;
  }

  function idb(mode, fn) {
    return openDb().then(db => new Promise((resolve, reject) => {
      if (!db) return reject(new Error('no idb'));
      const tx = db.transaction('outbox', mode);
      const req = fn(tx.objectStore('outbox'));
      tx.oncomplete = () => resolve(req && req.result);
      tx.onerror = tx.onabort = () => reject(tx.error);
    }));
  }

  async function outboxPut(shot) {
    try { await idb('readwrite', s => s.put(shot)); }
    catch (e) { memoryOutbox.set(shot.id, shot); }
  }

  async function outboxDelete(id) {
    memoryOutbox.delete(id);
    try { await idb('readwrite', s => s.delete(id)); } catch (e) {}
  }

  async function outboxAll() {
    let stored = [];
    try { stored = (await idb('readonly', s => s.getAll())) || []; } catch (e) {}
    return stored.concat([...memoryOutbox.values()]).sort((a, b) => a.taken - b.taken);
  }

  // ----------------------------------------------------------------
  // Uploading
  // ----------------------------------------------------------------
  let draining = false;
  let retryTimer = null;
  let retryDelay = 5000;
  let pendingCount = 0;

  function scheduleRetry() {
    clearTimeout(retryTimer);
    retryTimer = setTimeout(drain, retryDelay);
    retryDelay = Math.min(60000, retryDelay * 2);
  }

  // Outcomes that will never succeed on retry: drop the shot.
  const PERMANENT = new Set(['out_of_film', 'bad_photo', 'camera_off', 'banned', 'too_large']);

  async function uploadOne(shot) {
    const form = new FormData();
    form.append('shot_id', shot.id);
    form.append('photo', shot.blob, 'shot.jpg');
    let res;
    try {
      res = await fetch('/api/photos', { method: 'POST', body: form, credentials: 'same-origin' });
    } catch (e) {
      return 'retry';
    }
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (res.ok) {
      if (typeof data.shots_left === 'number') serverShotsLeft = data.shots_left;
      return 'done';
    }
    if (PERMANENT.has(data.code)) {
      if (data.code === 'out_of_film') serverShotsLeft = 0;
      return 'dropped';
    }
    // Party code expired, rate limited, server trouble: keep it and try later.
    return 'retry';
  }

  async function drain() {
    if (draining) return;
    draining = true;
    clearTimeout(retryTimer);
    retryTimer = null;
    try {
      let shots = await outboxAll();
      pendingCount = shots.length;
      render();
      for (const shot of shots) {
        const outcome = await uploadOne(shot);
        if (outcome === 'retry') {
          scheduleRetry();
          break;
        }
        await outboxDelete(shot.id);
        if (outcome === 'dropped') console.warn('Camera: shot not kept by the jukebox');
        retryDelay = 5000;
        pendingCount = Math.max(0, pendingCount - 1);
        render();
      }
    } finally {
      draining = false;
      pendingCount = (await outboxAll()).length;
      render();
    }
  }

  // ----------------------------------------------------------------
  // Card on the guest page
  // ----------------------------------------------------------------
  function shotsLeft() {
    return Math.max(0, serverShotsLeft - pendingCount);
  }

  function render() {
    const card = $('camera-card');
    if (!card) return;
    card.classList.toggle('hidden', !enabled);
    const left = shotsLeft();
    $('camera-shots-left').textContent = left;
    $('camera-shots-left-vf').textContent = left;
    $('camera-open-btn').disabled = left <= 0;
    $('camera-open-btn').textContent = left > 0 ? '📸 OPEN CAMERA' : 'OUT OF FILM';
    $('camera-shutter').disabled = left <= 0 || winding;

    const status = $('camera-status');
    if (pendingCount > 0) {
      status.textContent = `${pendingCount} shot${pendingCount === 1 ? '' : 's'} on the way to the darkroom…`;
    } else {
      status.textContent = left <= 0
        ? "That's the whole roll! Photos get developed after the party."
        : 'Every shot is a surprise. Photos get developed after the party.';
    }
    if (!enabled && stream) close();
  }

  // ----------------------------------------------------------------
  // Viewfinder
  // ----------------------------------------------------------------
  function cameraUnavailableReason() {
    if (!window.isSecureContext) return 'The camera only works on the secure (https) party link.';
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return "This browser can't use the camera. Try Safari or Chrome.";
    }
    return null;
  }

  async function startStream() {
    stopStream();
    stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: { facingMode: facing, width: { ideal: 1920 }, height: { ideal: 1440 } },
    });
    const video = $('camera-video');
    video.srcObject = stream;
    video.classList.toggle('mirrored', facing === 'user');
    await video.play().catch(() => {});
  }

  function stopStream() {
    if (stream) stream.getTracks().forEach(t => t.stop());
    stream = null;
    const video = $('camera-video');
    if (video) video.srcObject = null;
  }

  async function open() {
    const reason = cameraUnavailableReason();
    if (reason) { showToast(reason, 'error'); return; }
    if (shotsLeft() <= 0) { showToast("You're out of film!", 'error'); return; }
    $('camera-overlay').classList.remove('hidden');
    document.body.classList.add('camera-open');
    try {
      await startStream();
    } catch (e) {
      close();
      showToast(e && e.name === 'NotAllowedError'
        ? 'Camera access was blocked. Allow it in your browser settings to take photos.'
        : "Couldn't start the camera.", 'error');
    }
  }

  function close() {
    stopStream();
    $('camera-overlay').classList.add('hidden');
    document.body.classList.remove('camera-open');
  }

  async function flip() {
    facing = facing === 'environment' ? 'user' : 'environment';
    try { await startStream(); }
    catch (e) { showToast("Couldn't switch cameras.", 'error'); }
  }

  // A short mechanical click, synthesised so there's no sound file to load.
  function shutterSound() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = shutterSound.ctx || (shutterSound.ctx = new Ctx());
      const len = Math.floor(ctx.sampleRate * 0.06);
      const buf = ctx.createBuffer(1, len, ctx.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < len; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / len) ** 3;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start();
    } catch (e) {}
  }

  function grabFrame() {
    const video = $('camera-video');
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return Promise.resolve(null);
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d').drawImage(video, 0, 0, w, h);
    return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.9));
  }

  function newShotId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }

  async function takeShot() {
    if (winding || !stream || shotsLeft() <= 0) return;
    winding = true;
    render();

    const blob = await grabFrame();
    // Flash and click straight away; the shot itself is never displayed.
    shutterSound();
    if (navigator.vibrate) navigator.vibrate(30);
    const flash = $('camera-flash');
    flash.classList.remove('firing');
    void flash.offsetWidth;
    flash.classList.add('firing');

    if (blob) {
      await outboxPut({ id: newShotId(), blob, taken: Date.now() });
      pendingCount += 1;
      drain();
    } else {
      showToast('The camera missed that one. Try again.', 'error');
    }

    // Winding on to the next frame, like the real thing.
    $('camera-wind').classList.add('winding');
    setTimeout(() => {
      winding = false;
      $('camera-wind').classList.remove('winding');
      render();
      if (shotsLeft() <= 0) {
        showToast("That's the whole roll! 🎞", 'success');
        close();
      }
    }, 1200);
  }

  // ----------------------------------------------------------------
  // Wiring
  // ----------------------------------------------------------------
  function init() {
    $('camera-open-btn').addEventListener('click', open);
    $('camera-close').addEventListener('click', close);
    $('camera-flip').addEventListener('click', flip);
    $('camera-shutter').addEventListener('click', takeShot);
    // Let go of the camera when the guest leaves the tab.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && stream) close();
      if (!document.hidden) drain();
    });
    window.addEventListener('online', drain);
    drain();
  }

  init();

  return {
    // Called from app.js with the `camera` block of every /api/status poll.
    update(cam) {
      if (!cam) return;
      enabled = !!cam.enabled;
      serverShotsLeft = cam.shots_left;
      document.querySelector('.camera-viewfinder').classList.toggle('filtered', !!cam.viewfinder_filter);
      render();
      if (pendingCount > 0 && !draining && !retryTimer) drain();
    },
  };
})();
