/* ================================================================
   tv.js  –  TV Display Mode
   ================================================================ */

const $ = id => document.getElementById(id);

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ----------------------------------------------------------------
// State
// ----------------------------------------------------------------
let prevFireCount = 0;
let prevHeartCount = 0;
let prevTrackId = null;

// ----------------------------------------------------------------
// Render
// ----------------------------------------------------------------
function renderNowPlaying(track, reactions) {
  const vinyl = $('tv-vinyl');
  const albumArt = $('tv-album-art');
  const trackName = $('tv-track-name');
  const artistEl = $('tv-artist');
  const noTrack = $('tv-no-track');
  const playingSection = $('tv-playing-section');

  if (!track) {
    noTrack.classList.remove('hidden');
    playingSection.classList.add('hidden');
    if (vinyl) { vinyl.style.animationPlayState = 'paused'; }
    if (albumArt) { albumArt.style.animationPlayState = 'paused'; }
    return;
  }

  noTrack.classList.add('hidden');
  playingSection.classList.remove('hidden');

  // Song change → clear reactions animation
  if (track.track_id !== prevTrackId) {
    prevFireCount = 0;
    prevHeartCount = 0;
    prevTrackId = track.track_id;
  }

  trackName.textContent = track.track_name;
  artistEl.textContent = track.artist;

  if (track.album_art) {
    albumArt.src = track.album_art;
    albumArt.classList.remove('hidden');
  } else {
    albumArt.classList.add('hidden');
  }

  if (track.is_playing) {
    vinyl.style.animationPlayState = 'running';
    albumArt.style.animationPlayState = 'running';
  } else {
    vinyl.style.animationPlayState = 'paused';
    albumArt.style.animationPlayState = 'paused';
  }

  // Reactions
  const fireCount = reactions ? reactions.fire : 0;
  const heartCount = reactions ? reactions.heart : 0;

  $('tv-fire-count').textContent = fireCount;
  $('tv-heart-count').textContent = heartCount;

  if (fireCount > prevFireCount) {
    popReaction('tv-fire-wrap');
  }
  if (heartCount > prevHeartCount) {
    popReaction('tv-heart-wrap');
  }
  prevFireCount = fireCount;
  prevHeartCount = heartCount;
}

function popReaction(id) {
  const el = $(id);
  el.classList.remove('popping');
  void el.offsetWidth;
  el.classList.add('popping');
}

function renderQueue(queue) {
  const list = $('tv-queue-list');
  if (!queue || queue.length === 0) {
    list.innerHTML = '<div style="color:var(--text-dim);padding:20px;text-align:center;font-family:VT323,monospace;font-size:1.5rem">NOTHING QUEUED</div>';
    return;
  }
  list.innerHTML = queue.slice(0, 5).map((item, i) => `
    <div class="tv-queue-item">
      <div class="tv-queue-num">${i + 1}</div>
      ${item.album_art
        ? `<img src="${escHtml(item.album_art)}" alt="art">`
        : `<div style="width:48px;height:48px;background:var(--bg-card);border-radius:4px;flex-shrink:0"></div>`
      }
      <div style="min-width:0">
        <div class="tv-queue-track">${escHtml(item.track_name)}</div>
        <div class="tv-queue-artist">${escHtml(item.artist)}</div>
      </div>
    </div>
  `).join('');
}

function renderTicker(bannedUsers) {
  const inner = $('tv-ticker-inner');
  if (!bannedUsers || bannedUsers.length === 0) {
    inner.textContent = '🎵 WELCOME TO THE PARTY JUKEBOX 🎵';
    return;
  }
  const items = bannedUsers.map(u => `🚫 ${u.nickname} HAS BEEN SILENCED 🚫`);
  // Duplicate for seamless loop
  inner.textContent = [...items, ...items].join('          ');
}

// ----------------------------------------------------------------
// Poll
// ----------------------------------------------------------------
async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();
    renderNowPlaying(data.current_track, data.reactions);
    renderQueue(data.queue);
    renderTicker(data.banned_users);
  } catch (e) {
    console.error('TV poll error:', e);
  }
}

// ----------------------------------------------------------------
// Fullscreen
// ----------------------------------------------------------------
document.addEventListener('click', () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(() => {});
  }
});

// ----------------------------------------------------------------
// Start
// ----------------------------------------------------------------
pollStatus();
setInterval(pollStatus, 3000);
