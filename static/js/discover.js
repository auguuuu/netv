// Discovery rows for the Movies and Series landing pages.
// Data: GET /api/discover?kind=movies|series&lang=
// Cards reuse the movie-card / series-card markup and data attributes so
// downloads-ui.js (download button, offline dimming) and the favorite star keep
// working. Only a few rows are rendered at first ("Afficher plus" adds more),
// and each row ends with a "Voir plus" card linking to the full listing.
(function () {
  'use strict';
  const host = document.getElementById('discover-rows');
  if (!host) return;
  const sel = document.getElementById('lang-select');
  const kind = host.dataset.kind === 'series' ? 'series' : 'movies';
  const browseUrl = kind === 'series' ? '/series?sort=newest' : '/vod?sort=newest';
  const skeleton = host.innerHTML;
  const FIRST_ROWS = 4;  // rendered on load
  const MORE_ROWS = 4;   // added per "Afficher plus" click
  let reqId = 0;
  let pending = [];      // rows not rendered yet

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function seriesCardHtml(m) {
    const tip = m.name + (m.rating ? '\n★ ' + m.rating : '');
    const jsArgs = [m.series_id, m.name, m.poster || ''].map(v => JSON.stringify(v)).join(', ');
    return `<div class="series-card disc-card relative group" data-series-id="${esc(m.series_id)}" data-series-name="${esc(m.name)}"
        data-series-cover="${esc(m.poster || '')}">
      <a href="/series/${encodeURIComponent(m.series_id)}"
         class="block bg-gray-800 rounded-lg overflow-hidden hover:ring-2 hover:ring-blue-500 focus:ring-2 focus:ring-blue-500 focusable transition-all"
         tabindex="0" data-nav="disc" title="${esc(tip)}">
        <div class="disc-poster bg-gray-700">
          ${m.poster ? `<img src="${esc(m.poster)}" alt="" class="w-full h-full object-cover disc-loading" loading="lazy" decoding="async"
               onload="this.classList.remove('disc-loading')" onerror="this.style.display='none'">` : ''}
        </div>
        <div class="p-2">
          <div class="text-sm line-clamp-2 leading-tight">${esc(m.name)}</div>
          ${m.rating ? `<div class="text-xs text-yellow-400 mt-1">★ ${esc(m.rating)}</div>` : ''}
        </div>
      </a>
      <button type="button" class="fav-btn absolute top-2 right-2 w-8 h-8 rounded-full bg-black/50 text-xl opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity z-10"
              aria-label="Favori" onclick='toggleFavorite(${esc(jsArgs)})'>☆</button>
    </div>`;
  }

  // Last card of a row: link to the paginated listing behind the row.
  function moreCardHtml(row) {
    return `<div class="disc-card disc-more-card">
      <a href="${esc(row.more)}" class="disc-more focusable" tabindex="0" data-nav="disc"
         aria-label="Voir plus : ${esc(row.title)}">
        <span class="disc-more-icon" aria-hidden="true">&#8250;</span>
        <span>Voir plus</span>
      </a>
    </div>`;
  }

  function cardHtml(m) {
    const ext = m.ext || 'mkv';
    const tip = m.name + (m.rating ? '\n★ ' + m.rating : '');
    const jsArgs = [m.stream_id, m.name, m.poster || '', ext].map(v => JSON.stringify(v)).join(', ');
    return `<div class="movie-card disc-card relative group" data-movie-id="${esc(m.stream_id)}" data-movie-name="${esc(m.name)}"
        data-movie-cover="${esc(m.poster || '')}" data-movie-ext="${esc(ext)}">
      <a href="/movie/${encodeURIComponent(m.stream_id)}"
         class="block bg-gray-800 rounded-lg overflow-hidden hover:ring-2 hover:ring-blue-500 focus:ring-2 focus:ring-blue-500 focusable transition-all"
         tabindex="0" data-nav="disc" title="${esc(tip)}">
        <div class="disc-poster bg-gray-700">
          ${m.poster ? `<img src="${esc(m.poster)}" alt="" class="w-full h-full object-cover disc-loading" loading="lazy" decoding="async"
               onload="this.classList.remove('disc-loading')" onerror="this.style.display='none'">` : ''}
        </div>
        <div class="p-2">
          <div class="text-sm line-clamp-2 leading-tight">${esc(m.name)}</div>
          ${m.rating ? `<div class="text-xs text-yellow-400 mt-1">★ ${esc(m.rating)}</div>` : ''}
        </div>
      </a>
      <button type="button" class="fav-btn absolute top-2 right-2 w-8 h-8 rounded-full bg-black/50 text-xl opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity z-10"
              aria-label="Favori" onclick='toggleFavorite(${esc(jsArgs)})'>☆</button>
    </div>`;
  }

  function rowHtml(row) {
    return `<section class="disc-row" data-row="${esc(row.id)}" aria-label="${esc(row.title)}">
      <h3 class="disc-title">${row.more ? `<a href="${esc(row.more)}" class="hover:underline" tabindex="-1">${esc(row.title)}</a>` : esc(row.title)}</h3>
      <div class="disc-viewport">
        <button type="button" class="disc-arrow disc-prev" aria-label="Précédent" tabindex="-1" disabled>&#8249;</button>
        <div class="disc-track" role="list">${row.items.map(kind === 'series' ? seriesCardHtml : cardHtml).join('')}${row.more ? moreCardHtml(row) : ''}</div>
        <button type="button" class="disc-arrow disc-next" aria-label="Suivant" tabindex="-1">&#8250;</button>
      </div>
    </section>`;
  }

  function wireRow(section) {
    const track = section.querySelector('.disc-track');
    const prev = section.querySelector('.disc-prev');
    const next = section.querySelector('.disc-next');
    function update() {
      prev.disabled = track.scrollLeft < 8;
      next.disabled = track.scrollLeft + track.clientWidth >= track.scrollWidth - 8;
    }
    function page(dir) {
      track.scrollBy({ left: dir * Math.max(200, track.clientWidth * 0.85), behavior: 'smooth' });
    }
    prev.addEventListener('click', () => page(-1));
    next.addEventListener('click', () => page(1));
    track.addEventListener('scroll', () => requestAnimationFrame(update), { passive: true });
    window.addEventListener('resize', update);
    update();
  }

  // Arrow keys: left/right inside a row, up/down to the card at the same index in the next row.
  host.addEventListener('keydown', (e) => {
    const card = e.target.closest && e.target.closest('a[data-nav="disc"]');
    if (!card || e.altKey || e.ctrlKey || e.metaKey) return;
    const track = card.closest('.disc-track');
    const cards = Array.from(track.querySelectorAll('a[data-nav="disc"]'));
    const i = cards.indexOf(card);
    let target = null;
    if (e.key === 'ArrowRight') target = cards[i + 1];
    else if (e.key === 'ArrowLeft') target = cards[i - 1];
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const rows = Array.from(host.querySelectorAll('.disc-row:not(.disc-skel) .disc-track'));
      const other = rows[rows.indexOf(track) + (e.key === 'ArrowDown' ? 1 : -1)];
      if (other) {
        const oc = other.querySelectorAll('a[data-nav="disc"]');
        target = oc[Math.min(i, oc.length - 1)];
      } else return;  // let the global handler move to the toolbar / favorites
    } else return;
    e.preventDefault();
    e.stopPropagation();
    if (target) {
      target.focus({ preventScroll: true });
      target.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
    }
  });

  async function load() {
    const my = ++reqId;
    const lang = sel ? sel.value : '';
    host.innerHTML = skeleton;
    host.setAttribute('aria-busy', 'true');
    let data = null;
    try {
      const r = await fetch('/api/discover?kind=' + kind + '&lang=' + encodeURIComponent(lang), { credentials: 'same-origin' });
      if (r.ok) data = await r.json();
    } catch (e) { /* offline or server error, handled below */ }
    if (my !== reqId) return;  // a newer request superseded this one
    host.removeAttribute('aria-busy');
    if (!data) {
      host.innerHTML = `<p class="disc-empty">Impossible de charger les suggestions. <a class="underline" href="${browseUrl}">Tout parcourir</a></p>`;
      return;
    }
    if (data.loading) {
      host.innerHTML = '<p class="disc-empty">Chargement du catalogue...</p>';
      setTimeout(load, 3000);
      return;
    }
    if (!data.rows.length) {
      host.innerHTML = '<p class="disc-empty">Aucune suggestion pour cette langue. Utilisez « Tout parcourir ».</p>';
      return;
    }
    host.innerHTML = '';
    pending = data.rows.slice();
    renderMore(FIRST_ROWS);
  }

  // Append the next `n` pending rows, then the "Afficher plus" button if any remain.
  function renderMore(n) {
    const old = host.querySelector('.disc-more-rows');
    if (old) old.remove();
    const batch = pending.splice(0, n);
    const tmp = document.createElement('div');
    tmp.innerHTML = batch.map(rowHtml).join('');
    const added = Array.from(tmp.children);
    added.forEach(el => host.appendChild(el));
    added.forEach(wireRow);
    if (pending.length) {
      const wrap = document.createElement('div');
      wrap.className = 'disc-more-rows';
      wrap.innerHTML = `<button type="button" class="px-6 py-2 rounded bg-gray-700 hover:bg-gray-600 focusable" tabindex="0">
        Afficher plus de catégories (${pending.length})</button>`;
      wrap.firstElementChild.addEventListener('click', () => {
        const first = pending.length;
        renderMore(MORE_ROWS);
        // Keep keyboard focus on the first card of the newly added rows.
        const rows = host.querySelectorAll('.disc-row');
        const target = rows[rows.length - Math.min(first, MORE_ROWS)];
        const card = target && target.querySelector('a[data-nav="disc"]');
        if (card) card.focus({ preventScroll: true });
      });
      host.appendChild(wrap);
    }
    if (typeof window.updateFavoriteButtons === 'function') window.updateFavoriteButtons();
  }

  if (sel) {
    sel.addEventListener('change', () => {
      try {
        const u = new URL(location.href);
        u.searchParams.set('lang', sel.value);
        history.replaceState(null, '', u);
      } catch (e) { /* ignore */ }
      load();
    });
  }
  load();
})();
