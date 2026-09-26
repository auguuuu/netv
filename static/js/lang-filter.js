// Language filter ("Langue"): persisted in the URL query and in localStorage.
(function () {
  const KEY = 'netv.lang';
  const sel = document.getElementById('lang-select');
  if (!sel) return;

  function read() { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } }
  function write(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* ignore */ } }

  const url = new URL(window.location.href);
  if (!url.searchParams.has('lang')) {
    // No lang in the URL: restore the remembered choice if this page offers it.
    const saved = read();
    if (saved && Array.from(sel.options).some(o => o.value === saved)) {
      const isSearch = sel.form && sel.form.getAttribute('action') === '/search';
      if (isSearch && !url.searchParams.get('q')) {
        sel.value = saved;  // empty search form: just preselect
      } else {
        url.searchParams.set('lang', saved);
        window.location.replace(url.toString());
        return;
      }
    }
  } else {
    write(url.searchParams.get('lang') || '');
  }

  sel.addEventListener('change', function () {
    write(sel.value);
    if (sel.form) {
      // Search form: submit keeps q, regex, live/vod/series, limit and lang.
      // An empty lang is sent as "lang=" so the URL records the explicit "Toutes".
      sel.form.submit();
    } else if (typeof window.updateBrowseUrl === 'function') {
      window.updateBrowseUrl();
    }
  });
})();
