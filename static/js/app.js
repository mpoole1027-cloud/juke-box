/* ================================================================
   app.js  –  Guest Jukebox UI
   ================================================================ */

// ----------------------------------------------------------------
// User identity
// ----------------------------------------------------------------
function getUserId() {
  let uid = localStorage.getItem('jukebox_user_id');
  if (!uid) {
    uid = crypto.randomUUID ? crypto.randomUUID() : generateUUID();
    localStorage.setItem('jukebox_user_id', uid);
  }
  return uid;
}

function generateUUID() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

const USER_ID = getUserId();

function apiFetch(url, opts = {}) {
  opts.headers = Object.assign({ 'X-User-ID': USER_ID, 'Content-Type': 'application/json' }, opts.headers || {});
  return fetch(url, opts);
}

// ----------------------------------------------------------------
// Toast
// ----------------------------------------------------------------
function showToast(msg, type = '') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = 'toast' + (type ? ' ' + type : '');
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 3100);
}

// ----------------------------------------------------------------
// State
// ----------------------------------------------------------------
let state = {
  current_track: null,
  current_queue_id: null,
  downvote_count: 0,
  user_has_downvoted: false,
  queue: [],
  reactions: { fire: 0, heart: 0 },
  banned_users: [],
  user: null,
  settings: { downvote_threshold: 7 },
  spotify_connected: false,
};

// ----------------------------------------------------------------
// DOM helpers
// ----------------------------------------------------------------
const $ = id => document.getElementById(id);

// ----------------------------------------------------------------
// Render functions
// ----------------------------------------------------------------
function renderNowPlaying(track, queueId, downvoteCount, userDownvoted, reactions, settings) {
  const section = $('now-playing-section');
  const vinyl = $('vinyl-svg');
  const albumArt = $('album-art-img');
  const trackName = $('track-name');
  const trackArtist = $('track-artist');
  const progressFill = $('progress-fill');
  const downvoteBtn = $('downvote-btn');
  const downvoteLabel = $('downvote-label');
  const votePips = $('vote-pips');
  const noTrack = $('no-track-msg');

  if (!track) {
    section.classList.add('hidden');
    noTrack.classList.remove('hidden');
    return;
  }

  section.classList.remove('hidden');
  noTrack.classList.add('hidden');

  trackName.textContent = track.track_name;
  trackArtist.textContent = track.artist;

  if (track.album_art) {
    albumArt.src = track.album_art;
    albumArt.classList.remove('hidden');
  } else {
    albumArt.classList.add('hidden');
  }

  const progress = track.duration_ms > 0
    ? Math.min(100, (track.progress_ms / track.duration_ms) * 100)
    : 0;
  progressFill.style.width = progress + '%';

  if (track.is_playing) {
    vinyl.classList.remove('vinyl-paused');
    albumArt.style.animationPlayState = 'running';
  } else {
    vinyl.classList.add('vinyl-paused');
    albumArt.style.animationPlayState = 'paused';
  }

  // Downvote button
  const threshold = settings.downvote_threshold;
  downvoteBtn.disabled = userDownvoted || !queueId;
  downvoteLabel.textContent = `SKIP THIS! (${downvoteCount}/${threshold})`;

  // Vote pips
  votePips.innerHTML = '';
  for (let i = 0; i < threshold; i++) {
    const pip = document.createElement('div');
    pip.className = 'vote-pip' + (i < downvoteCount ? ' filled' : '');
    votePips.appendChild(pip);
  }

  // Reactions
  $('fire-count').textContent = reactions.fire || 0;
  $('heart-count').textContent = reactions.heart || 0;
}

function renderQueue(queue) {
  const list = $('queue-list');
  if (!queue || queue.length === 0) {
    list.innerHTML = '<div class="empty-queue">Nothing queued yet... be the first! 🎵</div>';
    return;
  }
  list.innerHTML = queue.map((item, i) => `
    <div class="queue-item">
      <div class="queue-position">${i + 1}</div>
      ${item.album_art
        ? `<img src="${escHtml(item.album_art)}" alt="art">`
        : `<div style="width:40px;height:40px;background:var(--bg-card2);border-radius:4px;flex-shrink:0"></div>`
      }
      <div class="queue-item-info">
        <div class="queue-item-title">${escHtml(item.track_name)}</div>
        <div class="queue-item-artist">${escHtml(item.artist)}</div>
      </div>
    </div>
  `).join('');
}

function renderBanned(bannedUsers) {
  const list = $('banned-list');
  const section = $('hall-of-shame');
  if (!bannedUsers || bannedUsers.length === 0) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');
  list.innerHTML = bannedUsers.map(u =>
    `<span class="banned-badge">🚫 ${escHtml(u.nickname)}</span>`
  ).join('');
}

function renderUserBanned(user) {
  const banner = $('ban-banner');
  const mainContent = $('main-content');
  if (user && user.is_banned) {
    $('ban-nickname').textContent = user.nickname;
    banner.classList.remove('hidden');
    mainContent.classList.add('hidden');
  } else {
    banner.classList.add('hidden');
    mainContent.classList.remove('hidden');
  }
}

function renderNickname(user) {
  if (user) {
    $('nickname-display').textContent = '🎵 ' + user.nickname;
  }
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ----------------------------------------------------------------
// Poll status
// ----------------------------------------------------------------
let lastFireCount = 0;
let lastHeartCount = 0;

async function pollStatus() {
  try {
    const res = await apiFetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();
    state = data;

    renderUserBanned(data.user);
    renderNickname(data.user);
    renderNowPlaying(
      data.current_track,
      data.current_queue_id,
      data.downvote_count,
      data.user_has_downvoted,
      data.reactions,
      data.settings
    );
    renderQueue(data.queue);
    renderBanned(data.banned_users);

    // Spotify not connected notice
    if (!data.spotify_connected) {
      $('spotify-warning').classList.remove('hidden');
    } else {
      $('spotify-warning').classList.add('hidden');
    }

  } catch (e) {
    console.error('Poll error:', e);
  }
}

// ----------------------------------------------------------------
// Downvote
// ----------------------------------------------------------------
$('downvote-btn').addEventListener('click', async () => {
  if (!state.current_queue_id) {
    showToast('Nothing is playing right now!', 'error');
    return;
  }
  try {
    const res = await apiFetch('/api/downvote', {
      method: 'POST',
      body: JSON.stringify({ queue_item_id: state.current_queue_id }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.error || 'Could not downvote', 'error');
      return;
    }
    state.downvote_count = data.downvote_count;
    state.user_has_downvoted = true;
    $('downvote-btn').disabled = true;
    $('downvote-label').textContent = `SKIP THIS! (${data.downvote_count}/${data.threshold})`;
    showToast('Downvote registered! 👎', 'success');
  } catch (e) {
    showToast('Network error', 'error');
  }
});

// ----------------------------------------------------------------
// Reactions
// ----------------------------------------------------------------
async function sendReaction(type) {
  try {
    const res = await apiFetch('/api/react', {
      method: 'POST',
      body: JSON.stringify({ reaction: type }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.error || 'Could not react', 'error');
      return;
    }
    // Animate button
    const btn = type === 'fire' ? $('fire-btn') : $('heart-btn');
    btn.classList.remove('popped');
    void btn.offsetWidth;
    btn.classList.add('popped');
    $('fire-count').textContent = data.reactions.fire;
    $('heart-count').textContent = data.reactions.heart;
  } catch (e) {
    showToast('Network error', 'error');
  }
}

$('fire-btn').addEventListener('click', () => sendReaction('fire'));
$('heart-btn').addEventListener('click', () => sendReaction('heart'));

// ----------------------------------------------------------------
// Search
// ----------------------------------------------------------------
let searchTimeout = null;
let lastQuery = '';

$('search-input').addEventListener('input', e => {
  clearTimeout(searchTimeout);
  const q = e.target.value.trim();
  if (!q) {
    hideSearchResults();
    return;
  }
  searchTimeout = setTimeout(() => doSearch(q), 500);
});

$('search-input').addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    hideSearchResults();
    $('search-input').value = '';
  }
});

function hideSearchResults() {
  $('search-results').classList.remove('visible');
  $('search-results').innerHTML = '';
}

async function doSearch(q) {
  if (q === lastQuery) return;
  lastQuery = q;
  const results = $('search-results');
  results.innerHTML = '<div style="text-align:center;padding:16px"><div class="spinner"></div></div>';
  results.classList.add('visible');

  try {
    const res = await apiFetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    if (!res.ok) {
      results.innerHTML = `<div class="empty-queue">${escHtml(data.error || 'Search failed')}</div>`;
      return;
    }
    renderSearchResults(data.tracks);
  } catch (e) {
    results.innerHTML = '<div class="empty-queue">Search failed.</div>';
  }
}

function renderSearchResults(tracks) {
  const results = $('search-results');
  if (!tracks || tracks.length === 0) {
    results.innerHTML = '<div class="empty-queue">No results found.</div>';
    return;
  }
  results.innerHTML = tracks.map((t, i) => `
    <div class="search-result-item" style="animation-delay:${i * 0.04}s">
      ${t.album_art
        ? `<img src="${escHtml(t.album_art)}" alt="art">`
        : `<div style="width:48px;height:48px;background:var(--bg-card);border-radius:4px;flex-shrink:0"></div>`
      }
      <div class="search-result-info">
        <div class="search-result-title">${escHtml(t.track_name)}</div>
        <div class="search-result-artist">${escHtml(t.artist)}</div>
      </div>
      <button class="btn btn-cyan btn-sm queue-it-btn"
        data-track-id="${escHtml(t.track_id)}"
        data-track-name="${escHtml(t.track_name)}"
        data-artist="${escHtml(t.artist)}"
        data-album-art="${escHtml(t.album_art || '')}"
        data-duration="${t.duration_ms}"
      >QUEUE IT</button>
    </div>
  `).join('');

  // Attach queue button listeners
  results.querySelectorAll('.queue-it-btn').forEach(btn => {
    btn.addEventListener('click', () => queueTrack(btn));
  });
}

async function queueTrack(btn) {
  btn.disabled = true;
  btn.textContent = '...';

  const body = {
    track_id: btn.dataset.trackId,
    track_name: btn.dataset.trackName,
    artist: btn.dataset.artist,
    album_art: btn.dataset.albumArt,
    duration_ms: parseInt(btn.dataset.duration, 10),
  };

  try {
    const res = await apiFetch('/api/queue', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.error || 'Could not queue song', 'error');
      btn.disabled = false;
      btn.textContent = 'QUEUE IT';
      return;
    }
    showToast(`"${body.track_name}" added to queue! 🎉`, 'success');
    btn.textContent = '✓ QUEUED';
    btn.style.opacity = '0.5';
    // Clear search
    setTimeout(() => {
      hideSearchResults();
      $('search-input').value = '';
      lastQuery = '';
    }, 1200);
    pollStatus();
  } catch (e) {
    showToast('Network error', 'error');
    btn.disabled = false;
    btn.textContent = 'QUEUE IT';
  }
}

// Close search results when clicking outside
document.addEventListener('click', e => {
  const searchWrap = $('search-wrap');
  if (!searchWrap.contains(e.target)) {
    hideSearchResults();
  }
});

// ----------------------------------------------------------------
// Start polling
// ----------------------------------------------------------------
pollStatus();
setInterval(pollStatus, 3000);
