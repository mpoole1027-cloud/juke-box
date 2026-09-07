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
  list.innerHTML = queue.map((item, i) => {
    const isMine = item.requested_by === USER_ID;
    const voted = !!item.user_has_upvoted;
    const count = item.upvote_count || 0;
    const nickname = item.nickname || 'someone';
    const dedication = item.dedication
      ? `<div class="queue-dedication">“${escHtml(item.dedication)}”</div>`
      : '';
    return `
    <div class="queue-item">
      <div class="queue-position">${i + 1}</div>
      ${item.album_art
        ? `<img src="${escHtml(item.album_art)}" alt="art">`
        : `<div style="width:40px;height:40px;background:var(--card-alt);border-radius:4px;flex-shrink:0"></div>`
      }
      <div class="queue-item-info">
        <div class="queue-item-title">${escHtml(item.track_name)}</div>
        <div class="queue-item-artist">${escHtml(item.artist)}</div>
        <div class="queue-requester">added by ${escHtml(nickname)}</div>
        ${dedication}
      </div>
      <div class="queue-actions">
        <button class="queue-upvote-btn${voted ? ' voted' : ''}"
          data-queue-id="${escHtml(item.id)}"${voted ? ' disabled' : ''}
          title="Upvote this song" type="button">
          👍 <span class="queue-upvote-count">${count}</span>
        </button>
        ${isMine
          ? `<button class="queue-remove-btn" data-queue-id="${escHtml(item.id)}" title="Remove your song" type="button">✕</button>`
          : ''
        }
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('.queue-upvote-btn').forEach(btn => {
    btn.addEventListener('click', () => upvoteItem(btn));
  });
  list.querySelectorAll('.queue-remove-btn').forEach(btn => {
    btn.addEventListener('click', () => removeItem(btn));
  });
}

// Upvote a queued song (Feature 3)
async function upvoteItem(btn) {
  const id = btn.dataset.queueId;
  btn.disabled = true;
  try {
    const res = await apiFetch('/api/upvote', {
      method: 'POST',
      body: JSON.stringify({ queue_item_id: parseInt(id, 10) }),
    });
    const data = await res.json();
    if (res.status === 409) {
      btn.classList.add('voted');
      return;
    }
    if (!res.ok || !data.success) {
      showToast(data.error || 'Could not upvote', 'error');
      btn.disabled = false;
      return;
    }
    btn.classList.add('voted');
    const countEl = btn.querySelector('.queue-upvote-count');
    if (countEl && typeof data.upvote_count === 'number') {
      countEl.textContent = data.upvote_count;
    }
  } catch (e) {
    showToast('Network error', 'error');
    btn.disabled = false;
  }
}

// Remove your own queued song (Feature 3)
async function removeItem(btn) {
  const id = btn.dataset.queueId;
  if (!confirm('Remove your song from the queue?')) return;
  btn.disabled = true;
  try {
    const res = await apiFetch(`/api/queue/${id}`, { method: 'DELETE' });
    if (!res.ok) {
      let msg = 'Could not remove song';
      try { const d = await res.json(); msg = d.error || msg; } catch (e) {}
      showToast(msg, 'error');
      btn.disabled = false;
      return;
    }
    showToast('Removed from queue', 'success');
    pollStatus();
  } catch (e) {
    showToast('Network error', 'error');
    btn.disabled = false;
  }
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

// ----------------------------------------------------------------
// Editable nickname (Feature 1)
// ----------------------------------------------------------------
let isEditingNickname = false;
let currentNickname = localStorage.getItem('jukebox_nickname') || '';

function renderNickname(user) {
  // Prefer the server value; remember it locally so optimistic renders match.
  if (user && user.nickname) {
    currentNickname = user.nickname;
    localStorage.setItem('jukebox_nickname', user.nickname);
  }
  // Don't clobber the input while the guest is actively editing.
  if (isEditingNickname) return;

  const badge = $('nickname-display');
  if (!badge || !currentNickname) return;
  badge.classList.remove('nickname-editing');
  badge.setAttribute('title', 'Tap to change your name');
  badge.innerHTML =
    `<span class="nickname-text">🎵 ${escHtml(currentNickname)}</span>` +
    `<span class="nickname-edit-hint" aria-hidden="true">✎</span>`;

  // One-time hint the first time the guest sees the badge.
  if (!localStorage.getItem('jukebox_name_hint_shown')) {
    localStorage.setItem('jukebox_name_hint_shown', '1');
    showToast('Tip: tap your name to change it ✎');
  }
}

function startNicknameEdit() {
  if (isEditingNickname) return;
  isEditingNickname = true;
  const badge = $('nickname-display');
  badge.classList.add('nickname-editing');
  badge.removeAttribute('title');
  badge.innerHTML =
    `<input id="nickname-input" class="nickname-input" type="text" maxlength="24" ` +
    `value="${escHtml(currentNickname)}" aria-label="Your name">` +
    `<button id="nickname-save" class="nickname-save" title="Save name" type="button">✓</button>`;

  const input = $('nickname-input');
  const save = $('nickname-save');
  input.focus();
  input.select();

  let done = false;
  const finishSave = () => {
    if (done) return;
    done = true;
    saveNickname(input.value);
  };
  const cancel = () => {
    if (done) return;
    done = true;
    isEditingNickname = false;
    renderNickname(null);
  };

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); finishSave(); }
    else if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
  input.addEventListener('blur', () => {
    // Let a click on the save button win over blur.
    setTimeout(() => { if (!done) finishSave(); }, 120);
  });
  save.addEventListener('mousedown', e => e.preventDefault());
  save.addEventListener('click', finishSave);
}

async function saveNickname(rawValue) {
  const value = (rawValue || '').trim().slice(0, 24);
  isEditingNickname = false;
  if (!value || value === currentNickname) {
    renderNickname(null);
    return;
  }
  try {
    const res = await apiFetch('/api/user/nickname', {
      method: 'POST',
      body: JSON.stringify({ nickname: value }),
    });
    const data = await res.json();
    if (!res.ok || !data.success) {
      showToast(data.error || 'Could not update name', 'error');
      renderNickname(null);
      return;
    }
    currentNickname = data.nickname;
    localStorage.setItem('jukebox_nickname', currentNickname);
    renderNickname(null);
    showToast('Name updated!', 'success');
    requestNotifyPermission();
  } catch (e) {
    showToast('Network error', 'error');
    renderNickname(null);
  }
}

$('nickname-display').addEventListener('click', () => {
  if (!isEditingNickname) startNicknameEdit();
});

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

// ----------------------------------------------------------------
// "You're up next" / "Now playing" pings (Feature 4)
// ----------------------------------------------------------------
let myPendingTrackIds = new Set();   // my track_ids in the queue on the previous poll
let firedNowPlaying = new Set();     // track_ids we already announced as playing
let firedUpNext = new Set();         // track_ids we already announced as up-next

function requestNotifyPermission() {
  try {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'default') {
      Notification.requestPermission().catch(() => {});
    }
  } catch (e) { /* unsupported browser — ignore */ }
}

function fireNotification(body) {
  try {
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      new Notification('Party Jukebox', { body });
    }
  } catch (e) { /* best-effort only */ }
}

function checkMySongTransitions(data) {
  const queue = Array.isArray(data.queue) ? data.queue : [];
  const mine = queue.filter(it => it.requested_by === USER_ID);
  const myIdsNow = new Set(mine.map(it => it.track_id));

  // "Up next": one of my songs is now first in line and wasn't announced yet.
  const first = queue[0];
  if (first && first.requested_by === USER_ID && !firedUpNext.has(first.track_id)) {
    firedUpNext.add(first.track_id);
    showToast("🔔 You're up next!");
    fireNotification("You're up next — get ready!");
  }

  // "Now playing": my pending song from a previous poll just became the current track.
  const cur = data.current_track;
  if (cur && cur.track_id && myPendingTrackIds.has(cur.track_id) && !firedNowPlaying.has(cur.track_id)) {
    firedNowPlaying.add(cur.track_id);
    showToast('🎉 Your song is now playing!', 'success');
    fireNotification('🎉 Your song is now playing!');
  }

  // Reset the up-next latch once a song leaves the queue so it can fire again if re-queued.
  firedUpNext.forEach(tid => { if (!myIdsNow.has(tid)) firedUpNext.delete(tid); });

  myPendingTrackIds = myIdsNow;
}

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
    checkMySongTransitions(data);

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
    <div class="search-result-wrap" style="animation-delay:${i * 0.04}s">
      <div class="search-result-item">
        ${t.album_art
          ? `<img src="${escHtml(t.album_art)}" alt="art">`
          : `<div style="width:48px;height:48px;background:var(--card-alt);border-radius:4px;flex-shrink:0"></div>`
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
      <div class="dedication-row hidden">
        <input class="dedication-input" type="text" maxlength="80"
          placeholder="Add a dedication (optional)…" aria-label="Dedication">
        <button class="btn btn-cyan btn-sm dedication-confirm" type="button">ADD →</button>
      </div>
    </div>
  `).join('');

  // Attach queue button listeners
  results.querySelectorAll('.queue-it-btn').forEach(btn => {
    btn.addEventListener('click', () => revealDedication(btn));
  });
}

// Reveal the optional dedication row for a result (Feature 2)
function revealDedication(queueBtn) {
  const wrap = queueBtn.closest('.search-result-wrap');
  if (!wrap) { queueTrack(queueBtn, ''); return; }
  const row = wrap.querySelector('.dedication-row');
  const input = wrap.querySelector('.dedication-input');
  const confirm = wrap.querySelector('.dedication-confirm');
  if (!row || row.dataset.wired) {
    // Already revealed — a second QUEUE IT tap just queues with whatever's typed.
    queueTrack(queueBtn, input ? input.value : '');
    return;
  }
  row.dataset.wired = '1';
  row.classList.remove('hidden');
  input.focus();
  requestNotifyPermission();

  const go = () => queueTrack(queueBtn, input.value);
  confirm.addEventListener('click', go);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); go(); }
  });
}

async function queueTrack(btn, dedication = '') {
  btn.disabled = true;
  btn.textContent = '...';

  const body = {
    track_id: btn.dataset.trackId,
    track_name: btn.dataset.trackName,
    artist: btn.dataset.artist,
    album_art: btn.dataset.albumArt,
    duration_ms: parseInt(btn.dataset.duration, 10),
  };
  const ded = (dedication || '').trim();
  if (ded) body.dedication = ded.slice(0, 80);

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
    requestNotifyPermission();
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
