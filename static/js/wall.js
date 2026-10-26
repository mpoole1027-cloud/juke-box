// Photo wall: counts down to develop time, then loops tonight's photos.
// Photos approved after the slideshow starts join it next, ahead of the loop.
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const SLIDE_SECS = 7;
  const POLL_BEFORE_MS = 5000;
  const POLL_AFTER_MS = 20000;

  let clockOffset = 0;        // server time minus local time, seconds
  let developAt = null;
  let developed = null;       // unknown until the first poll
  let order = [];             // photo ids in play order
  let byId = {};
  let pos = -1;               // index in order of the slide on screen
  let fresh = [];             // newly developed ids to show next
  let slideTimer = null;
  let front = 'a';

  const serverNow = () => Date.now() / 1000 + clockOffset;

  function pad(n) { return String(n).padStart(2, '0'); }

  function renderCountdown() {
    if (developAt == null || developed) return;
    const left = Math.max(0, Math.round(developAt - serverNow()));
    $('wall-countdown').textContent =
      `${pad(Math.floor(left / 3600))}:${pad(Math.floor(left / 60) % 60)}:${pad(left % 60)}`;
    if (left === 0) poll();
  }

  function developTimeLabel(ts) {
    const d = new Date(ts * 1000);
    const midnight = d.getHours() === 0 && d.getMinutes() === 0;
    return midnight ? "Tonight's photos develop at midnight"
      : `Tonight's photos develop at ${d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;
  }

  function renderNow(np) {
    const bar = $('wall-now');
    if (!np || !np.track_name) { bar.classList.add('hidden'); return; }
    bar.classList.remove('hidden');
    $('wall-now-title').textContent = np.track_name;
    $('wall-now-artist').textContent = np.artist ? '— ' + np.artist : '';
    const art = $('wall-now-art');
    if (np.album_art) { art.src = np.album_art; art.classList.remove('hidden'); }
    else art.classList.add('hidden');
  }

  function absorb(photos) {
    const known = new Set(order);
    const ids = photos.map(p => p.id);
    photos.forEach(p => { byId[p.id] = p; });
    // Photos rejected since: drop them.
    const keep = new Set(ids);
    if (order.some(id => !keep.has(id))) {
      const current = order[pos];
      order = order.filter(id => keep.has(id));
      fresh = fresh.filter(id => keep.has(id));
      pos = Math.max(-1, order.indexOf(current));
    }
    ids.forEach(id => {
      if (!known.has(id)) { order.push(id); if (pos >= 0) fresh.push(id); }
    });
  }

  function nextId() {
    if (fresh.length) {
      const id = fresh.shift();
      pos = order.indexOf(id);
      return id;
    }
    if (!order.length) return null;
    pos = (pos + 1) % order.length;
    return order[pos];
  }

  function showSlide() {
    clearTimeout(slideTimer);
    const id = nextId();
    $('wall-empty').classList.toggle('hidden', id != null);
    if (id == null) { slideTimer = setTimeout(showSlide, 3000); return; }
    const back = front === 'a' ? 'b' : 'a';
    const el = $('wall-slide-' + back);
    const img = el.querySelector('img');
    const photo = byId[id];
    img.onload = () => {
      img.style.setProperty('--tilt', ((Math.random() * 4) - 2).toFixed(2) + 'deg');
      el.querySelector('.wall-caption').textContent = photo && photo.by ? '📸 ' + photo.by : '';
      $('wall-slide-' + front).classList.remove('on');
      el.classList.add('on');
      front = back;
      $('wall-count').textContent = `${pos + 1} / ${order.length}`;
      slideTimer = setTimeout(showSlide, SLIDE_SECS * 1000);
    };
    img.onerror = () => { slideTimer = setTimeout(showSlide, 500); };
    img.src = '/api/wall/photo/' + id;
    // Warm the cache for the one after.
    const peek = fresh[0] || order[(pos + 1) % order.length];
    if (peek != null && peek !== id) new Image().src = '/api/wall/photo/' + peek;
  }

  function startShow(withReveal) {
    $('wall-darkroom').classList.add('hidden');
    const go = () => {
      $('wall-reveal').classList.add('hidden');
      $('wall-show').classList.remove('hidden');
      showSlide();
    };
    if (withReveal) {
      $('wall-reveal').classList.remove('hidden');
      setTimeout(go, 3600);
    } else {
      go();
    }
  }

  let pollTimer = null;
  async function poll() {
    clearTimeout(pollTimer);
    try {
      const res = await fetch('/api/wall', { credentials: 'same-origin' });
      if (res.status === 403) {
        $('wall-darkroom').classList.add('hidden');
        $('wall-locked').classList.remove('hidden');
        return;
      }
      if (res.ok) {
        const data = await res.json();
        clockOffset = data.now - Date.now() / 1000;
        developAt = data.develop_at;
        $('wall-shots').textContent = data.shots;
        $('wall-develop-time').textContent = developTimeLabel(data.develop_at);
        renderNow(data.now_playing);
        if (data.developed) {
          absorb(data.photos);
          if (developed !== true) startShow(developed === false);
        }
        developed = data.developed;
        renderCountdown();
      }
    } catch (e) {}
    pollTimer = setTimeout(poll, developed ? POLL_AFTER_MS : POLL_BEFORE_MS);
  }

  document.addEventListener('click', () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen().catch(() => {});
  });

  poll();
  setInterval(renderCountdown, 1000);
})();
