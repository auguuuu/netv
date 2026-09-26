const assert = require('node:assert/strict');
const { test } = require('node:test');
const T = require('../static/js/player-tracks.js');

test('language codes map to French names', () => {
  for (const c of ['fre', 'fra', 'fr', 'fr-FR', 'FRE']) assert.equal(T.languageName(c), 'Français');
  for (const c of ['eng', 'en', 'en_US']) assert.equal(T.languageName(c), 'Anglais');
  assert.equal(T.languageName('jpn'), 'Japonais');
  assert.equal(T.languageName('ja'), 'Japonais');
  assert.equal(T.languageName('ger'), 'Allemand');
  assert.equal(T.languageName('deu'), 'Allemand');
  assert.equal(T.languageName('xx'), '');
});

test('trackLabel prefers language, falls back to raw name', () => {
  assert.equal(T.trackLabel('jpn', 'Japanese'), 'Japonais');
  assert.equal(T.trackLabel('fre', 'Français'), 'Français');
  assert.equal(T.trackLabel('fre', 'Audio 2'), 'Français');
  assert.equal(T.trackLabel('eng', 'Commentary'), 'Anglais (Commentary)');
  assert.equal(T.trackLabel('', 'Klingon'), 'Klingon');
  assert.equal(T.trackLabel('tlh', ''), 'TLH');
  assert.equal(T.trackLabel('', '', 'Piste 3'), 'Piste 3');
});

test('findAudioByLang matches across code variants', () => {
  const tracks = [{ lang: 'jpn', name: 'Japonais' }, { lang: 'fre', name: 'Français' }];
  assert.equal(T.findAudioByLang(tracks, T.langKey(tracks[1])), 1);
  assert.equal(T.findAudioByLang(tracks, 'fra'), 1);
  assert.equal(T.findAudioByLang(tracks, 'spa'), -1);
  assert.equal(T.findAudioByLang(tracks, ''), -1);
});

test('default audio: last choice, then original, then English, then French, then first', () => {
  const jpFrEn = [{ lang: 'jpn' }, { lang: 'fre' }, { lang: 'eng' }];
  assert.equal(T.pickDefaultAudio(jpFrEn, {}), 2); // English beats French and Japanese
  assert.equal(T.pickDefaultAudio(jpFrEn, { preferred: 'Français' }), 1); // explicit choice wins
  assert.equal(T.pickDefaultAudio(jpFrEn, { original: 'ja' }), 0); // known original language
  assert.equal(T.pickDefaultAudio([{ lang: 'ger' }, { lang: 'fre' }], {}), 1); // no English: French
  assert.equal(T.pickDefaultAudio([{ lang: 'ger' }, { lang: 'ita' }], {}), 0); // nothing known: first
  assert.equal(T.pickDefaultAudio([{ lang: 'fre' }, { lang: 'und', name: 'Original' }], {}), 1);
  assert.equal(T.pickDefaultAudio([], {}), -1);
});

test('default subtitles: English SDH, else English, else French, skipping forced tracks', () => {
  const subs = [
    { lang: 'fre', name: 'Français' },
    { lang: 'eng', name: 'English (forced)' },
    { lang: 'eng', name: 'English' },
    { lang: 'eng', name: 'English SDH' },
  ];
  assert.equal(T.pickDefaultSubtitle(subs), 3);
  assert.equal(T.pickDefaultSubtitle(subs.slice(0, 3)), 2); // plain English, not the forced one
  assert.equal(T.pickDefaultSubtitle([{ lang: 'ger' }, { lang: 'fre', name: 'French CC' }]), 1);
  assert.equal(T.pickDefaultSubtitle([{ lang: 'ger' }, { lang: 'ita' }]), 0);
  assert.equal(T.pickDefaultSubtitle([]), -1);
  assert.equal(T.isSdh({ name: 'English [CC]' }), true);
  assert.equal(T.isSdh({ name: 'English' }), false);
});

test('findOriginal: language match, first non-commentary, probable source', () => {
  const tr = [
    { lang: 'fre', name: 'Français' },
    { lang: 'eng', name: 'English Commentary' },
    { lang: 'eng', name: 'English' },
    { lang: 'eng', name: 'English' },
  ];
  const r = T.findOriginal(tr, { originalLang: 'en', source: 'tmdb' });
  assert.deepEqual(r, { idx: 2, lang: 'Anglais', probable: false, source: 'tmdb' });
  assert.equal(T.findOriginal(tr, { originalLang: 'en', source: 'country' }).probable, true);
  assert.equal(T.findOriginal(tr, { originalLang: 'ja', source: 'tmdb' }), null);
  assert.equal(T.findOriginal(tr, {}), null);
  assert.equal(T.findOriginal([], { originalLang: 'en' }), null);
  assert.equal(T.findOriginal(null, undefined), null);
});

test('findOriginal: falls back to flags and names when the language is unknown', () => {
  const byName = [{ lang: 'fre', name: 'Français' }, { lang: 'jpn', name: 'Japanese (Original)' }];
  assert.deepEqual(T.findOriginal(byName, {}), { idx: 1, lang: 'Japonais', probable: false, source: 'name' });
  const byFlag = [{ lang: 'fre', name: 'Audio 1' }, { lang: 'spa', name: 'Audio 2', original: true }];
  assert.equal(T.findOriginal(byFlag, { originalLang: '' }).idx, 1);
  assert.equal(T.findOriginal(byFlag, { originalLang: '' }).source, 'flag');
  // A flagged track among language matches wins over an earlier plain one.
  const two = [{ lang: 'eng', name: 'English' }, { lang: 'eng', name: 'English', original: true }];
  assert.equal(T.findOriginal(two, { originalLang: 'en', source: 'tmdb' }).idx, 1);
  // Commentary is never picked, even when flagged.
  assert.equal(T.findOriginal([{ lang: 'eng', name: 'Audio Description', original: true }], {}), null);
});

test('originalTitle and audioButtonTitle', () => {
  const sure = { idx: 0, lang: 'Anglais', probable: false, source: 'tmdb' };
  const guess = { ...sure, probable: true, source: 'country' };
  assert.equal(T.originalTitle(sure), 'Version originale : Anglais');
  assert.equal(T.originalTitle(guess), 'Version originale probable : Anglais');
  assert.equal(T.originalTitle({ idx: 0, lang: '', probable: false, source: 'flag' }), 'Version originale');
  assert.equal(T.originalTitle(null), '');
  assert.equal(T.audioButtonTitle('Audio (l)', sure), 'Audio (l), VO : Anglais');
  assert.equal(T.audioButtonTitle('Audio (l)', guess), 'Audio (l), VO probable : Anglais');
  assert.equal(T.audioButtonTitle('Audio (l)', null), 'Audio (l)');
});

test('jump-to-time feature is gone from player.js and player.html', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const root = path.join(__dirname, '..');
  const src = fs.readFileSync(path.join(root, 'static/js/player.js'), 'utf8') +
    fs.readFileSync(path.join(root, 'templates/player.html'), 'utf8');
  for (const needle of ['jump-btn', 'menu-jump', 'seek-container', 'seek-input', 'seek-duration', "case 'j'"]) {
    assert.ok(!src.includes(needle), needle);
  }
  assert.ok(!/currentTime [-+]= 10/.test(src));
  assert.match(src, /currentTime -= SEEK_STEP/);
  assert.match(src, /SEEK_STEP = 5;/);
});
