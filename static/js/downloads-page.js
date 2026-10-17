// /downloads page: fully client-side, driven by /api/downloads (poll every 2s while active).
(function() {
  'use strict';
  const U = window.NetvDLUtils;
  const list = document.getElementById('dl-list');
  if (!U || !list) return;
  const empty = document.getElementById('dl-empty');
  const errBox = document.getElementById('dl-error');
  const dirEl = document.getElementById('dl-dir');
  const freeEl = document.getElementById('dl-free');
  let timer = null;
  let inFlight = false;
  const rows = new Map(); // id -> {li, refs}

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  const BTN = 'px-3 py-1.5 rounded text-sm bg-gray-700 hover:bg-gray-600 focusable';

  function makeRow(d) {
    const li = document.createElement('li');
    li.className = 'bg-gray-800 rounded-lg p-3 flex gap-3 items-start';
    li.dataset.id = d.id;
    li.innerHTML =
      '<div class="w-12 h-[4.5rem] flex-shrink-0 rounded bg-gray-700 overflow-hidden"><img alt="" class="w-full h-full object-cover" hidden></div>' +
      '<div class="flex-1 min-w-0">' +
        '<div class="flex flex-wrap items-baseline justify-between gap-x-3">' +
          '<div class="font-medium truncate min-w-0" data-r="title"></div>' +
          '<div class="text-xs text-gray-400" data-r="status"></div>' +
        '</div>' +
        '<div class="mt-2 h-2 rounded bg-gray-700 overflow-hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" data-r="bar"><div class="h-full bg-blue-500 transition-all" data-r="fill"></div></div>' +
        '<div class="mt-1 text-xs text-gray-400 flex flex-wrap gap-x-4" data-r="meta"></div>' +
        '<div class="mt-1 text-xs text-red-400 break-words" data-r="err"></div>' +
        '<div class="mt-2 flex flex-wrap gap-2" data-r="actions"></div>' +
      '</div>';
    const refs = {};
    li.querySelectorAll('[data-r]').forEach(el => { refs[el.dataset.r] = el; });
    refs.img = li.querySelector('img');
    return { li, refs, sig: '' };
  }

  function actionsFor(d) {
    const a = [];
    if (d.status === 'done') a.push(['play', 'Lire']);
    // Enough data to start watching while the rest downloads
    else if (['downloading', 'queued', 'paused'].includes(d.status) && d.bytes_done > 30 * 1024 * 1024) a.push(['watch', 'Regarder maintenant']);
    if (d.status === 'downloading' || d.status === 'queued') a.push(['pause', 'Pause']);
    if (d.status === 'paused') a.push(['resume', 'Reprendre']);
    if (d.status === 'error') a.push(['resume', 'Réessayer']);
    if (d.status === 'queued' || d.status === 'downloading' || d.status === 'paused') a.push(['cancel', 'Annuler']);
    if (d.status === 'done' || d.status === 'error' || d.status === 'cancelled') a.push(['delete', 'Supprimer']);
    return a;
  }

  function update(row, d) {
    const r = row.refs;
    r.title.textContent = d.title || d.filename || d.id;
    r.title.title = d.filename || '';
    if (d.poster) { r.img.src = d.poster; r.img.hidden = false; }
    const pct = d.status === 'done' ? 100 : U.percent(d);
    r.status.textContent = U.statusLabel(d.status) + (pct != null && d.status !== 'done' ? ' ' + pct + '%' : '') +
      (d.processing ? ' · préparation de l’audio…' : '');
    r.bar.setAttribute('aria-valuenow', pct == null ? '' : String(pct));
    r.fill.style.width = (pct == null ? (d.status === 'downloading' ? 8 : 0) : pct) + '%';
    r.fill.className = 'h-full transition-all ' + (d.status === 'done' ? 'bg-green-500' :
      d.status === 'error' ? 'bg-red-500' : d.status === 'paused' ? 'bg-yellow-500' : 'bg-blue-500') +
      (pct == null && d.status === 'downloading' ? ' animate-pulse' : '');
    const parts = [];
    const done = U.formatBytes(d.bytes_done), total = U.formatBytes(d.bytes_total);
    if (d.status === 'done') { if (total || done) parts.push(total || done); }
    else if (total) parts.push((done || '0 o') + ' / ' + total);
    else if (done) parts.push(done);
    if (d.status === 'downloading') {
      const sp = U.formatSpeed(d.speed_bps); if (sp) parts.push(sp);
      const eta = U.formatEta(d.eta_seconds); if (eta) parts.push('reste ' + eta);
    }
    r.meta.innerHTML = parts.map(p => '<span>' + esc(p) + '</span>').join('');
    r.err.textContent = d.status === 'error' && d.error ? d.error : '';
    // Rebuild the buttons only when the set changes (keeps focus stable while polling)
    const acts = actionsFor(d);
    const sig = acts.map(x => x[0]).join(',');
    if (sig !== row.sig) {
      row.sig = sig;
      r.actions.innerHTML = acts.map(([k, label]) =>
        '<button type="button" data-act="' + k + '" class="' + BTN + (k === 'delete' || k === 'cancel' ? ' hover:bg-red-700' : '') +
        (k === 'play' || k === 'watch' ? ' bg-blue-600 hover:bg-blue-700' : '') + '" tabindex="0">' + label + '</button>').join('');
    }
  }

  async function act(id, kind) {
    const enc = encodeURIComponent(id);
    if (kind === 'play') {
      location.href = await window.NetvDL.playUrl(id);
      return;
    }
    if (kind === 'watch') {
      location.href = '/play/partial/' + enc;
      return;
    }
    if (kind === 'delete') {
      const row = rows.get(id);
      const name = row ? row.refs.title.textContent : '';
      if (!confirm('Supprimer ce téléchargement' + (name ? ' (' + name + ')' : '') + ' et le fichier associé ?')) return;
    }
    if (kind === 'cancel' && !confirm('Annuler ce téléchargement ?')) return;
    const url = '/api/downloads/' + enc + (kind === 'delete' ? '' : '/' + kind);
    try {
      const r = await fetch(url, { method: kind === 'delete' ? 'DELETE' : 'POST', credentials: 'same-origin' });
      if (!r.ok) throw new Error(r.status);
    } catch (e) {
      window.NetvDL && window.NetvDL.toast('Action impossible');
    }
    load();
  }

  list.addEventListener('click', (ev) => {
    const b = ev.target.closest('button[data-act]');
    if (!b) return;
    const li = b.closest('li[data-id]');
    if (li) act(li.dataset.id, b.dataset.act);
  });

  function sortItems(items) {
    const rank = { downloading: 0, queued: 1, paused: 2, error: 3, done: 4, cancelled: 5 };
    return items.slice().sort((a, b) =>
      (rank[a.status] ?? 9) - (rank[b.status] ?? 9) || String(b.created_at || '').localeCompare(String(a.created_at || '')));
  }

  function renderAll(data) {
    dirEl.textContent = data.dir || 'inconnu';
    freeEl.textContent = data.free_bytes != null ? U.formatBytes(data.free_bytes) : 'inconnu';
    const items = sortItems(data.downloads || []);
    empty.classList.toggle('hidden', items.length > 0);
    const seen = new Set();
    items.forEach((d, i) => {
      seen.add(d.id);
      let row = rows.get(d.id);
      if (!row) { row = makeRow(d); rows.set(d.id, row); }
      update(row, d);
      if (list.children[i] !== row.li) list.insertBefore(row.li, list.children[i] || null);
    });
    for (const [id, row] of Array.from(rows)) {
      if (!seen.has(id)) { row.li.remove(); rows.delete(id); }
    }
  }

  async function load() {
    if (inFlight) return;
    inFlight = true;
    clearTimeout(timer);
    let data = null;
    try {
      const r = await fetch('/api/downloads', { credentials: 'same-origin' });
      if (r.ok) data = await r.json();
    } catch (e) { /* offline server or missing API */ }
    inFlight = false;
    errBox.classList.toggle('hidden', !!data);
    if (data) {
      renderAll(data);
      const active = (data.downloads || []).some(d => U.isActive(d.status) || d.processing);
      timer = setTimeout(load, active ? 2000 : 8000);
    } else {
      timer = setTimeout(load, 8000);
    }
  }

  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  load();
})();
