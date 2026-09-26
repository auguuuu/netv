// Discovery rows for the Movies landing page. Data: GET /api/discover?lang=
// Cards reuse the movie-card markup and data attributes so downloads-ui.js
// (download button, offline dimming) and the favorite star keep working.
(function () {
  'use strict';
  const host = document.getElementById('discover-rows');
  if (!host) return;
  const sel = document.getElementById('lang-select');
  const skeleton = host.innerHTML;
  let reqId = 0;

  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
      <h3 class="disc-title">${esc(row.title)}</h3>
      <div class="disc-viewport">
        <button type="button" class="disc-arrow disc-prev" aria-label="Précédent" tabindex="-1" disabled>&#8249;</button>
        <div class="disc-track" role="list">${row.items.map(cardHtml).join('')}</div>
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
      const rows = Array.from(host.querySelectorAll('.disc-track'));
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
      const r = await fetch('/api/discover?lang=' + encodeURIComponent(lang), { credentials: 'same-origin' });
      if (r.ok) data = await r.json();
    } catch (e) { /* offline or server error, handled below */ }
    if (my !== reqId) return;  // a newer request superseded this one
    host.removeAttribute('aria-busy');
    if (!data) {
      host.innerHTML = '<p class="disc-empty">Impossible de charger les suggestions. <a class="underline" href="/vod?sort=rating">Tout parcourir</a></p>';
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
    host.innerHTML = data.rows.map(rowHtml).join('');
    host.querySelectorAll('.disc-row').forEach(wireRow);
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
