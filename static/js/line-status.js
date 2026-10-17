// Sidebar dot: is the IPTV line free? (/api/line-status, see line_status.py)
(function() {
  'use strict';
  const nav = document.querySelector('nav');
  if (!nav) return;
  const COLORS = { free: '#22c55e', mine: '#f97316', busy: '#ef4444', unknown: '#6b7280' };
  const dot = document.createElement('div');
  dot.className = 'w-3 h-3 rounded-full mt-auto mb-2 cursor-help';
  dot.setAttribute('role', 'status');
  dot.style.background = COLORS.unknown;
  dot.title = 'Ligne IPTV : vérification…';
  const bottom = nav.querySelector('.mt-auto');
  if (bottom) { bottom.classList.remove('mt-auto'); nav.insertBefore(dot, bottom); } else { nav.appendChild(dot); }

  function describe(s) {
    const n = s.max > 0 ? ' (' + s.active + '/' + s.max + ')' : '';
    if (s.state === 'free') return 'Ligne libre, tu peux lancer une vidéo' + n;
    if (s.state === 'mine') return 'Ligne occupée par toi (lecture ou téléchargement en cours)' + n;
    if (s.state === 'busy') return 'Ligne occupée par quelqu’un d’autre, attends avant de lancer une vidéo' + n;
    return 'État de la ligne inconnu (fournisseur injoignable)';
  }

  async function refresh() {
    if (document.hidden) return;
    try {
      const r = await fetch('/api/line-status', { credentials: 'same-origin' });
      if (!r.ok) return;
      const sources = (await r.json()).sources || [];
      if (!sources.length) { dot.hidden = true; return; }
      // worst state wins when there are several sources
      const rank = { busy: 3, mine: 2, unknown: 1, free: 0 };
      const s = sources.reduce((a, b) => (rank[b.state] > rank[a.state] ? b : a));
      dot.style.background = COLORS[s.state] || COLORS.unknown;
      dot.title = sources.length > 1 ? sources.map(x => x.name + ' : ' + describe(x)).join('\n') : describe(s);
      dot.setAttribute('aria-label', dot.title);
    } catch (e) { /* offline: keep the last state */ }
  }

  refresh();
  setInterval(refresh, 20000);
  document.addEventListener('visibilitychange', refresh);
})();
