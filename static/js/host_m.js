/* ================================================================
   host_m.js  –  Host panel for a phone (/host/m)
   Same host session and APIs as /host; lights go through
   /api/host/lights since party-lights itself isn't on the internet.
   ================================================================ */

const $ = id => document.getElementById(id);

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg, type = '') {
  const toast = document.createElement('div');
  toast.className = 'toast' + (type ? ' ' + type : '');
  toast.textContent = msg;
  $('toast-container').appendChild(toast);
  setTimeout(() => toast.remove(), 3100);
}

function api(url, opts = {}) {
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  return fetch(url, opts);
}

async function post(url, body = {}) {
  const res = await api(url, { method: 'POST', body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) showLogin();
  return { ok: res.ok, data };
}

function applyTheme(theme) {
  if (!theme || document.documentElement.dataset.theme === theme) return;
  document.documentElement.dataset.theme = theme;
  let color = null;
  document.querySelectorAll('link[data-theme-css]').forEach(link => {
    link.disabled = link.dataset.themeCss !== theme;
    if (!link.disabled) color = link.dataset.themeColor;
  });
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = color || meta.dataset.default;
}

function mins(ms) {
  const m = Math.round((ms || 0) / 60000);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

// ----------------------------------------------------------------
// Confirm sheet (a promise that resolves true on OK)
// ----------------------------------------------------------------
function confirmSheet(msg, okLabel = 'OK') {
  return new Promise(resolve => {
    $('sheet-msg').textContent = msg;
    $('sheet-ok').textContent = okLabel;
    $('sheet').classList.remove('hidden');
    const done = answer => {
      $('sheet').classList.add('hidden');
      $('sheet-ok').onclick = $('sheet-cancel').onclick = $('sheet').onclick = null;
      resolve(answer);
    };
    $('sheet-ok').onclick = () => done(true);
    $('sheet-cancel').onclick = () => done(false);
    $('sheet').onclick = e => { if (e.target === $('sheet')) done(false); };
  });
}

// ----------------------------------------------------------------
// Auth
// ----------------------------------------------------------------
function showLogin() {
  stopPolling();
  $('panel-view').classList.add('hidden');
  $('login-view').classList.remove('hidden');
}

function showPanel() {
  $('login-view').classList.add('hidden');
  $('panel-view').classList.remove('hidden');
  selectTab(savedTab());
  startPolling();
}

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const res = await api('/api/host/login', {
    method: 'POST', body: JSON.stringify({ password: $('login-password').value }),
  });
  if (res.ok) {
    $('login-password').value = '';
    $('login-error').classList.add('hidden');
    showPanel();
  } else {
    const data = await res.json().catch(() => ({}));
    $('login-error').textContent = data.error || 'Login failed';
    $('login-error').classList.remove('hidden');
  }
});

// ----------------------------------------------------------------
// Tabs
// ----------------------------------------------------------------
const TABS = ['music', 'lights', 'party', 'stats'];
let currentTab = 'music';

function savedTab() {
  try {
    const t = localStorage.getItem('hm-tab');
    return TABS.includes(t) ? t : 'music';
  } catch (e) { return 'music'; }
}

function selectTab(tab) {
  currentTab = tab;
  try { localStorage.setItem('hm-tab', tab); } catch (e) {}
  document.querySelectorAll('.hm-tabs button').forEach(b =>
    b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false'));
  document.querySelectorAll('.hm-tab').forEach(s =>
    s.classList.toggle('hidden', s.dataset.tab !== tab));
  window.scrollTo(0, 0);
  loadTab();
}

document.querySelector('.hm-tabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-tab]');
  if (btn) selectTab(btn.dataset.tab);
});

// ----------------------------------------------------------------
// Polling: the basics every 5s (they feed the health strip), plus
// whatever the open tab shows. Nothing while the phone is locked.
// ----------------------------------------------------------------
let pollTimer = null;
let health = { reachable: true, healthz: null, spotify: null, lights: null };

function startPolling() {
  if (pollTimer) return;
  tick();
  pollTimer = setInterval(tick, 5000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

document.addEventListener('visibilitychange', () => {
  if ($('panel-view').classList.contains('hidden')) return;
  if (document.hidden) stopPolling();
  else startPolling();
});

let ticks = 0;
async function tick() {
  await Promise.all([loadHealth(), loadSpotifyStatus(), loadLights(), loadStatus()]);
  renderHealth();
  // Music and lights are covered above; party and stats change slower.
  if (++ticks % 2 === 0) loadTab();
}

function loadTab() {
  if (currentTab === 'party') loadParty();
  if (currentTab === 'stats') loadStats();
}

async function getJson(url) {
  const res = await fetch(url);
  if (res.status === 401) { showLogin(); throw new Error('logged out'); }
  return res.json();
}

// ----------------------------------------------------------------
// Health strip
// ----------------------------------------------------------------
async function loadHealth() {
  try {
    const res = await fetch('/healthz');
    health.healthz = await res.json();
    health.reachable = true;
  } catch (e) {
    health.reachable = false;
  }
}

function setPill(id, state, text) {
  const el = $(id);
  el.className = 'hm-pill ' + state;
  el.lastChild.textContent = text;
}

function renderHealth() {
  const problems = [];
  const h = health.healthz;
  if (!health.reachable) {
    problems.push("Can't reach the jukebox. Check the Mac and its internet.");
    setPill('health-music', 'bad', 'Jukebox');
  } else if (h && (!h.database || !h.queue_worker)) {
    problems.push(!h.database ? 'The database is unreadable.' : 'The queue worker stopped. Restart the jukebox.');
    setPill('health-music', 'bad', 'Jukebox');
  } else if (h && h.standing_down) {
    setPill('health-music', 'warn', 'Standby');
  } else {
    setPill('health-music', h ? 'ok' : '', 'Jukebox');
  }

  const sp = health.spotify;
  if (sp && !sp.connected) {
    problems.push('Spotify is disconnected. Reconnect it from the full panel.');
    setPill('health-spotify', 'bad', 'Spotify');
  } else if (sp && sp.playback_issue) {
    problems.push(sp.playback_issue.host_message);
    setPill('health-spotify', 'bad', 'Spotify');
  } else {
    setPill('health-spotify', sp ? 'ok' : '', 'Spotify');
  }

  const li = health.lights;
  if (li && !li.online) {
    problems.push("Can't reach the lights.");
    setPill('health-lights', 'bad', 'Lights');
  } else if (li && li.fps != null && li.target_fps && li.fps < li.target_fps * 0.85) {
    problems.push(`Lights are running slow (${li.fps.toFixed(0)} of ${li.target_fps.toFixed(0)} fps).`);
    setPill('health-lights', 'warn', `Lights ${li.fps.toFixed(0)} fps`);
  } else if (li && li.blackout) {
    setPill('health-lights', 'warn', 'Blackout');
  } else {
    setPill('health-lights', li ? 'ok' : '', 'Lights');
  }

  $('health-msg').textContent = problems.join(' ');
  $('health-msg').classList.toggle('hidden', !problems.length);
}

// ----------------------------------------------------------------
// Music
// ----------------------------------------------------------------
let inviteUrl = '';

async function loadSpotifyStatus() {
  try {
    const data = await getJson('/api/host/spotify_status');
    health.spotify = data;
    $('standby').classList.toggle('hidden', !data.standing_down);
    const issue = data.playback_issue;
    $('playback-issue').classList.toggle('hidden', !issue);
    $('playback-issue').textContent = issue ? '⚠ ' + issue.host_message : '';
    if (data.invite_url && data.invite_url !== inviteUrl) {
      inviteUrl = data.invite_url;
      $('qr-img').src = '/qr?' + Date.now();
      $('party-code').textContent = data.party_code || '';
      $('tv-link').href = data.tv_url || '/tv';
      if ($('recap-link')) $('recap-link').href = data.recap_url || '/recap';
    }
  } catch (e) {}
}

async function loadStatus() {
  try {
    const data = await getJson('/api/status');
    applyTheme(data.ui_theme);
    renderNowPlaying(data.current_track);
    renderQueue(data.queue || []);
  } catch (e) {}
}

function renderNowPlaying(t) {
  $('now-playing').innerHTML = t
    ? `${t.album_art ? `<img src="${escHtml(t.album_art)}" alt="">` : ''}
       <div class="hm-item-main">
         <div class="hm-now-title">${escHtml(t.track_name)}</div>
         <div class="hm-now-artist">${escHtml(t.artist)}</div>
       </div>`
    : '<span class="hm-dim">Nothing playing</span>';
}

function renderQueue(queue) {
  const wait = queue.reduce((ms, q) => ms + (q.duration_ms || 0), 0);
  $('queue-count').textContent = queue.length ? `· ${queue.length} · ${mins(wait)}` : '';
  $('clear-queue-btn').disabled = !queue.length;
  if (!queue.length) {
    $('queue-list').innerHTML = '<div class="hm-empty">The queue is empty.</div>';
    return;
  }
  $('queue-list').innerHTML = queue.map((q, i) => `
    <div class="hm-item">
      <div class="hm-move">
        <button type="button" data-move="up" data-id="${q.id}" aria-label="Move up"${i === 0 ? ' disabled' : ''}>▲</button>
        <button type="button" data-move="down" data-id="${q.id}" aria-label="Move down"${i === queue.length - 1 ? ' disabled' : ''}>▼</button>
      </div>
      ${q.album_art ? `<img src="${escHtml(q.album_art)}" alt="">` : '<div class="hm-art"></div>'}
      <div class="hm-item-main">
        <div class="hm-item-title">${escHtml(q.track_name)}</div>
        <div class="hm-item-sub">${escHtml(q.artist)} · ${escHtml(q.nickname || '—')}${q.upvote_count ? ` · 👍 ${q.upvote_count}` : ''}</div>
      </div>
    </div>`).join('');
}

$('queue-list').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-move]');
  if (!btn) return;
  btn.disabled = true;
  const { ok } = await post('/api/host/reorder', { queue_id: +btn.dataset.id, direction: btn.dataset.move });
  if (!ok) showToast('Could not move that', 'error');
  loadStatus();
});

$('skip-btn').addEventListener('click', async () => {
  $('skip-btn').disabled = true;
  const { ok, data } = await post('/api/host/skip');
  $('skip-btn').disabled = false;
  showToast(ok ? 'Skipped' : (data.error || 'Skip failed'), ok ? 'success' : 'error');
  loadStatus();
});

$('clear-queue-btn').addEventListener('click', async () => {
  if (!await confirmSheet('Clear every song waiting in the queue?', 'CLEAR QUEUE')) return;
  const { ok } = await post('/api/host/clear_queue');
  showToast(ok ? 'Queue cleared' : 'Failed to clear', ok ? 'success' : 'error');
  loadStatus();
});

$('resume-btn').addEventListener('click', async () => {
  const { ok } = await post('/api/host/resume_party');
  if (ok) showToast('Party resumed', 'success');
  loadSpotifyStatus();
});

// Search + add
let searchTimer = null;
let lastQuery = '';
let results = [];

$('search-input').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (!q) { results = []; lastQuery = ''; $('search-results').innerHTML = ''; return; }
  searchTimer = setTimeout(() => search(q), 450);
});

async function search(q) {
  if (q === lastQuery) return;
  lastQuery = q;
  $('search-results').innerHTML = '<div class="hm-empty">Searching…</div>';
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (q !== lastQuery) return;
    if (!res.ok || data.error) {
      $('search-results').innerHTML = `<div class="hm-empty">${escHtml(data.error || 'Search unavailable')}</div>`;
      return;
    }
    results = data.tracks || [];
    $('search-results').innerHTML = results.length
      ? results.map((t, i) => `
        <div class="hm-item">
          ${t.album_art ? `<img src="${escHtml(t.album_art)}" alt="">` : '<div class="hm-art"></div>'}
          <div class="hm-item-main">
            <div class="hm-item-title">${escHtml(t.track_name)}</div>
            <div class="hm-item-sub">${escHtml(t.artist)}</div>
          </div>
          <button class="btn btn-cyan btn-sm" type="button" data-add="${i}">ADD</button>
        </div>`).join('')
      : '<div class="hm-empty">No results.</div>';
  } catch (e) {
    $('search-results').innerHTML = '<div class="hm-empty">Search failed.</div>';
  }
}

$('search-results').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-add]');
  if (!btn) return;
  const t = results[+btn.dataset.add];
  if (!t) return;
  btn.disabled = true;
  const { ok, data } = await post('/api/host/queue', {
    track_id: t.track_id, track_name: t.track_name, artist: t.artist,
    album_art: t.album_art || '', duration_ms: t.duration_ms,
  });
  if (ok) {
    showToast(`Added “${t.track_name}”`, 'success');
    $('search-input').value = '';
    $('search-results').innerHTML = '';
    results = [];
    lastQuery = '';
    loadStatus();
  } else {
    btn.disabled = false;
    showToast(data.error || 'Failed to add', 'error');
  }
});

// ----------------------------------------------------------------
// Lights
// ----------------------------------------------------------------
let lightsBusy = false;   // while a control is in flight, don't redraw it

async function loadLights() {
  try {
    const data = await getJson('/api/host/lights');
    health.lights = data;
    if (!lightsBusy) renderLights(data);
  } catch (e) {}
}

function renderLights(li) {
  $('lights-offline').classList.toggle('hidden', li.online);
  $('lights-panel').classList.toggle('hidden', !li.online);
  if (!li.online) return;

  $('blackout-btn').setAttribute('aria-pressed', li.blackout ? 'true' : 'false');
  $('blackout-btn').textContent = li.blackout ? 'BLACKOUT ON · TAP TO RESTORE' : 'BLACKOUT';
  const manual = li.mode === 'manual';
  $('auto-btn').classList.toggle('hidden', !manual);
  const preset = li.presets.find(p => p.name === li.preset);
  $('lights-status').textContent = (manual
    ? `Holding ${preset ? preset.label : li.look}`
    : `Auto show · ${li.look || '—'}`) + ` · ${li.palette || '—'}`;

  const grid = $('presets');
  if (grid.childElementCount !== li.presets.length) {
    grid.innerHTML = li.presets.map(p =>
      `<button type="button" data-preset="${escHtml(p.name)}" aria-pressed="false">${escHtml(p.label)}</button>`).join('');
  }
  grid.querySelectorAll('button').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.preset === li.preset ? 'true' : 'false'));

  setSlider('master', li.master);
  setSlider('vibe', li.vibe);
}

function setSlider(name, value) {
  const slider = $(`${name}-slider`);
  if (value == null || document.activeElement === slider) return;
  slider.value = Math.round(value * 100);
  $(`${name}-val`).textContent = `${slider.value}%`;
}

async function lights(body, okMsg) {
  lightsBusy = true;
  const { ok, data } = await post('/api/host/lights', body);
  lightsBusy = false;
  if (ok) {
    health.lights = data;
    renderLights(data);
    renderHealth();
    if (okMsg) showToast(okMsg, 'success');
  } else {
    showToast(data.error || 'The lights didn’t respond', 'error');
  }
}

$('blackout-btn').addEventListener('click', () => lights({ action: 'blackout' }));
$('auto-btn').addEventListener('click', () => lights({ action: 'auto' }, 'Back to the auto show'));
$('presets').addEventListener('click', e => {
  const btn = e.target.closest('button[data-preset]');
  if (btn) lights({ action: 'preset', name: btn.dataset.preset });
});

// Sliders send while dragging (throttled) and once more on release.
['master', 'vibe'].forEach(name => {
  const slider = $(`${name}-slider`);
  let timer = null;
  const send = () => lights({ action: name, value: slider.value / 100 });
  slider.addEventListener('input', () => {
    $(`${name}-val`).textContent = `${slider.value}%`;
    if (!timer) timer = setTimeout(() => { timer = null; send(); }, 200);
  });
  slider.addEventListener('change', () => { clearTimeout(timer); timer = null; send(); });
});

// ----------------------------------------------------------------
// Party: costume contest, camera, invite, guests
// ----------------------------------------------------------------
const COSTUME_TEXT = {
  off: 'Hidden from guests.',
  open: 'Voting is open. Guests enter and vote from their phones.',
  closed: 'Voting closed. The winner is on the TV and every phone.',
};
let guests = [];

function loadParty() {
  return Promise.all([loadCostume(), loadCamera(), loadGuests()]);
}

async function loadCostume() {
  try {
    const c = await getJson('/api/host/costume');
    $('costume-phase').textContent = COSTUME_TEXT[c.phase] || '';
    $('costume-open').disabled = c.phase === 'open';
    $('costume-open').textContent = c.phase === 'closed' ? 'REOPEN' : 'OPEN VOTING';
    $('costume-close').disabled = c.phase !== 'open';
    $('costume-off').disabled = c.phase === 'off';
    $('costume-tally').innerHTML = c.entries.length
      ? `<div class="hm-item-sub">${c.entries.length} entr${c.entries.length === 1 ? 'y' : 'ies'} · ${c.votes} vote${c.votes === 1 ? '' : 's'}</div>` +
        c.entries.map(e => `
          <div class="hm-item">
            <span class="hm-num">${e.rank}</span>
            ${e.photo_url ? `<img src="${escHtml(e.photo_url)}" alt="">` : '<div class="hm-art"></div>'}
            <div class="hm-item-main">
              <div class="hm-item-title">${escHtml(e.costume)}</div>
              <div class="hm-item-sub">${escHtml(e.nickname)}</div>
            </div>
            <span class="hm-votes">${e.votes}</span>
            <button class="btn btn-ghost btn-sm" type="button" data-remove="${e.id}" aria-label="Remove entry">✕</button>
          </div>`).join('')
      : '<div class="hm-empty">No entries yet.</div>';
  } catch (e) {}
}

async function costumePhase(phase, okMsg) {
  const { ok } = await post('/api/host/costume/phase', { phase });
  showToast(ok ? okMsg : 'Failed to update the contest', ok ? 'success' : 'error');
  loadCostume();
}

$('costume-open').addEventListener('click', () => costumePhase('open', 'Costume voting is open'));
$('costume-close').addEventListener('click', async () => {
  if (await confirmSheet('Close voting and reveal the winner on the TV?', 'REVEAL')) {
    costumePhase('closed', 'Voting closed. Winner revealed!');
  }
});
$('costume-off').addEventListener('click', () => costumePhase('off', 'Costume contest hidden'));

$('costume-tally').addEventListener('click', async e => {
  const btn = e.target.closest('button[data-remove]');
  if (!btn || !await confirmSheet('Remove this costume entry and its votes?', 'REMOVE')) return;
  const res = await api(`/api/host/costume/entry/${btn.dataset.remove}`, { method: 'DELETE' });
  showToast(res.ok ? 'Entry removed' : 'Failed to remove', res.ok ? 'success' : 'error');
  loadCostume();
});

async function loadCamera() {
  try {
    const cam = await getJson('/api/host/camera');
    $('camera-toggle').checked = cam.enabled;
    const tonight = cam.parties.find(p => p.party_code === cam.current_party);
    $('camera-counts').textContent = tonight
      ? `${tonight.total} photo${tonight.total === 1 ? '' : 's'} tonight · ${tonight.pending} waiting for review`
      : 'No photos yet tonight.';
    if (cam.develop_at != null && $('develop-status')) {
      const when = new Date(cam.develop_at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      $('develop-status').textContent = cam.developed
        ? 'Developed: the photo wall is live.'
        : `Photos develop at ${when}. Change the time in the full panel.`;
      $('develop-now-btn').classList.toggle('hidden', !!cam.developed);
      if (cam.wall_url) $('wall-link').href = cam.wall_url;
    }
  } catch (e) {}
}

$('develop-now-btn')?.addEventListener('click', async () => {
  if (!confirm("Develop tonight's photos now? The TV switches to the photo wall.")) return;
  const { ok } = await post('/api/host/settings', { develop_now: true });
  showToast(ok ? 'Developed! The wall is live.' : 'Failed to develop', ok ? 'success' : 'error');
  loadCamera();
});

$('camera-toggle').addEventListener('change', async e => {
  const { ok } = await post('/api/host/settings', { camera_enabled: e.target.checked });
  showToast(ok ? (e.target.checked ? 'Camera on' : 'Camera off') : 'Failed to save', ok ? 'success' : 'error');
  loadCamera();
});

$('share-btn').addEventListener('click', async () => {
  if (!inviteUrl) return;
  if (navigator.share) {
    try { await navigator.share({ title: 'Join the party jukebox', url: inviteUrl }); } catch (e) {}
    return;
  }
  try {
    await navigator.clipboard.writeText(inviteUrl);
    showToast('Invite link copied', 'success');
  } catch (e) {
    showToast(inviteUrl);
  }
});

async function loadGuests() {
  try {
    guests = (await getJson('/api/host/users')).users || [];
    renderGuests();
  } catch (e) {}
}

function renderGuests() {
  const q = $('guest-filter').value.trim().toLowerCase();
  const list = guests
    .filter(u => u.user_id !== 'host' && (!q || u.nickname.toLowerCase().includes(q)))
    .sort((a, b) => (b.is_banned - a.is_banned) || a.nickname.localeCompare(b.nickname));
  $('guest-count').textContent = `· ${guests.filter(u => u.user_id !== 'host').length}`;
  $('guest-list').innerHTML = list.length
    ? list.map(u => `
      <div class="hm-item${u.is_banned ? ' hm-banned' : ''}">
        <div class="hm-item-main">
          <div class="hm-item-title">${escHtml(u.nickname)}</div>
          <div class="hm-item-sub">${u.songs_queued} queued · ${u.songs_skipped_count} skipped</div>
        </div>
        ${u.is_banned
          ? `<button class="btn btn-cyan btn-sm" type="button" data-unban="${escHtml(u.user_id)}">UNBAN</button>`
          : `<button class="btn btn-danger btn-sm" type="button" data-ban="${escHtml(u.user_id)}" data-name="${escHtml(u.nickname)}">BAN</button>`}
      </div>`).join('')
    : `<div class="hm-empty">${q ? 'Nobody by that name.' : 'No guests yet.'}</div>`;
}

$('guest-filter').addEventListener('input', renderGuests);

$('guest-list').addEventListener('click', async e => {
  const ban = e.target.closest('button[data-ban]');
  const unban = e.target.closest('button[data-unban]');
  if (ban) {
    if (!await confirmSheet(`Ban ${ban.dataset.name}? They won't be able to add songs or vote.`, 'BAN')) return;
    const { ok } = await post('/api/host/ban', { user_id: ban.dataset.ban });
    showToast(ok ? 'Guest banned' : 'Failed to ban', ok ? 'success' : 'error');
  } else if (unban) {
    const { ok } = await post('/api/host/unban', { user_id: unban.dataset.unban });
    showToast(ok ? 'Guest unbanned' : 'Failed to unban', ok ? 'success' : 'error');
  } else {
    return;
  }
  loadGuests();
});

// ----------------------------------------------------------------
// Stats
// ----------------------------------------------------------------
async function loadStats() {
  try {
    const s = await getJson('/api/host/stats');
    const tiles = [
      [s.guests, 'guests here'],
      [s.active_now, 'active in the last 15 min'],
      [s.songs_played, 'songs played'],
      [s.songs_requested, 'songs requested'],
      [s.queue_depth, `in the queue · ${mins(s.queue_ms)}`],
      [s.hearts, 'hearts'],
      [s.photos, `photos · ${s.photos_pending} to review`],
      [s.costume_entries, `costumes · ${s.costume_votes} votes`],
    ];
    $('stat-tiles').innerHTML = tiles.map(([n, label]) =>
      `<div class="hm-tile"><b>${n}</b><span>${escHtml(label)}</span></div>`).join('');

    $('stat-top').innerHTML = s.top_requesters.length
      ? s.top_requesters.map((r, i) => `
        <div class="hm-item">
          <span class="hm-num">${i + 1}</span>
          <div class="hm-item-main"><div class="hm-item-title">${escHtml(r.nickname)}</div></div>
          <span class="hm-votes">${r.n}</span>
        </div>`).join('')
      : '<div class="hm-empty">No requests yet.</div>';

    const songs = [
      ['❤️ Most hearted', s.most_hearted],
      ['👍 Most upvoted', s.most_upvoted],
      ['👎 Most downvoted', s.most_downvoted],
    ].filter(([, song]) => song);
    $('stat-songs').innerHTML = (songs.length
      ? songs.map(([label, song]) => `
        <div class="hm-item">
          <div class="hm-item-main">
            <div class="hm-item-sub">${label}</div>
            <div class="hm-item-title">${escHtml(song.track_name)}</div>
            <div class="hm-item-sub">${escHtml(song.artist)}</div>
          </div>
          <span class="hm-votes">${song.votes}</span>
        </div>`).join('')
      : '<div class="hm-empty">No votes yet.</div>') +
      `<div class="hm-item-sub" style="padding-top:8px">${s.songs_skipped} skipped · ${s.banned} guest${s.banned === 1 ? '' : 's'} banned</div>`;

    const since = new Date(s.since.replace(' ', 'T') + 'Z');
    $('stat-since').textContent = isNaN(since) ? '' :
      `Counting since ${since.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })}`;
  } catch (e) {}
}

// ----------------------------------------------------------------
// Init
// ----------------------------------------------------------------
(async () => {
  try {
    const data = await (await fetch('/api/host/auth_check')).json();
    if (data.authenticated) return showPanel();
  } catch (e) {}
  showLogin();
})();


// ----------------------------------------------------------------
// Setlist: every song played tonight, saved to a private Spotify playlist.
// Pressing it again updates the same playlist.
// ----------------------------------------------------------------
document.getElementById('setlist-btn')?.addEventListener('click', async () => {
  const btn = document.getElementById('setlist-btn');
  const status = document.getElementById('setlist-status');
  btn.disabled = true;
  try {
    const res = await fetch('/api/host/setlist', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      showToast(`${data.updated ? 'Updated' : 'Saved'}: ${data.songs} songs`, 'success');
      if (status) status.innerHTML = `<a href="${data.url}" target="_blank" rel="noopener">Open the setlist in Spotify</a>`;
    } else {
      showToast(data.error || 'Could not save the setlist', 'error');
    }
  } catch (e) {
    showToast('Could not save the setlist', 'error');
  } finally {
    btn.disabled = false;
  }
});
