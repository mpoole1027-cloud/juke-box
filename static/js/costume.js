/* ================================================================
   costume.js  –  Costume contest for guests

   Enter with what you came as, then vote for someone else's costume.
   Vote counts stay hidden until the host closes voting and the TV
   reveals the winner. app.js calls Costume.update() with every poll.
   ================================================================ */

const Costume = (() => {
  const $ = id => document.getElementById(id);
  const MEDALS = ['🥇', '🥈', '🥉'];

  let state = { phase: 'off' };
  let lastListKey = '';
  let busy = false;

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

  function renderOpen() {
    const mine = state.my_entry;
    const input = $('costume-input');
    // Never overwrite what the guest is typing.
    if (document.activeElement !== input) input.value = mine ? mine.costume : '';
    $('costume-entry-label').textContent = mine
      ? "You're in the running as:"
      : 'What did you come as? Enter the contest:';
    $('costume-save-btn').textContent = mine ? 'RENAME' : 'ENTER';
    $('costume-withdraw-btn').classList.toggle('hidden', !mine);

    const entries = state.entries || [];
    // The list only changes when entries or my vote do; skip rebuilding it
    // on every poll so a tap never lands on a button mid-replace.
    const key = JSON.stringify([entries, state.my_vote]);
    if (key === lastListKey) return;
    lastListKey = key;

    const list = $('costume-entries');
    if (!entries.length) {
      list.innerHTML = '<div class="costume-empty">No costumes entered yet. Be the first!</div>';
      return;
    }
    list.innerHTML = entries.map(e => {
      const voted = e.id === state.my_vote;
      const action = e.is_mine
        ? '<span class="costume-you">YOU</span>'
        : `<button class="btn btn-sm ${voted ? 'btn-primary' : 'btn-ghost'} costume-vote-btn"
             type="button" data-id="${e.id}" aria-pressed="${voted}">${voted ? '✓ VOTED' : 'VOTE'}</button>`;
      return `
      <div class="costume-entry${voted ? ' voted' : ''}">
        <div class="costume-entry-body">
          <div class="costume-name">${esc(e.costume)}</div>
          <div class="costume-who">${esc(e.nickname)}</div>
        </div>
        ${action}
      </div>`;
    }).join('');
  }

  function renderClosed() {
    const results = state.results || [];
    const key = JSON.stringify(results);
    if (key !== lastListKey) {
      lastListKey = key;
      $('costume-results').innerHTML = results.length
        ? results.map(r => `
          <div class="costume-entry${r.rank === 1 ? ' winner' : ''}">
            <div class="costume-rank">${r.rank <= 3 ? MEDALS[r.rank - 1] : r.rank}</div>
            <div class="costume-entry-body">
              <div class="costume-name">${esc(r.costume)}</div>
              <div class="costume-who">${esc(r.nickname)}</div>
            </div>
            <div class="costume-votes">${r.votes} vote${r.votes === 1 ? '' : 's'}</div>
          </div>`).join('')
        : '<div class="costume-empty">Nobody entered this time.</div>';
    }

    const mine = state.my_entry && results.find(r => r.id === state.my_entry.id);
    let msg = 'Voting is closed. Here are the results!';
    if (mine && mine.rank === 1) msg = '👑 You won best costume! Take a bow.';
    else if (mine) msg = `Voting is closed. You placed #${mine.rank} with ${mine.votes} vote${mine.votes === 1 ? '' : 's'}.`;
    $('costume-my-result').textContent = msg;
  }

  // ----------------------------------------------------------------
  // Actions
  // ----------------------------------------------------------------
  async function send(url, opts, okMsg) {
    if (busy) return null;
    busy = true;
    try {
      const res = await apiFetch(url, opts);
      let data = {};
      try { data = await res.json(); } catch (e) {}
      if (!res.ok) {
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

  async function saveEntry(ev) {
    ev.preventDefault();
    const input = $('costume-input');
    const costume = input.value.trim();
    if (!costume) { showToast('Tell us what you came as!', 'error'); return; }
    const renaming = !!state.my_entry;
    const data = await send('/api/costume/entry', {
      method: 'POST', body: JSON.stringify({ costume }),
    }, renaming ? 'Entry renamed' : "You're in the costume contest! 🎃");
    if (data) {
      state.my_entry = data.entry;
      input.blur();
    }
  }

  async function withdraw() {
    if (!confirm('Withdraw from the costume contest? Any votes you got are lost.')) return;
    const data = await send('/api/costume/entry', { method: 'DELETE' }, 'Entry withdrawn');
    if (data) state.my_entry = null;
  }

  async function vote(id) {
    const data = await send('/api/costume/vote', {
      method: 'POST', body: JSON.stringify({ entry_id: id }),
    }, 'Vote saved 🗳️');
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
  $('costume-entries').addEventListener('click', ev => {
    const btn = ev.target.closest('.costume-vote-btn');
    if (btn && !btn.disabled) vote(Number(btn.dataset.id));
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
