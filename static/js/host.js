/* ================================================================
   host.js  –  Host Control Panel
   ================================================================ */

const $ = id => document.getElementById(id);

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg, type = '') {
  const container = $('toast-container');
  const toast = document.createElement('div');
  toast.className = 'toast' + (type ? ' ' + type : '');
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3100);
}

// ----------------------------------------------------------------
// Auth
// ----------------------------------------------------------------
let isLoggedIn = false;

async function checkAuth() {
  try {
    const res = await fetch('/api/host/auth_check');
    const data = await res.json();
    if (data.authenticated) {
      showPanel();
    }
  } catch (e) {}
}

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const pw = $('login-password').value;
  const res = await fetch('/api/host/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password: pw }),
  });
  const data = await res.json();
  if (res.ok) {
    isLoggedIn = true;
    showPanel();
  } else {
    $('login-error').textContent = data.error || 'Login failed';
    $('login-error').classList.remove('hidden');
  }
});

function hostFetch(url, opts = {}) {
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  return fetch(url, opts);
}

function showPanel() {
  $('login-view').classList.add('hidden');
  $('panel-view').classList.remove('hidden');
  isLoggedIn = true;
  loadAll();
  startPolling();
}

// ----------------------------------------------------------------
// Data loading
// ----------------------------------------------------------------
async function loadAll() {
  await Promise.all([loadSpotifyStatus(), loadUsers(), loadStatus(), loadDemoState()]);
}

async function loadStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    renderNowPlaying(data.current_track);
    renderManageQueue(data.queue);
    renderSettings(data.settings);
  } catch (e) {}
}

async function loadDemoState() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    const toggle = $('demo-toggle');
    if (toggle && data.demo_mode !== undefined) {
      toggle.checked = !!data.demo_mode;
      $('demo-status').textContent = data.demo_mode ? 'ON' : 'OFF';
      $('demo-status').style.color = data.demo_mode ? 'var(--primary)' : 'var(--text-muted)';
    }
  } catch (e) {}
}

async function loadSpotifyStatus() {
  try {
    const res = await hostFetch('/api/host/spotify_status');
    const data = await res.json();
    const statusEl = $('spotify-status');
    const connectBtn = $('spotify-connect-btn');
    const qrImg = $('qr-img');
    const partyUrlEl = $('party-url-display');

    if (data.connected) {
      statusEl.textContent = '✓ Connected';
      statusEl.style.color = 'var(--accent)';
      connectBtn.classList.add('hidden');
    } else {
      statusEl.textContent = '✗ Not connected';
      statusEl.style.color = 'var(--warning)';
      connectBtn.classList.remove('hidden');
      if (data.auth_url) {
        connectBtn.href = data.auth_url;
      }
    }

    if (data.party_url) {
      qrImg.src = '/qr?' + Date.now();
      partyUrlEl.textContent = data.party_url;
    }
  } catch (e) {}
}

async function loadUsers() {
  try {
    const res = await hostFetch('/api/host/users');
    const data = await res.json();
    renderUsers(data.users || []);
  } catch (e) {}
}

// ----------------------------------------------------------------
// Render
// ----------------------------------------------------------------
function renderNowPlaying(track) {
  const el = $('now-playing-info');
  if (!track) {
    el.innerHTML = '<span style="color:var(--text-dim)">Nothing playing</span>';
    return;
  }
  el.innerHTML = `
    <div class="flex items-center gap-12">
      ${track.album_art ? `<img src="${escHtml(track.album_art)}" style="width:56px;height:56px;border-radius:6px;object-fit:cover" alt="art">` : ''}
      <div>
        <div style="color:var(--accent);font-size:1rem">${escHtml(track.track_name)}</div>
        <div style="color:var(--text-dim);font-size:0.82rem">${escHtml(track.artist)}</div>
      </div>
    </div>
  `;
}

function renderManageQueue(queue) {
  const el = $('queue-manage-list');
  if (!queue || queue.length === 0) {
    el.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem;font-style:italic">Queue is empty</div>';
    return;
  }
  el.innerHTML = queue.map((item, i) => `
    <div class="manage-queue-item">
      <div class="reorder-btns">
        <button class="reorder-btn" title="Move up"
          onclick="reorderItem(${item.id}, 'up')" ${i === 0 ? 'disabled style="opacity:0.3"' : ''}>▲</button>
        <button class="reorder-btn" title="Move down"
          onclick="reorderItem(${item.id}, 'down')" ${i === queue.length - 1 ? 'disabled style="opacity:0.3"' : ''}>▼</button>
      </div>
      ${item.album_art
        ? `<img src="${escHtml(item.album_art)}" alt="art">`
        : `<div style="width:36px;height:36px;background:var(--bg-card2);border-radius:3px;flex-shrink:0;border:1px solid rgba(0,0,0,0.1)"></div>`
      }
      <div class="manage-queue-info">
        <div class="manage-queue-title">${escHtml(item.track_name)}</div>
        <div class="manage-queue-artist">${escHtml(item.artist)}</div>
      </div>
    </div>
  `).join('');
}

function renderSettings(settings) {
  if (!settings) return;
  const thresholdSlider = $('threshold-slider');
  const thresholdVal = $('threshold-val');
  const maxQueueSlider = $('max-queue-slider');
  const maxQueueVal = $('max-queue-val');
  const banThresholdSlider = $('ban-threshold-slider');
  const banThresholdVal = $('ban-threshold-val');

  if (thresholdSlider.value == thresholdSlider.defaultValue || thresholdSlider.dataset.loaded !== 'true') {
    thresholdSlider.value = settings.downvote_threshold;
    thresholdVal.textContent = settings.downvote_threshold;
    thresholdSlider.dataset.loaded = 'true';
  }
  if (maxQueueSlider.dataset.loaded !== 'true') {
    maxQueueSlider.value = settings.max_queue_per_user;
    maxQueueVal.textContent = settings.max_queue_per_user;
    maxQueueSlider.dataset.loaded = 'true';
  }
  if (banThresholdSlider.dataset.loaded !== 'true') {
    banThresholdSlider.value = settings.skip_ban_threshold;
    banThresholdVal.textContent = settings.skip_ban_threshold;
    banThresholdSlider.dataset.loaded = 'true';
  }
}

function renderUsers(users) {
  const tbody = $('users-tbody');
  if (!users.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-dim);padding:20px">No guests yet</td></tr>';
    return;
  }
  tbody.innerHTML = users.map(u => `
    <tr>
      <td>${escHtml(u.nickname)}</td>
      <td>${u.songs_queued}</td>
      <td>${u.songs_skipped_count}</td>
      <td>
        <span class="badge ${u.is_banned ? 'badge-banned' : 'badge-ok'}">
          ${u.is_banned ? 'BANNED' : 'OK'}
        </span>
      </td>
      <td style="font-size:0.72rem;color:var(--text-dim)">${escHtml(u.first_seen || '')}</td>
      <td>
        ${u.is_banned
          ? `<button class="btn btn-cyan btn-sm" onclick="unbanUser('${escHtml(u.user_id)}')">UNBAN</button>`
          : `<button class="btn btn-danger btn-sm" onclick="banUser('${escHtml(u.user_id)}')">BAN</button>`
        }
      </td>
    </tr>
  `).join('');
}

// ----------------------------------------------------------------
// Actions
// ----------------------------------------------------------------
window.banUser = async (userId) => {
  if (!confirm('Ban this user?')) return;
  const res = await hostFetch('/api/host/ban', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  });
  if (res.ok) { showToast('User banned', 'success'); loadUsers(); }
  else showToast('Failed to ban', 'error');
};

window.unbanUser = async (userId) => {
  const res = await hostFetch('/api/host/unban', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  });
  if (res.ok) { showToast('User unbanned', 'success'); loadUsers(); }
  else showToast('Failed to unban', 'error');
};

$('skip-btn').addEventListener('click', async () => {
  const res = await hostFetch('/api/host/skip', { method: 'POST' });
  const data = await res.json();
  if (res.ok) showToast('Skipped!', 'success');
  else showToast(data.error || 'Skip failed', 'error');
});

$('clear-queue-btn').addEventListener('click', async () => {
  if (!confirm('Clear the entire pending queue?')) return;
  const res = await hostFetch('/api/host/clear_queue', { method: 'POST' });
  if (res.ok) { showToast('Queue cleared', 'success'); loadStatus(); }
  else showToast('Failed', 'error');
});

// Sliders
$('threshold-slider').addEventListener('input', e => {
  $('threshold-val').textContent = e.target.value;
});

$('max-queue-slider').addEventListener('input', e => {
  $('max-queue-val').textContent = e.target.value;
});

$('ban-threshold-slider').addEventListener('input', e => {
  $('ban-threshold-val').textContent = e.target.value;
});

$('save-settings-btn').addEventListener('click', async () => {
  const body = {
    downvote_threshold: parseInt($('threshold-slider').value, 10),
    max_queue_per_user: parseInt($('max-queue-slider').value, 10),
    skip_ban_threshold: parseInt($('ban-threshold-slider').value, 10),
  };
  const newPw = $('new-password').value.trim();
  if (newPw) body.host_password = newPw;

  const partyUrl = $('party-url-input').value.trim();
  if (partyUrl) body.party_url = partyUrl;

  const res = await hostFetch('/api/host/settings', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  if (res.ok) {
    showToast('Settings saved!', 'success');
    $('new-password').value = '';
    loadSpotifyStatus();
  } else {
    showToast('Failed to save settings', 'error');
  }
});

// ----------------------------------------------------------------
// Queue reorder
// ----------------------------------------------------------------
window.reorderItem = async (queueId, direction) => {
  const res = await hostFetch('/api/host/reorder', {
    method: 'POST',
    body: JSON.stringify({ queue_id: queueId, direction }),
  });
  if (res.ok) loadStatus();
  else showToast('Could not reorder', 'error');
};

// ----------------------------------------------------------------
// Demo mode
// ----------------------------------------------------------------
$('demo-toggle').addEventListener('change', async e => {
  const enabled = e.target.checked;
  $('demo-status').textContent = enabled ? 'ON' : 'OFF';
  $('demo-status').style.color = enabled ? 'var(--primary)' : 'var(--text-muted)';
  await hostFetch('/api/host/demo', {
    method: 'POST',
    body: JSON.stringify({ enabled }),
  });
  showToast(enabled ? 'Demo mode ON' : 'Demo mode OFF', 'success');
});

$('set-demo-track-btn').addEventListener('click', async () => {
  const name = $('demo-track-name').value.trim();
  const artist = $('demo-artist').value.trim();
  const art = $('demo-album-art').value.trim();
  if (!name || !artist) { showToast('Enter a track name and artist', 'error'); return; }
  const res = await hostFetch('/api/host/demo', {
    method: 'POST',
    body: JSON.stringify({ track: { track_name: name, artist, album_art: art } }),
  });
  if (res.ok) showToast(`Demo track set: "${name}"`, 'success');
  else showToast('Failed to set demo track', 'error');
});

// ----------------------------------------------------------------
// Host queue a song
// ----------------------------------------------------------------
// Spotify search
let hostSearchTimeout = null;
let hostLastQuery = '';

$('host-search-input').addEventListener('input', e => {
  clearTimeout(hostSearchTimeout);
  const q = e.target.value.trim();
  if (!q) { hideHostResults(); return; }
  hostSearchTimeout = setTimeout(() => doHostSearch(q), 500);
});

function hideHostResults() {
  const r = $('host-search-results');
  r.classList.remove('visible');
  r.innerHTML = '';
}

async function doHostSearch(q) {
  if (q === hostLastQuery) return;
  hostLastQuery = q;
  const r = $('host-search-results');
  r.innerHTML = '<div style="text-align:center;padding:14px"><div class="spinner"></div></div>';
  r.classList.add('visible');
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (!res.ok || data.error) {
      r.innerHTML = `<div class="empty-queue">${escHtml(data.error || 'Search unavailable — use manual entry')}</div>`;
      return;
    }
    renderHostSearchResults(data.tracks);
  } catch (e) {
    r.innerHTML = '<div class="empty-queue">Search failed.</div>';
  }
}

function renderHostSearchResults(tracks) {
  const r = $('host-search-results');
  if (!tracks || !tracks.length) {
    r.innerHTML = '<div class="empty-queue">No results.</div>';
    return;
  }
  r.innerHTML = tracks.map(t => `
    <div class="search-result-item">
      ${t.album_art ? `<img src="${escHtml(t.album_art)}" alt="art">` : ''}
      <div class="search-result-info">
        <div class="search-result-title">${escHtml(t.track_name)}</div>
        <div class="search-result-artist">${escHtml(t.artist)}</div>
      </div>
      <button class="btn btn-cyan btn-sm"
        onclick="hostQueueTrack('${escHtml(t.track_id)}','${escHtml(t.track_name)}','${escHtml(t.artist)}','${escHtml(t.album_art||'')}',${t.duration_ms})">
        QUEUE IT
      </button>
    </div>
  `).join('');
}

window.hostQueueTrack = async (trackId, trackName, artist, albumArt, durationMs) => {
  const res = await hostFetch('/api/host/queue', {
    method: 'POST',
    body: JSON.stringify({ track_id: trackId, track_name: trackName, artist, album_art: albumArt, duration_ms: durationMs }),
  });
  const data = await res.json();
  if (res.ok) {
    showToast(`"${trackName}" added to queue!`, 'success');
    hideHostResults();
    $('host-search-input').value = '';
    hostLastQuery = '';
    loadStatus();
  } else {
    showToast(data.error || 'Failed to queue', 'error');
  }
};

// Manual add
$('host-manual-add-btn').addEventListener('click', async () => {
  const name = $('host-manual-name').value.trim();
  const artist = $('host-manual-artist').value.trim();
  if (!name || !artist) { showToast('Enter track name and artist', 'error'); return; }
  const res = await hostFetch('/api/host/queue', {
    method: 'POST',
    body: JSON.stringify({ track_name: name, artist }),
  });
  const data = await res.json();
  if (res.ok) {
    showToast(`"${name}" added to queue!`, 'success');
    $('host-manual-name').value = '';
    $('host-manual-artist').value = '';
    loadStatus();
  } else {
    showToast(data.error || 'Failed', 'error');
  }
});

// Close host search when clicking outside
document.addEventListener('click', e => {
  const wrap = $('host-search-section');
  if (wrap && !wrap.contains(e.target)) hideHostResults();
});

// ----------------------------------------------------------------
// Polling
// ----------------------------------------------------------------
let pollInterval = null;

function startPolling() {
  if (pollInterval) return;
  pollInterval = setInterval(loadAll, 5000);
}

// ----------------------------------------------------------------
// Init
// ----------------------------------------------------------------
checkAuth();
