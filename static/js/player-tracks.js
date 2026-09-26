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

  const trackText = (t) => `${t.name || ''} ${t.label || ''}`;

  // Subtitle track meant for deaf / hard of hearing viewers (SDH, CC, HI...).
  function isSdh(track) {
    return /\bsdh\b|\bhoh\b|\bhi\b|\bcc\b|hearing|deaf|closed.?caption|malentendant|sourds/i.test(trackText(track));
  }

  // Forced subtitles only translate foreign dialogue, not what we want by default.
  function isForced(track) {
    return /forc|forced/i.test(trackText(track));
  }

  // Track flagged as the original version by its name (e.g. "English (Original)", "VO").
  function isOriginal(track) {
    return /\boriginal\b|\borig\.|\bv\.?o\.?\b|\bo\.?v\.?\b/i.test(trackText(track));
  }

  // Default audio track index. Order: language the user picked last
  // (opts.preferred), original language (opts.original, or a track named
  // "original"), English, French, then the first track. -1 when there are no tracks.
  function pickDefaultAudio(tracks, opts) {
    if (!tracks || tracks.length === 0) return -1;
    const o = opts || {};
    let idx = findAudioByLang(tracks, o.preferred);
    if (idx >= 0) return idx;
    idx = findAudioByLang(tracks, o.original);
    if (idx >= 0) return idx;
    idx = tracks.findIndex(isOriginal);
    if (idx >= 0) return idx;
    idx = findAudioByLang(tracks, 'Anglais');
    if (idx >= 0) return idx;
    idx = findAudioByLang(tracks, 'Français');
    return idx >= 0 ? idx : 0;
  }

  // Track to show when the user turns subtitles on: English SDH, else any
  // English (not forced), else French SDH, else French, else the first one.
  // Returns -1 when there are no tracks.
  function pickDefaultSubtitle(tracks) {
    if (!tracks || tracks.length === 0) return -1;
    for (const lang of ['Anglais', 'Français']) {
      const same = tracks
        .map((t, i) => ({ t, i }))
        .filter(({ t }) => (languageName(t.lang) || languageName(t.name)) === lang && !isForced(t));
      const sdh = same.find(({ t }) => isSdh(t));
      if (sdh) return sdh.i;
      if (same.length > 0) return same[0].i;
    }
    return 0;
  }

  const api = {
    languageName, trackLabel, findAudioByLang, langKey,
    isSdh, isForced, isOriginal, pickDefaultAudio, pickDefaultSubtitle,
  };
  root.NetvTracks = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
