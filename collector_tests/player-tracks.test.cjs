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
