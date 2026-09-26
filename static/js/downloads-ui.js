// Download buttons, nav badge and offline mode. Loaded from base.html.
// Everything fails silently when the /api/downloads or /api/online endpoints are missing.
(function() {
  'use strict';
  const U = window.NetvDLUtils;
  if (!U) return;

  const ICONS = {
    idle: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M5 20h14v-2H5v2zM19 9h-4V3H9v6H5l7 7 7-7z"/></svg>',
    done: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M9 16.2 4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>',
    error: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M12 2 1 21h22L12 2zm1 15h-2v-2h2v2zm0-4h-2V9h2v4z"/></svg>',
    paused: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>',
    active: '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true" class="dl-spin"><path d="M12 4V1L8 5l4 4V6a6 6 0 0 1 6 6c0 1-.25 1.97-.7 2.8l1.46 1.46A8 8 0 0 0 12 4zm-6 6c0-1 .25-1.97.7-2.8L5.24 5.74A8 8 0 0 0 12 20v3l4-4-4-4v3a6 6 0 0 1-6-6z"/></svg>',
  };
  const TITLES = {
    idle: 'Télécharger',
    active: 'Téléchargement en cours (voir Téléchargements)',
    paused: 'Téléchargement en pause (voir Téléchargements)',
    done: 'Téléchargé (voir Téléchargements)',
    error: 'Échec du téléchargement, réessayer',
  };

  // ---------- API helpers ----------
  async function getJson(url, opts) {
    try {
      const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  }

  function toast(msg) {
    let box = document.getElementById('dl-toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'dl-toast';
      box.setAttribute('role', 'status');
      box.className = 'dl-toast';
      document.body.appendChild(box);
    }
    box.textContent = msg;
    box.classList.add('show');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => box.classList.remove('show'), 3500);
  }

  // ---------- Buttons ----------
  const entries = new Set();          // {btn, kind, id, spec, state, pct}
  let apiAvailable = null;            // null unknown, true/false after first lookup
  let refreshTimer = null;
  let pollTimer = null;
  let lastLookup = {};                // key -> {status, download_id}

  function render(entry) {
    const { btn } = entry;
    const st = entry.state;
    btn.dataset.state = st;
    const ico = btn.querySelector('.dl-ico');
    const txt = btn.querySelector('.dl-txt');
    if (ico) ico.innerHTML = ICONS[st] || ICONS.idle;
    let pctText = '';
    if (st === 'active') pctText = entry.pct != null ? entry.pct + '%' : '';
    if (txt) {
      const label = entry.spec.label || {};
      txt.textContent = pctText || (st === 'done' ? (label.done || '') : st === 'idle' ? (label.idle || '') : '');
    }
    let title = TITLES[st];
    if (st === 'active' && entry.pct != null) title = 'Téléchargement en cours : ' + entry.pct + '%';
    btn.title = title;
    btn.setAttribute('aria-label', title + (entry.spec.title ? ' : ' + entry.spec.title : ''));
    btn.classList.toggle('dl-idle', st === 'idle');
    btn.hidden = apiAvailable !== true;
  }

  async function onClick(entry, ev) {
    ev.preventDefault();
    ev.stopPropagation();
    if (entry.state === 'done' || entry.state === 'active' || entry.state === 'paused') {
      location.href = '/downloads';
      return;
    }
    if (entry.busy) return;
    entry.busy = true;
    entry.btn.setAttribute('aria-busy', 'true');
    const body = { kind: entry.kind, id: String(entry.id) };
    if (entry.spec.sourceId) body.source_id = String(entry.spec.sourceId);
    if (entry.spec.title) body.title = entry.spec.title;
    if (entry.spec.seriesId) body.series_id = String(entry.spec.seriesId);
    if (entry.spec.ext) body.ext = String(entry.spec.ext);
    if (entry.spec.poster) body.poster = String(entry.spec.poster);
    const res = await getJson('/api/downloads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    entry.busy = false;
    entry.btn.removeAttribute('aria-busy');
    if (!res || !res.download) {
      toast('Impossible de lancer le téléchargement');
      return;
    }
    toast('Téléchargement ajouté' + (entry.spec.title ? ' : ' + entry.spec.title : ''));
    await refresh();
  }

  // spec: {title, sourceId, label:{idle,done}}
  function bind(btn, kind, id, spec) {
    if (btn._dlEntry) return btn._dlEntry;
    const entry = { btn, kind, id: String(id), spec: spec || {}, state: 'idle', pct: null, busy: false };
    btn._dlEntry = entry;
    btn.addEventListener('click', (ev) => onClick(entry, ev));
    entries.add(entry);
    render(entry);
    scheduleRefresh();
    return entry;
  }

  function makeButton(variant, spec) {
    const b = document.createElement('button');
    b.type = 'button';
    b.hidden = true;
    b.className = 'dl-btn dl-' + variant;
    b.innerHTML = '<span class="dl-ico"></span><span class="dl-txt"></span>';
    return b;
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 60); // batch everything rendered in the same tick
  }

  async function refresh() {
    // drop entries removed from the DOM
    for (const e of Array.from(entries)) if (!e.btn.isConnected) entries.delete(e);
    if (!entries.size) return;
    const keys = Array.from(new Set(Array.from(entries).map(e => U.lookupKey(e.kind, e.id))));
    const res = {};
    let ok = true;
    for (let i = 0; i < keys.length; i += 100) { // keep URLs short
      const part = await getJson(U.lookupUrl(keys.slice(i, i + 100)));
      if (part === null) { ok = false; break; }
      Object.assign(res, part);
    }
    apiAvailable = ok;
    if (!ok) { entries.forEach(render); return; }
    lastLookup = res;
    let anyActive = false;
    let needPct = false;
    entries.forEach(e => {
      const info = res[U.lookupKey(e.kind, e.id)];
      e.state = U.uiState(info && info.status);
      e.downloadId = info && info.download_id;
      if (e.state === 'active') {
        anyActive = true;
        needPct = true;
        e.pct = info && info.percent != null ? info.percent : e.pct;
      } else e.pct = null;
    });
    if (needPct) {
      const list = await getJson('/api/downloads');
      if (list && list.downloads) {
        const byId = new Map(list.downloads.map(d => [d.id, d]));
        entries.forEach(e => {
          const d = e.downloadId && byId.get(e.downloadId);
          if (d && e.state === 'active') e.pct = U.percent(d);
        });
      }
    }
    entries.forEach(render);
    document.dispatchEvent(new CustomEvent('netv:downloads-lookup', { detail: lastLookup }));
    clearTimeout(pollTimer);
    if (anyActive) pollTimer = setTimeout(refresh, 2000);
  }

  // ---------- Injection into rendered lists ----------
  function inject(root) {
    root = root || document;
    const scope = root.querySelectorAll ? root : document;

    // Movie cards (Movies browse, search results)
    scope.querySelectorAll('.movie-card[data-movie-id]').forEach(card => {
      if (card.querySelector('.dl-btn')) return;
      const btn = makeButton('card');
      card.appendChild(btn);
      bind(btn, 'movie', card.dataset.movieId, {
        title: card.dataset.movieName || '', ext: card.dataset.movieExt || '', poster: card.dataset.movieCover || '',
      });
    });

    // Favorites grid tiles (movies only, series are not downloadable as a whole)
    const cfg = window.FAVORITES_CONFIG;
    if (cfg && cfg.type === 'movies') {
      scope.querySelectorAll('.' + cfg.tileClass + '[data-id]').forEach(tile => {
        if (tile.querySelector('.dl-btn')) return;
        const id = tile.dataset.id;
        const fav = (cfg.favorites && cfg.favorites.movies && cfg.favorites.movies[id]) || {};
        tile.classList.add('relative', 'group');
        const btn = makeButton('card');
        tile.appendChild(btn);
        bind(btn, 'movie', id, { title: fav.name || '', ext: fav.ext || '', poster: fav.cover || '' });
      });
    }

    // Explicit placeholders: <div data-dl-kind data-dl-id data-dl-title data-dl-variant="card|inline">
    scope.querySelectorAll('[data-dl-kind][data-dl-id]').forEach(host => {
      if (host.querySelector('.dl-btn')) return;
      const variant = host.dataset.dlVariant || 'card';
      const btn = makeButton(variant);
      host.appendChild(btn);
      bind(btn, host.dataset.dlKind, host.dataset.dlId, {
        title: host.dataset.dlTitle || '',
        sourceId: host.dataset.dlSource || '',
        seriesId: host.dataset.dlSeries || '',
        ext: host.dataset.dlExt || '',
        poster: host.dataset.dlPoster || '',
        label: variant === 'inline' ? { idle: 'Télécharger', done: 'Téléchargé' } : undefined,
      });
    });
  }

  function watchDom() {
    let queued = false;
    const mo = new MutationObserver(() => {
      if (queued) return;
      queued = true;
      setTimeout(() => { queued = false; inject(); applyOfflineState(); }, 50);
    });
    const main = document.querySelector('main') || document.body;
    mo.observe(main, { childList: true, subtree: true });
  }

  // ---------- Nav badge ----------
  function setBadge(n) {
    const b = document.getElementById('dl-nav-badge');
    if (!b) return;
    b.textContent = n > 9 ? '9+' : String(n);
    b.hidden = n <= 0;
    const link = document.getElementById('dl-nav-link');
    if (link) link.title = n > 0 ? 'Téléchargements (' + n + ' en cours)' : 'Téléchargements';
  }
  async function pollBadge() {
    if (document.hidden) return;
    const res = await getJson('/api/downloads');
    if (res && res.downloads) setBadge(U.activeCount(res.downloads));
  }

  // ---------- Offline mode ----------
  let offline = false;

  function ensureBanner() {
    let bar = document.getElementById('offline-banner');
    if (bar) return bar;
    bar = document.createElement('div');
    bar.id = 'offline-banner';
    bar.setAttribute('role', 'status');
    bar.className = 'offline-banner';
    bar.hidden = true;
    bar.innerHTML = '<span>Hors ligne : seuls les téléchargements sont disponibles</span>' +
      '<a href="/downloads" class="offline-banner-link">Téléchargements</a>';
    document.body.appendChild(bar);
    return bar;
  }

  function applyOfflineState() {
    const bar = ensureBanner();
    bar.hidden = !offline;
    document.body.classList.toggle('is-offline', offline);
    const disable = (a, on) => {
      if (on) {
        if (a.dataset.dlDisabled) return;
        a.dataset.dlDisabled = '1';
        a.setAttribute('aria-disabled', 'true');
        a.classList.add('dl-disabled');
        a.tabIndex = -1;
      } else if (a.dataset.dlDisabled) {
        delete a.dataset.dlDisabled;
        a.removeAttribute('aria-disabled');
        a.classList.remove('dl-disabled');
        a.tabIndex = 0;
      }
    };
    // Play links and cards leading to a provider-backed page
    document.querySelectorAll('a[href^="/play/movie/"], a[href^="/play/series/"], .movie-card > a[href^="/movie/"], a[href^="/play/live/"]').forEach(a => {
      if (!offline) return disable(a, false);
      const m = a.getAttribute('href').match(/^\/play\/movie\/([^/?]+)/) ||
                a.getAttribute('href').match(/^\/movie\/([^/?]+)/);
      const e = a.getAttribute('href').match(/^\/play\/series\/([^/?]+)/);
      let info = null;
      if (m) info = lastLookup[U.lookupKey('movie', decodeURIComponent(m[1]))];
      else if (e) info = lastLookup[U.lookupKey('episode', decodeURIComponent(e[1]))];
      disable(a, !(info && info.status === 'done'));
    });
  }

  document.addEventListener('click', (ev) => {
    const a = ev.target.closest && ev.target.closest('a[data-dl-disabled]');
    if (a) { ev.preventDefault(); ev.stopPropagation(); toast('Indisponible hors ligne : non téléchargé'); }
  }, true);

  async function checkOnline() {
    let next = navigator.onLine === false;
    if (!next) {
      const res = await getJson('/api/online');
      if (res && typeof res.online === 'boolean') next = !res.online;
    }
    if (next !== offline) {
      offline = next;
      if (offline) await refreshLookupForOffline();
      applyOfflineState();
      document.dispatchEvent(new CustomEvent('netv:offline-change', { detail: { offline } }));
    }
  }
  async function refreshLookupForOffline() {
    // Collect play/detail ids on the page so we know which are downloaded.
    const keys = new Set();
    document.querySelectorAll('a[href^="/play/movie/"], a[href^="/movie/"]').forEach(a => {
      const m = a.getAttribute('href').match(/^\/(?:play\/)?movie\/([^/?]+)/);
      if (m) keys.add(U.lookupKey('movie', decodeURIComponent(m[1])));
    });
    document.querySelectorAll('a[href^="/play/series/"]').forEach(a => {
      const m = a.getAttribute('href').match(/^\/play\/series\/([^/?]+)/);
      if (m) keys.add(U.lookupKey('episode', decodeURIComponent(m[1])));
    });
    if (!keys.size) return;
    const arr = Array.from(keys);
    for (let i = 0; i < arr.length; i += 100) {
      const part = await getJson(U.lookupUrl(arr.slice(i, i + 100)));
      if (part) Object.assign(lastLookup, part);
    }
  }

  // ---------- Offline playback URL ----------
  // Normal player (audio/subtitle menus) served from the local file.
  async function playUrl(id) {
    return '/play/download/' + encodeURIComponent(id);
  }

  window.NetvDL = { bind, makeButton, refresh, inject, toast, playUrl, getJson, isOffline: () => offline };

  // ---------- Boot ----------
  function boot() {
    inject();
    watchDom();
    pollBadge();
    setInterval(pollBadge, 5000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollBadge(); });
    checkOnline();
    setInterval(checkOnline, 30000);
    window.addEventListener('online', checkOnline);
    window.addEventListener('offline', () => { checkOnline(); });
    document.addEventListener('netv:downloads-lookup', () => { if (offline) applyOfflineState(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
