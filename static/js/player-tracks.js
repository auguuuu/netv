// Audio / subtitle track helpers (pure functions, no DOM).
// Exposed as window.NetvTracks and as a CommonJS module for tests.
(function(root) {
  'use strict';

  // ISO 639-1/2/3 codes and common English names -> French label.
  const LANGS = {
    fr: 'Français', fre: 'Français', fra: 'Français', french: 'Français', francais: 'Français',
    en: 'Anglais', eng: 'Anglais', english: 'Anglais',
    ja: 'Japonais', jpn: 'Japonais', jp: 'Japonais', japanese: 'Japonais',
    es: 'Espagnol', spa: 'Espagnol', spanish: 'Espagnol',
    de: 'Allemand', ger: 'Allemand', deu: 'Allemand', german: 'Allemand',
    it: 'Italien', ita: 'Italien', italian: 'Italien',
    pt: 'Portugais', por: 'Portugais', portuguese: 'Portugais',
    ar: 'Arabe', ara: 'Arabe', arabic: 'Arabe',
    ru: 'Russe', rus: 'Russe', russian: 'Russe',
    zh: 'Chinois', chi: 'Chinois', zho: 'Chinois', chinese: 'Chinois',
    ko: 'Coréen', kor: 'Coréen', korean: 'Coréen',
    nl: 'Néerlandais', dut: 'Néerlandais', nld: 'Néerlandais', dutch: 'Néerlandais',
    pl: 'Polonais', pol: 'Polonais', polish: 'Polonais',
    tr: 'Turc', tur: 'Turc', turkish: 'Turc',
    hi: 'Hindi', hin: 'Hindi', hindi: 'Hindi',
    sv: 'Suédois', swe: 'Suédois', swedish: 'Suédois',
    da: 'Danois', dan: 'Danois', danish: 'Danois',
    no: 'Norvégien', nor: 'Norvégien', nob: 'Norvégien', norwegian: 'Norvégien',
    fi: 'Finnois', fin: 'Finnois', finnish: 'Finnois',
    el: 'Grec', gre: 'Grec', ell: 'Grec', greek: 'Grec',
    he: 'Hébreu', heb: 'Hébreu', hebrew: 'Hébreu',
    cs: 'Tchèque', cze: 'Tchèque', ces: 'Tchèque', czech: 'Tchèque',
    ro: 'Roumain', rum: 'Roumain', ron: 'Roumain', romanian: 'Roumain',
    hu: 'Hongrois', hun: 'Hongrois', hungarian: 'Hongrois',
    uk: 'Ukrainien', ukr: 'Ukrainien', ukrainian: 'Ukrainien',
    th: 'Thaï', tha: 'Thaï', thai: 'Thaï',
    vi: 'Vietnamien', vie: 'Vietnamien', vietnamese: 'Vietnamien',
    id: 'Indonésien', ind: 'Indonésien', indonesian: 'Indonésien',
    fa: 'Persan', per: 'Persan', fas: 'Persan', persian: 'Persan',
    ca: 'Catalan', cat: 'Catalan', catalan: 'Catalan',
    la: 'Latin', lat: 'Latin', latin: 'Latin',
    und: 'Indéterminé',
  };

  function stripAccents(s) {
    return s.normalize('NFD').replace(/[̀-ͯ]/g, '');
  }

  // Full French name for a language code or name, or '' when unknown.
  function languageName(code) {
    if (!code) return '';
    const raw = String(code).trim().toLowerCase();
    if (!raw) return '';
    const key = stripAccents(raw);
    if (LANGS[key]) return LANGS[key];
    const base = key.split(/[-_]/)[0];
    return LANGS[base] || '';
  }

  // Readable label from a track's language code and name.
  // Extras in the raw name (e.g. "Français (forcé)", "Commentary") are kept as a suffix.
  function trackLabel(lang, name, fallback) {
    const fromLang = languageName(lang);
    const rawName = name == null ? '' : String(name).trim();
    const fromName = languageName(rawName);
    const base = fromLang || fromName;
    if (base) {
      const same = stripAccents(rawName.toLowerCase()) === stripAccents(base.toLowerCase());
      const generic = /^(audio|track|piste|stream)[\s_-]*\d*$/i.test(rawName);
      if (rawName && !fromName && !same && !generic &&
          rawName.toLowerCase() !== String(lang || '').toLowerCase()) {
        return `${base} (${rawName})`;
      }
      return base;
    }
    return rawName || (lang ? String(lang).toUpperCase() : '') || fallback || 'Piste';
  }

  // Index of the audio track matching a stored language, or -1.
  // tracks: [{lang, name}]
  function findAudioByLang(tracks, savedLang) {
    const saved = languageName(savedLang) || String(savedLang || '').toLowerCase();
    if (!saved) return -1;
    return tracks.findIndex(t => {
      const l = languageName(t.lang) || languageName(t.name) || String(t.lang || '').toLowerCase();
      return l && l.toLowerCase() === saved.toLowerCase();
    });
  }

  // Canonical value to persist for a track (its French language label, else raw lang).
  function langKey(track) {
    return languageName(track.lang) || languageName(track.name) || String(track.lang || '').toLowerCase();
  }

  const api = { languageName, trackLabel, findAudioByLang, langKey };
  root.NetvTracks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
