/* ================================================================
   tv.js  –  TV Display Mode / Live Party Dashboard
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
// Theme (host-controlled; mirrored from every poll)
// ----------------------------------------------------------------
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

// ----------------------------------------------------------------
// State
// ----------------------------------------------------------------
let prevHeartCount = 0;
let prevTrackId = null;

// Live reaction feed
let lastSeenReactionId = 0;
let reactionBaselineSet = false;   // becomes true after first poll
const MAX_FLOATERS = 30;

// Ticker rotation
let tickerMessages = [];
let tickerIdx = 0;

// ----------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------
function fmtTime(ms) {
  if (!ms || ms < 0 || !isFinite(ms)) ms = 0;
  const totalSec = Math.floor(ms / 1000);
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return m + ':' + String(s).padStart(2, '0');
}

function fmtRuntime(startStr) {
  if (!startStr) return '—';
  // party_start is "YYYY-MM-DD HH:MM:SS" (local server time). Parse safely.
  const iso = String(startStr).replace(' ', 'T');
  const start = new Date(iso);
  if (isNaN(start.getTime())) return '—';
  let diff = Date.now() - start.getTime();
  if (diff < 0) diff = 0;
  const totalMin = Math.floor(diff / 60000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

const MEDALS = ['🥇', '🥈', '🥉'];

// ----------------------------------------------------------------
// Now Playing
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
    prevTrackId = null;
    return;
  }

  noTrack.classList.add('hidden');
  playingSection.classList.remove('hidden');

  // Song change → clear reactions animation
  if (track.track_id !== prevTrackId) {
    prevHeartCount = 0;
    prevTrackId = track.track_id;
  }

  trackName.textContent = track.track_name || '';
  artistEl.textContent = track.artist || '';

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

  // Progress bar
  const dur = Number(track.duration_ms) || 0;
  const prog = Math.max(0, Number(track.progress_ms) || 0);
  const pct = dur > 0 ? Math.min(100, (prog / dur) * 100) : 0;
  $('tv-progress-fill').style.width = pct + '%';
  $('tv-elapsed').textContent = fmtTime(prog);
  $('tv-total').textContent = dur > 0 ? fmtTime(dur) : '--:--';

  // Requester
  const reqEl = $('tv-requester');
  if (track.requested_by_nickname) {
    reqEl.innerHTML = 'Requested by <strong>' + escHtml(track.requested_by_nickname) + '</strong>';
    reqEl.classList.remove('hidden');
  } else {
    reqEl.classList.add('hidden');
  }

  // Reaction counters
  const heartCount = reactions ? (reactions.heart || 0) : 0;

  $('tv-heart-count').textContent = heartCount;

  if (heartCount > prevHeartCount) popReaction('tv-heart-wrap');
  prevHeartCount = heartCount;
}

function popReaction(id) {
  const el = $(id);
  if (!el) return;
  el.classList.remove('popped');
  void el.offsetWidth;
  el.classList.add('popped');
}

// ----------------------------------------------------------------
// Up Next queue
// ----------------------------------------------------------------
function renderQueue(queue) {
  const list = $('tv-queue-list');
  if (!queue || queue.length === 0) {
    list.innerHTML = '<div class="tv-queue-empty">Nothing queued yet</div>';
    return;
  }
  list.innerHTML = queue.slice(0, 5).map((item, i) => {
    const art = item.album_art
      ? `<img src="${escHtml(item.album_art)}" alt="art">`
      : `<div class="tv-queue-art-fallback">🎵</div>`;
    const upvote = (item.upvote_count > 0)
      ? `<span class="tv-queue-upvote">👍 ${item.upvote_count}${item.skips_maxed ? ' · ⏫ max' : ''}</span>`
      : '';
    const requester = item.nickname
      ? `<span class="tv-queue-added">added by ${escHtml(item.nickname)}</span>`
      : '';
    return `
    <div class="tv-queue-item">
      <div class="tv-queue-num">${i + 1}</div>
      ${art}
      <div class="tv-queue-body">
        <div class="tv-queue-track">${escHtml(item.track_name)}</div>
        <div class="tv-queue-artist">${escHtml(item.artist)}</div>
        <div class="tv-queue-meta">
          ${requester}
          ${upvote}
        </div>
      </div>
    </div>`;
  }).join('');
}

// ----------------------------------------------------------------
// Leaderboards
// ----------------------------------------------------------------
function renderLeaderboard(elId, rows, mapRow) {
  const el = $(elId);
  if (!rows || rows.length === 0) {
    el.innerHTML = '<div class="tv-lb-empty">No data yet</div>';
    return;
  }
  el.innerHTML = rows.slice(0, 5).map((r, i) => {
    const rank = i < 3
      ? `<span class="tv-lb-medal">${MEDALS[i]}</span>`
      : `<span class="tv-lb-rank">${i + 1}</span>`;
    const { name, val } = mapRow(r);
    return `
    <div class="tv-lb-row">
      ${rank}
      <span class="tv-lb-name">${name}</span>
      <span class="tv-lb-val">${val}</span>
    </div>`;
  }).join('');
}

function renderLeaderboards(lb) {
  lb = lb || {};
  renderLeaderboard('tv-lb-djs', lb.top_djs, r => ({
    name: escHtml(r.nickname),
    val: r.count
  }));
  renderLeaderboard('tv-lb-favorites', lb.crowd_favorites, r => ({
    name: escHtml(r.track_name) + ' <span class="tv-lb-sub">— ' + escHtml(r.artist) + '</span>',
    val: '♥️ ' + r.reaction_count
  }));
  renderLeaderboard('tv-lb-skipped', lb.most_skipped, r => ({
    name: escHtml(r.nickname),
    val: '⏭️ ' + r.songs_skipped_count
  }));
}

// ----------------------------------------------------------------
// Party stats
// ----------------------------------------------------------------
let partyStart = null;

function renderStats(stats) {
  stats = stats || {};
  $('tv-stat-songs').textContent = stats.songs_played != null ? stats.songs_played : 0;
  $('tv-stat-guests').textContent = stats.guest_count != null ? stats.guest_count : 0;
  $('tv-stat-reactions').textContent = stats.total_reactions != null ? stats.total_reactions : 0;
  partyStart = stats.party_start || null;
  updateRuntime();
}

function updateRuntime() {
  $('tv-stat-runtime').textContent = fmtRuntime(partyStart);
}

// ----------------------------------------------------------------
// Live reaction feed
// ----------------------------------------------------------------
function spawnFloaters(recent) {
  if (!Array.isArray(recent)) recent = [];

  const maxId = recent.reduce((m, r) => (r.id > m ? r.id : m), lastSeenReactionId);

  // First poll → set baseline to current max, do NOT animate the backlog
  if (!reactionBaselineSet) {
    reactionBaselineSet = true;
    lastSeenReactionId = maxId;
    return;
  }

  if (recent.length === 0) return;

  const feed = $('tv-reaction-feed');
  if (!feed) { lastSeenReactionId = maxId; return; }

  // Newest-first array → animate the new ones (older first for nicer stagger)
  const fresh = recent.filter(r => r.id > lastSeenReactionId).reverse();
  for (const r of fresh) {
    if (feed.childElementCount >= MAX_FLOATERS) break;
    const el = document.createElement('div');
    el.className = 'tv-floater';
    el.textContent = '♥️';
    // random horizontal position + drift so they don't stack
    const left = 15 + Math.random() * 70;      // 15%–85%
    const drift = (Math.random() * 60 - 30);    // -30px .. +30px
    el.style.left = left + '%';
    el.style.setProperty('--tv-drift', drift + 'px');
    feed.appendChild(el);
    el.addEventListener('animationend', () => el.remove());
    // safety removal in case animationend never fires
    setTimeout(() => { if (el.parentNode) el.remove(); }, 3000);
  }

  lastSeenReactionId = maxId;
}

// ----------------------------------------------------------------
// Ticker (party shoutouts)
// ----------------------------------------------------------------
function buildTickerMessages(data) {
  const msgs = [];
  const lb = data.leaderboards || {};
  const stats = data.stats || {};

  if (lb.crowd_favorites && lb.crowd_favorites.length) {
    const f = lb.crowd_favorites[0];
    msgs.push(`♥️ Crowd favorite: ${f.track_name} — ${f.artist}`);
  }
  if (lb.top_djs && lb.top_djs.length) {
    const d = lb.top_djs[0];
    msgs.push(`🎧 Top DJ: ${d.nickname} (${d.count} spun)`);
  }
  if (stats.songs_played) {
    msgs.push(`🎉 ${stats.songs_played} songs played tonight`);
  }
  if (stats.total_reactions) {
    msgs.push(`♥️ ${stats.total_reactions} hearts and counting`);
  }
  if (stats.guest_count) {
    msgs.push(`🕺 ${stats.guest_count} guests in the room`);
  }
  const costume = data.costume || {};
  if (costume.phase === 'open') {
    msgs.unshift('🎃 Costume contest! Vote for your favorite on your phone');
  } else if (costume.phase === 'closed') {
    const winners = winnersOf(costume);
    if (winners.length) {
      msgs.unshift('👑 Best costume: ' + winners.map(w => `${w.costume} (${w.nickname})`).join(' & '));
    }
  }
  if (msgs.length === 0) {
    msgs.push('🎵 WELCOME TO THE PARTY JUKEBOX 🎵');
  }
  return msgs;
}

function renderTicker(data) {
  const next = buildTickerMessages(data);
  // Only reset the rotation if the message set actually changed
  if (next.join('|') !== tickerMessages.join('|')) {
    tickerMessages = next;
    if (tickerIdx >= tickerMessages.length) tickerIdx = 0;
    setTickerText(tickerMessages[tickerIdx]);
  }
}

function setTickerText(text) {
  const inner = $('tv-ticker-inner');
  if (inner) inner.textContent = text;
}

function rotateTicker() {
  if (tickerMessages.length <= 1) return;
  tickerIdx = (tickerIdx + 1) % tickerMessages.length;
  setTickerText(tickerMessages[tickerIdx]);
}

// ----------------------------------------------------------------
// Costume contest
// ----------------------------------------------------------------
const COSTUME_REVEAL_SECS = 90;   // how long the winner fills the screen
const COSTUME_DRUMROLL_MS = 4000;
let costumeRevealedAt = null;   // when the reveal on screen closed voting (ms, TV clock)
let costumeTimers = [];

function winnersOf(c) {
  return (c.results || []).filter(r => r.rank === 1);
}

function renderCostume(c) {
  c = c || { phase: 'off' };
  const banner = $('tv-costume-banner');
  banner.classList.toggle('hidden', c.phase !== 'open');
  if (c.phase === 'open') {
    $('tv-costume-counts').textContent =
      `${c.entries} entr${c.entries === 1 ? 'y' : 'ies'} · ${c.votes} vote${c.votes === 1 ? '' : 's'}`;
  }

  const fresh = c.phase === 'closed' && c.closed_secs_ago != null
    && c.closed_secs_ago < COSTUME_REVEAL_SECS && winnersOf(c).length > 0;
  if (!fresh) {
    if (costumeRevealedAt !== null) hideCostumeReveal();
    costumeRevealedAt = null;
    return;
  }
  // Same closing as the reveal already running: leave it be. A later one
  // (the host reopened and closed again) starts over.
  const closedAt = Date.now() - c.closed_secs_ago * 1000;
  if (costumeRevealedAt !== null && Math.abs(closedAt - costumeRevealedAt) < 5000) return;
  hideCostumeReveal();
  costumeRevealedAt = closedAt;
  startCostumeReveal(c);
}

function startCostumeReveal(c) {
  const winners = winnersOf(c);
  const tie = winners.length > 1;
  $('tv-costume-winner-name').textContent = winners.map(w => w.costume).join(' & ');
  $('tv-costume-winner-who').textContent =
    (tie ? "It's a tie! " : '') + winners.map(w => w.nickname).join(' & ') +
    ` · ${winners[0].votes} vote${winners[0].votes === 1 ? '' : 's'}`;
  const frame = r => r.photo_url
    ? `<span class="costume-frame"><img src="${escHtml(r.photo_url)}" alt=""></span>`
    : '';
  $('tv-costume-winner-photos').innerHTML = winners.slice(0, 3)
    .map(w => `<div class="tv-costume-winner-photo">${frame(w)}</div>`).join('');
  const rest = (c.results || []).filter(r => r.rank > 1).slice(0, 3);
  $('tv-costume-podium').innerHTML = rest.map(r =>
    `<div>${frame(r)}<span>${r.rank <= 3 ? MEDALS[r.rank - 1] : r.rank} <strong>${escHtml(r.costume)}</strong> · ${escHtml(r.nickname)}</span></div>`
  ).join('');

  $('tv-costume-reveal').classList.remove('hidden');
  const age = c.closed_secs_ago * 1000;
  // A screen that loads mid-reveal skips straight to the winner.
  const drumroll = Math.max(0, COSTUME_DRUMROLL_MS - age);
  $('tv-costume-drumroll').classList.toggle('hidden', drumroll === 0);
  $('tv-costume-winner').classList.toggle('hidden', drumroll > 0);
  costumeTimers.push(setTimeout(() => {
    $('tv-costume-drumroll').classList.add('hidden');
    $('tv-costume-winner').classList.remove('hidden');
  }, drumroll));
  costumeTimers.push(setTimeout(hideCostumeReveal, COSTUME_REVEAL_SECS * 1000 - age));
}

function hideCostumeReveal() {
  costumeTimers.forEach(clearTimeout);
  costumeTimers = [];
  $('tv-costume-reveal').classList.add('hidden');
}

// ----------------------------------------------------------------
// Poll
// ----------------------------------------------------------------
async function pollTv() {
  try {
    const res = await fetch('/api/tv', { credentials: 'same-origin' });
    if (res.status === 403) {
      // No party code in this browser yet. The host panel's TV link carries it.
      document.getElementById('tv-no-track').classList.remove('hidden');
      document.querySelector('#tv-no-track .tv-no-track-sub').textContent =
        'Open this screen from the TV link in the host panel';
      return;
    }
    if (!res.ok) return;
    const data = await res.json();
    applyTheme(data.ui_theme);
    renderNowPlaying(data.current_track, data.reactions);
    renderQueue(data.queue);
    renderStats(data.stats);
    renderLeaderboards(data.leaderboards);
    spawnFloaters(data.recent_reactions);
    renderTicker(data);
    renderCostume(data.costume);
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
pollTv();
setInterval(pollTv, 3000);
setInterval(updateRuntime, 15000);   // keep runtime fresh between polls
setInterval(rotateTicker, 6000);     // rotate shoutouts
