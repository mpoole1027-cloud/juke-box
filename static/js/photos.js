/* ================================================================
   photos.js  –  Host photo review (/host/photos)
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
  const toast = document.createElement('div');
  toast.className = 'toast' + (type ? ' ' + type : '');
  toast.textContent = msg;
  $('toast-container').appendChild(toast);
  setTimeout(() => toast.remove(), 3100);
}

function jsonFetch(url, opts = {}) {
  opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  return fetch(url, opts);
}

// ----------------------------------------------------------------
// State
// ----------------------------------------------------------------
let party = '';
let statusFilter = 'pending';
let photos = [];          // what the grid shows
let viewerIndex = -1;     // index into photos while the viewer is open

const imgUrl = id => `/api/host/photos/${id}/image`;

// ----------------------------------------------------------------
// Auth
// ----------------------------------------------------------------
async function checkAuth() {
  try {
    const data = await (await fetch('/api/host/auth_check')).json();
    if (data.authenticated) return start();
  } catch (e) {}
  $('login-view').classList.remove('hidden');
}

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const res = await jsonFetch('/api/host/login', {
    method: 'POST', body: JSON.stringify({ password: $('login-password').value }),
  });
  if (res.ok) {
    $('login-view').classList.add('hidden');
    start();
  } else {
    const data = await res.json().catch(() => ({}));
    $('login-error').textContent = data.error || 'Login failed';
    $('login-error').classList.remove('hidden');
  }
});

// ----------------------------------------------------------------
// Loading
// ----------------------------------------------------------------
async function loadParties() {
  const cam = await (await jsonFetch('/api/host/camera')).json();
  const parties = cam.parties.slice();
  if (!parties.some(p => p.party_code === cam.current_party)) {
    parties.unshift({ party_code: cam.current_party, total: 0, pending: 0, approved: 0, rejected: 0 });
  }
  if (!party) {
    // Default to tonight, or the latest party with photos waiting.
    const waiting = parties.find(p => p.pending > 0);
    party = (waiting || parties[0]).party_code;
  }
  $('party-select').innerHTML = parties.map(p => {
    const when = p.first_shot ? ` · ${p.first_shot.slice(0, 10)}` : '';
    const now = p.party_code === cam.current_party ? ' (current)' : '';
    return `<option value="${escHtml(p.party_code)}"${p.party_code === party ? ' selected' : ''}>`
      + `${escHtml(p.party_code)}${now}${when} — ${p.total} photos</option>`;
  }).join('');

  const counts = parties.find(p => p.party_code === party) || {};
  document.querySelectorAll('.review-tab').forEach(tab => {
    const s = tab.dataset.status;
    tab.querySelector('span').textContent = s ? (counts[s] || 0) : (counts.total || 0);
  });
  $('approve-all-btn').disabled = !counts.pending;
  $('export-cmd').textContent = `.venv/bin/python deploy/export_photos.py --party ${party}`;
}

async function loadPhotos() {
  const q = new URLSearchParams({ party });
  if (statusFilter) q.set('status', statusFilter);
  const data = await (await jsonFetch('/api/host/photos?' + q)).json();
  photos = data.photos || [];
  renderGrid();
}

async function refresh() {
  try {
    await loadParties();
    await loadPhotos();
  } catch (e) {
    showToast("Couldn't load photos", 'error');
  }
}

function start() {
  $('review-view').classList.remove('hidden');
  refresh();
}

// ----------------------------------------------------------------
// Grid
// ----------------------------------------------------------------
function timeOf(p) {
  // created_at is SQLite UTC ("YYYY-MM-DD HH:MM:SS"); show it in local time.
  const d = new Date(p.created_at.replace(' ', 'T') + 'Z');
  return isNaN(d) ? p.created_at : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function renderGrid() {
  const grid = $('photo-grid');
  const empty = $('empty-msg');
  if (!photos.length) {
    grid.innerHTML = '';
    empty.textContent = statusFilter === 'pending'
      ? 'Nothing waiting for review.'
      : 'No photos here.';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  grid.innerHTML = photos.map((p, i) => `
    <figure class="photo-tile status-${p.status}" data-index="${i}">
      <img src="${imgUrl(p.id)}" loading="lazy" alt="Photo by ${escHtml(p.nickname)}">
      <figcaption>
        <span class="photo-meta">${escHtml(p.nickname)} · ${escHtml(timeOf(p))}</span>
        <span class="photo-actions">
          <button class="tile-btn reject" data-id="${p.id}" data-status="rejected" title="Reject"${p.status === 'rejected' ? ' disabled' : ''}>✕</button>
          <button class="tile-btn approve" data-id="${p.id}" data-status="approved" title="Approve"${p.status === 'approved' ? ' disabled' : ''}>✓</button>
        </span>
      </figcaption>
    </figure>
  `).join('');
}

$('photo-grid').addEventListener('click', e => {
  const btn = e.target.closest('.tile-btn');
  if (btn) {
    review([parseInt(btn.dataset.id, 10)], btn.dataset.status);
    return;
  }
  const tile = e.target.closest('.photo-tile');
  if (tile) openViewer(parseInt(tile.dataset.index, 10));
});

// ----------------------------------------------------------------
// Review actions
// ----------------------------------------------------------------
async function review(ids, status) {
  const res = await jsonFetch('/api/host/photos/review', {
    method: 'POST', body: JSON.stringify({ ids, status }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    showToast(data.error || 'Could not update', 'error');
    return false;
  }
  // Update in place so the viewer can keep going; drop it from a filtered view.
  photos.forEach(p => { if (ids.includes(p.id)) p.status = status; });
  if (statusFilter && statusFilter !== status) {
    photos = photos.filter(p => !ids.includes(p.id));
  }
  renderGrid();
  loadParties();
  return true;
}

$('approve-all-btn').addEventListener('click', async () => {
  const q = new URLSearchParams({ party, status: 'pending' });
  const pending = (await (await jsonFetch('/api/host/photos?' + q)).json()).photos || [];
  if (!pending.length) return;
  if (!confirm(`Approve all ${pending.length} pending photos without looking at each one?`)) return;
  if (await review(pending.map(p => p.id), 'approved')) {
    showToast(`Approved ${pending.length} photos`, 'success');
    loadPhotos();
  }
});

document.querySelectorAll('.review-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.review-tab').forEach(t => t.classList.toggle('active', t === tab));
    statusFilter = tab.dataset.status;
    closeViewer();
    loadPhotos();
  });
});

$('party-select').addEventListener('change', e => {
  party = e.target.value;
  closeViewer();
  refresh();
});

// ----------------------------------------------------------------
// Full-size viewer
// ----------------------------------------------------------------
function openViewer(i) {
  if (!photos.length) return closeViewer();
  viewerIndex = Math.max(0, Math.min(i, photos.length - 1));
  const p = photos[viewerIndex];
  $('viewer-img').src = imgUrl(p.id);
  $('viewer-meta').textContent =
    `${viewerIndex + 1} / ${photos.length} · ${p.nickname} · ${timeOf(p)} · ${p.status}`;
  $('viewer-approve').disabled = p.status === 'approved';
  $('viewer-reject').disabled = p.status === 'rejected';
  $('viewer').classList.remove('hidden');
}

function closeViewer() {
  viewerIndex = -1;
  $('viewer').classList.add('hidden');
  $('viewer-img').removeAttribute('src');
}

async function reviewCurrent(status) {
  const p = photos[viewerIndex];
  if (!p) return;
  const before = photos.length;
  if (!(await review([p.id], status))) return;
  // In a filtered view the photo left the list, so the same index is now the next one.
  const next = photos.length < before ? viewerIndex : viewerIndex + 1;
  if (next >= photos.length) {
    closeViewer();
    if (!photos.length) showToast('All caught up! 🎉', 'success');
  } else {
    openViewer(next);
  }
}

$('viewer-approve').addEventListener('click', () => reviewCurrent('approved'));
$('viewer-reject').addEventListener('click', () => reviewCurrent('rejected'));
$('viewer-close').addEventListener('click', closeViewer);
$('viewer-prev').addEventListener('click', () => openViewer(viewerIndex - 1));
$('viewer-next').addEventListener('click', () => openViewer(viewerIndex + 1));

document.addEventListener('keydown', e => {
  if (viewerIndex < 0) return;
  const key = e.key.toLowerCase();
  if (key === 'escape') closeViewer();
  else if (key === 'arrowleft') openViewer(viewerIndex - 1);
  else if (key === 'arrowright') openViewer(viewerIndex + 1);
  else if (key === 'a' && !$('viewer-approve').disabled) reviewCurrent('approved');
  else if (key === 'r' && !$('viewer-reject').disabled) reviewCurrent('rejected');
});

checkAuth();
