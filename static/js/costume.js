/* ================================================================
   costume.js  –  Costume contest for guests

   Enter with a photo and what you came as, then vote by tapping
   someone else's tile. Vote counts stay hidden until the host closes
   voting and the TV reveals the winner. app.js calls Costume.update()
   with every poll.
   ================================================================ */

const Costume = (() => {
  const $ = id => document.getElementById(id);
  const MEDALS = ['🥇', '🥈', '🥉'];
  // Photos are cropped square on the phone before upload: smaller and
  // faster on party wifi, and it turns iPhone HEIC into a JPEG.
  const PHOTO_EDGE = 1024;

  let state = { phase: 'off' };
  let lastListKey = '';
  let busy = false;
  let pickedPhoto = null;      // Blob waiting to be sent with the entry
  let previewUrl = null;       // object URL of pickedPhoto, for the preview

  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ----------------------------------------------------------------
  // Rendering
  // ----------------------------------------------------------------
  function render() {
    const phase = state.phase;
    $('costume-card').classList.toggle('hidden', phase === 'off');
    $('costume-open').classList.toggle('hidden', phase !== 'open');
    $('costume-closed').classList.toggle('hidden', phase !== 'closed');
    if (phase === 'open') renderOpen();
    if (phase === 'closed') renderClosed();
  }

  function tile(e, { action = '', badge = '', extra = '', classes = '' } = {}) {
    const photo = e.photo_url
      ? `<img src="${esc(e.photo_url)}" alt="" loading="lazy">`
      : '<span class="costume-noimg">🎭</span>';
    const tag = action ? 'button' : 'div';
    return `
      <${tag} class="costume-tile ${classes}" ${action}>
        <span class="costume-frame">${photo}</span>
        ${badge}
        <span class="costume-tile-name">${esc(e.costume)}</span>
        <span class="costume-tile-who">${esc(e.nickname)}</span>
        ${extra}
      </${tag}>`;
  }

  function renderOpen() {
    const mine = state.my_entry;
    const input = $('costume-input');
    // Never overwrite what the guest is typing.
    if (document.activeElement !== input) input.value = mine ? mine.costume : '';
    $('costume-entry-label').textContent = mine
      ? "You're in the running! Tap the photo or name to change them."
      : 'What did you come as? Snap a photo and enter the contest:';
    $('costume-save-btn').textContent = mine ? 'SAVE' : 'ENTER';
    $('costume-withdraw-btn').classList.toggle('hidden', !mine);
    renderPreview();

    const entries = state.entries || [];
    // The grid only changes when entries or my vote do; skip rebuilding it
    // on every poll so a tap never lands on a tile mid-replace.
    const key = JSON.stringify([entries, state.my_vote]);
    if (key === lastListKey) return;
    lastListKey = key;

    const grid = $('costume-entries');
    if (!entries.length) {
      grid.innerHTML = '<div class="costume-empty">No costumes entered yet. Be the first!</div>';
      return;
    }
    grid.innerHTML = entries.map(e => {
      if (e.is_mine) {
        return tile(e, { classes: 'mine', badge: '<span class="costume-badge">YOU</span>' });
      }
      const voted = e.id === state.my_vote;
      return tile(e, {
        classes: voted ? 'voted' : '',
        action: `type="button" data-id="${e.id}" aria-pressed="${voted}" ` +
                `aria-label="Vote for ${esc(e.costume)} by ${esc(e.nickname)}"`,
        badge: voted ? '<span class="costume-badge">✓ YOUR VOTE</span>' : '',
      });
    }).join('');
  }

  function renderPreview() {
    const mine = state.my_entry;
    const src = previewUrl || (mine && mine.photo_url) || '';
    const img = $('costume-photo-preview');
    if (img.getAttribute('src') !== src) {
      if (src) img.src = src; else img.removeAttribute('src');
    }
    img.classList.toggle('hidden', !src);
    $('costume-photo-hint').classList.toggle('hidden', !!src);
    $('costume-photo-btn').classList.toggle('has-photo', !!src);
  }

  function renderClosed() {
    const results = state.results || [];
    const key = JSON.stringify(results);
    if (key !== lastListKey) {
      lastListKey = key;
      $('costume-results').innerHTML = results.length
        ? results.map(r => tile(r, {
            classes: r.rank === 1 ? 'winner' : '',
            badge: `<span class="costume-badge rank">${r.rank <= 3 ? MEDALS[r.rank - 1] : '#' + r.rank}</span>`,
            extra: `<span class="costume-tile-votes">${r.votes} vote${r.votes === 1 ? '' : 's'}</span>`,
          })).join('')
        : '<div class="costume-empty">Nobody entered this time.</div>';
    }

    const mine = state.my_entry && results.find(r => r.id === state.my_entry.id);
    let msg = 'Voting is closed. Here are the results!';
    if (mine && mine.rank === 1) msg = '👑 You won best costume! Take a bow.';
    else if (mine) msg = `Voting is closed. You placed #${mine.rank} with ${mine.votes} vote${mine.votes === 1 ? '' : 's'}.`;
    $('costume-my-result').textContent = msg;
  }

  // ----------------------------------------------------------------
  // Photo
  // ----------------------------------------------------------------
  function loadImage(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('decode')); };
      img.src = url;
    });
  }

  // Centre-crop to a square JPEG. Browsers apply the photo's EXIF
  // rotation when drawing, so it comes out the right way up.
  async function squareJpeg(file) {
    const img = await loadImage(file);
    const side = Math.min(img.naturalWidth, img.naturalHeight);
    const edge = Math.min(PHOTO_EDGE, side);
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = edge;
    canvas.getContext('2d').drawImage(img,
      (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side,
      0, 0, edge, edge);
    return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.88));
  }

  async function photoPicked() {
    const file = $('costume-photo-input').files[0];
    $('costume-photo-input').value = '';
    if (!file) return;
    let blob = null;
    try { blob = await squareJpeg(file); } catch (e) {}
    // Couldn't decode it here: send it as-is and let the jukebox decide.
    pickedPhoto = blob || file;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = blob ? URL.createObjectURL(blob) : null;
    renderPreview();
    if (!blob) showToast('Photo added', 'success');
    // Already entered: a new photo saves straight away.
    if (state.my_entry && $('costume-input').value.trim()) saveEntry();
    else $('costume-input').focus();
  }

  function clearPicked() {
    pickedPhoto = null;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  }

  // ----------------------------------------------------------------
  // Actions
  // ----------------------------------------------------------------
  async function request(url, opts, okMsg) {
    if (busy) return null;
    busy = true;
    try {
      // Not apiFetch: entries are multipart, so no JSON content type.
      const res = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts));
      let data = {};
      try { data = await res.json(); } catch (e) {}
      if (!res.ok) {
        if (data.code === 'party_code_required') showJoinGate(true);
        showToast(data.error || 'Something went wrong. Try again.', 'error');
        return null;
      }
      if (okMsg) showToast(okMsg, 'success');
      return data;
    } catch (e) {
      showToast("Couldn't reach the jukebox.", 'error');
      return null;
    } finally {
      busy = false;
      pollStatus();
    }
  }

  function json(method, body) {
    return { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) };
  }

  async function saveEntry(ev) {
    if (ev) ev.preventDefault();
    const costume = $('costume-input').value.trim();
    if (!costume) { showToast('Tell us what you came as!', 'error'); return; }
    if (!pickedPhoto && !(state.my_entry && state.my_entry.photo_url)) {
      showToast('Add a photo of your costume first 📷', 'error');
      $('costume-photo-btn').focus();
      return;
    }
    const form = new FormData();
    form.append('costume', costume);
    if (pickedPhoto) form.append('photo', pickedPhoto, 'costume.jpg');
    const renaming = !!state.my_entry;
    const btn = $('costume-save-btn');
    btn.disabled = true;
    const data = await request('/api/costume/entry', { method: 'POST', body: form },
      renaming ? 'Entry updated' : "You're in the costume contest! 🎃");
    btn.disabled = false;
    if (data) {
      state.my_entry = data.entry;
      clearPicked();
      $('costume-input').blur();
      render();
    }
  }

  async function withdraw() {
    if (!confirm('Withdraw from the costume contest? Your photo is deleted and any votes you got are lost.')) return;
    const data = await request('/api/costume/entry', json('DELETE'), 'Entry withdrawn');
    if (data) {
      state.my_entry = null;
      clearPicked();
      render();
    }
  }

  async function vote(id) {
    const data = await request('/api/costume/vote', json('POST', { entry_id: id }), 'Vote saved 🗳️');
    if (data) {
      state.my_vote = data.my_vote;
      render();
    }
  }

  // ----------------------------------------------------------------
  // Wiring
  // ----------------------------------------------------------------
  $('costume-entry-form').addEventListener('submit', saveEntry);
  $('costume-withdraw-btn').addEventListener('click', withdraw);
  $('costume-photo-btn').addEventListener('click', () => $('costume-photo-input').click());
  $('costume-photo-input').addEventListener('change', photoPicked);
  $('costume-entries').addEventListener('click', ev => {
    const btn = ev.target.closest('.costume-tile[data-id]');
    if (btn && !busy) vote(Number(btn.dataset.id));
  });

  return {
    // Called from app.js with the `costume` block of every /api/status poll.
    update(next) {
      if (!next) return;
      if (next.phase !== state.phase) lastListKey = '';
      state = next;
      render();
    },
  };
})();
