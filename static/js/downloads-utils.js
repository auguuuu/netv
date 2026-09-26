// Download helpers (pure functions, no DOM).
// Exposed as window.NetvDLUtils and as a CommonJS module for tests.
(function(root) {
  'use strict';

  const UNITS = ['o', 'Ko', 'Mo', 'Go', 'To'];

  function formatBytes(n) {
    n = Number(n);
    if (!isFinite(n) || n < 0) return '';
    let i = 0;
    while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
    const digits = i === 0 || n >= 100 ? 0 : (n >= 10 ? 1 : 2);
    return n.toFixed(digits).replace('.', ',') + ' ' + UNITS[i];
  }

  function formatSpeed(bps) {
    bps = Number(bps);
    if (!isFinite(bps) || bps <= 0) return '';
    return formatBytes(bps) + '/s';
  }

  function formatEta(seconds) {
    if (seconds === null || seconds === undefined || seconds === '') return '';
    let s = Math.round(Number(seconds));
    if (!isFinite(s) || s < 0) return '';
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' min ' + String(s % 60).padStart(2, '0') + ' s';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h + ' h ' + String(m).padStart(2, '0') + ' min';
  }

  // Integer percent 0..100, or null when the total size is unknown.
  function percent(d) {
    const done = Number(d && d.bytes_done);
    const total = Number(d && d.bytes_total);
    if (!isFinite(done) || !isFinite(total) || total <= 0) return null;
    return Math.max(0, Math.min(100, Math.floor(done / total * 100)));
  }

  function isActive(status) { return status === 'queued' || status === 'downloading'; }

  // UI state used by the buttons: idle | active | paused | done | error
  function uiState(status) {
    if (isActive(status)) return 'active';
    if (status === 'paused' || status === 'done' || status === 'error') return status;
    return 'idle'; // missing or cancelled
  }

  const LABELS = {
    queued: 'En attente', downloading: 'Téléchargement', paused: 'En pause',
    done: 'Terminé', error: 'Erreur', cancelled: 'Annulé',
  };
  function statusLabel(status) { return LABELS[status] || String(status || ''); }

  function lookupKey(kind, id) { return kind + ':' + id; }

  function lookupUrl(keys) {
    return '/api/downloads/lookup?keys=' + keys.map(encodeURIComponent).join(',');
  }

  function activeCount(list) {
    return (list || []).filter(d => isActive(d.status)).length;
  }

  const api = { formatBytes, formatSpeed, formatEta, percent, isActive, uiState,
                statusLabel, lookupKey, lookupUrl, activeCount };
  root.NetvDLUtils = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
