// Party recap: everything from /api/recap, rendered once (and refreshed every
// minute while the party is still going).
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function list(id, rows, fmt) {
    $(id).innerHTML = rows && rows.length
      ? rows.map(r => `<li>${fmt(r)}</li>`).join('')
      : '<li class="n">Nobody yet</li>';
  }

  function timeOf(sqliteUtc) {
    if (!sqliteUtc) return '';
    const d = new Date(sqliteUtc.replace(' ', 'T') + 'Z');
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function render(data) {
    $('recap-title').textContent = data.title || 'Party recap';
    const st = data.stats || {};
    $('rs-songs').textContent = (data.setlist || []).length;
    $('rs-guests').textContent = st.guest_count || 0;
    $('rs-hearts').textContent = st.total_reactions || 0;
    $('rs-photos').textContent = st.photos || 0;

    const costume = data.costume;
    $('recap-costume').classList.toggle('hidden', !(costume && costume.length));
    if (costume && costume.length) {
      $('recap-costume-list').innerHTML = costume.slice(0, 3).map(e => `
        <figure class="${e.rank === 1 ? 'first' : ''}">
          ${e.photo_url ? `<img src="${esc(e.photo_url)}" alt="">` : ''}
          <figcaption>${e.rank === 1 ? '🏆 ' : `#${e.rank} `}<b>${esc(e.costume)}</b><br>
            ${esc(e.nickname)} · ${e.votes} vote${e.votes === 1 ? '' : 's'}</figcaption>
        </figure>`).join('');
    }

    const lb = data.leaderboards || {};
    list('rb-djs', lb.top_djs, r => `${esc(r.nickname)} <span class="n">${r.count}</span>`);
    list('rb-favs', lb.crowd_favorites,
         r => `${esc(r.track_name)} <span class="n">♥ ${r.reaction_count}</span>`);
    list('rb-photos', lb.photographers, r => `${esc(r.nickname)} <span class="n">${r.count}</span>`);

    const photos = data.photos || [];
    if (!data.developed) {
      const at = new Date(data.develop_at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      $('recap-photos-note').textContent = `Still in the darkroom. They develop at ${at}.`;
    } else {
      $('recap-photos-note').textContent = photos.length ? `${photos.length} developed. Tap one to see it big.`
        : 'No photos developed.';
    }
    $('recap-photos').innerHTML = photos.map(p =>
      `<img loading="lazy" src="/api/wall/photo/${p.id}" data-by="${esc(p.by)}" alt="">`).join('');

    const link = $('recap-spotify');
    link.classList.toggle('hidden', !data.setlist_url);
    if (data.setlist_url) link.href = data.setlist_url;
    $('recap-setlist').innerHTML = (data.setlist || []).map(s => `
      <li><b>${esc(s.track_name)}</b> <span class="artist">— ${esc(s.artist)}</span>
        <span class="meta">${timeOf(s.played_at)}${s.requested_by ? ' · requested by ' + esc(s.requested_by) : ''}${s.hearts ? ' · ♥ ' + s.hearts : ''}</span></li>`).join('')
      || '<li class="n">Nothing has played yet.</li>';
  }

  async function load() {
    try {
      const res = await fetch('/api/recap', { credentials: 'same-origin' });
      if (res.status === 403) { $('recap-locked').classList.remove('hidden'); return; }
      if (!res.ok) return;
      $('recap-body').classList.remove('hidden');
      render(await res.json());
    } catch (e) {}
  }

  $('recap-photos').addEventListener('click', e => {
    if (e.target.tagName !== 'IMG') return;
    const v = $('recap-viewer');
    v.querySelector('img').src = e.target.src;
    v.querySelector('.recap-viewer-by').textContent = e.target.dataset.by ? '📸 ' + e.target.dataset.by : '';
    v.classList.remove('hidden');
  });
  $('recap-viewer').addEventListener('click', () => $('recap-viewer').classList.add('hidden'));

  load();
  setInterval(load, 60000);
})();
