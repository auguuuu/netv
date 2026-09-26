const assert = require('node:assert/strict');
const { test } = require('node:test');
const L = require('../static/js/local-playback.js');

const caps = (over = {}) => ({
  canPlayType: () => 'probably',
  mseSupported: () => true,
  hlsJs: true,
  nativeHls: false,
  audioTracks: false,
  ...over,
});

const plan = (over = {}) => ({
  audio: [{ idx: 0 }],
  subs: [],
  direct: { ok: true, url: '/downloads/files/x', mimes: ['video/mp4; codecs="avc1.640028, mp4a.40.2"'] },
  hls: [
    { variant: 'copy', master: '/local/x/copy/master.m3u8', mime: 'video/mp4; codecs="avc1.640028, mp4a.40.2"' },
    { variant: 'x264', master: '/local/x/x264/master.m3u8', mime: 'video/mp4; codecs="avc1.640028, mp4a.40.2"' },
  ],
  ...over,
});

const kinds = (modes) => modes.map((m) => m.kind + (m.variant ? ':' + m.variant : ''));

test('a playable single audio file goes straight to the raw file, HLS stays as the fallback', () => {
  assert.deepEqual(kinds(L.chooseModes(plan(), caps())), ['direct', 'hls:copy', 'hls:x264']);
});

test('the raw file is skipped when the browser cannot play its codecs', () => {
  const modes = L.chooseModes(plan(), caps({ canPlayType: () => '' }));
  assert.deepEqual(kinds(modes), ['hls:copy', 'hls:x264', 'direct']); // still tried, last
});

test('several audio tracks need audioTracks to play the raw file', () => {
  const two = plan({ audio: [{ idx: 0 }, { idx: 1 }] });
  assert.equal(kinds(L.chooseModes(two, caps()))[0], 'hls:copy');
  assert.equal(kinds(L.chooseModes(two, caps({ audioTracks: true })))[0], 'direct'); // Safari
});

test('incompatible files (ac3, avi...) only offer HLS and never a raw first try', () => {
  const p = plan({ direct: { ok: false, reason: 'audio codec ac3', url: '/d', mimes: [] } });
  assert.deepEqual(kinds(L.chooseModes(p, caps())), ['hls:copy', 'hls:x264']);
});

test('video the browser cannot decode from HLS copy falls back to the x264 variant first', () => {
  const p = plan({ direct: { ok: false, url: '/d', mimes: [] } });
  p.hls[0].mime = 'video/mp4; codecs="hvc1.1.6.L120.B0, mp4a.40.2"';
  const modes = L.chooseModes(p, caps({ mseSupported: (m) => !m.includes('hvc1') }));
  assert.deepEqual(kinds(modes), ['hls:x264', 'hls:copy']);
});

test('every way the server offers stays in the list so a failure can always retry', () => {
  const modes = L.chooseModes(plan(), caps({ canPlayType: () => '', mseSupported: () => false }));
  assert.equal(modes.length, 3);
  assert.equal(new Set(kinds(modes)).size, 3);
});

test('native HLS (Safari without MSE) counts as playable', () => {
  const p = plan({ direct: { ok: false, url: '/d', mimes: [] } });
  const modes = L.chooseModes(p, caps({ hlsJs: false, nativeHls: true, mseSupported: () => false }));
  assert.equal(kinds(modes)[0], 'hls:copy');
});

test('a plan with nothing playable gives an empty list', () => {
  assert.deepEqual(L.chooseModes({ audio: [], direct: { ok: false }, hls: [] }, caps()), []);
});

test('startAt restores a resume position only when it is worth it', () => {
  assert.equal(L.startAt(0, 100), 0);
  assert.equal(L.startAt(3, 1000), 0);
  assert.equal(L.startAt(600, 1000), 600);
  assert.equal(L.startAt(995, 1000), 0); // nearly finished: start over
  assert.equal(L.startAt(600, 0), 600);
  assert.equal(L.startAt(undefined, 1000), 0);
});

test('subtitleEntries keeps text tracks in order, forced ones last, with their URL', () => {
  const entries = L.subtitleEntries({
    subs: [
      { idx: 0, lang: 'fre', title: '', forced: true, url: '/s/0' },
      { idx: 1, lang: 'eng', title: 'SDH', forced: false, url: '/s/1' },
      { idx: 2, lang: 'fre', title: '', forced: false, url: '/s/2' },
    ],
  });
  assert.deepEqual(entries.map((e) => e.url), ['/s/1', '/s/2', '/s/0']);
  assert.equal(entries[0].label, 'SDH');
  assert.equal(entries[2].label, 'forcés');
  assert.deepEqual(L.subtitleEntries({}), []);
});

test('detectCaps reads what the browser offers', () => {
  const video = {
    canPlayType: (m) => (m === 'application/vnd.apple.mpegurl' ? 'maybe' : 'probably'),
    audioTracks: [],
  };
  const win = { MediaSource: { isTypeSupported: (m) => m.includes('avc1') }, Hls: { isSupported: () => true } };
  const c = L.detectCaps(video, win);
  assert.equal(c.hlsJs, true);
  assert.equal(c.nativeHls, true);
  assert.equal(c.audioTracks, true);
  assert.equal(c.mseSupported('video/mp4; codecs="avc1.640028"'), true);
  assert.equal(c.mseSupported('video/mp4; codecs="hvc1.1.6.L93.B0"'), false);
  const bare = L.detectCaps({ canPlayType: () => '' }, {});
  assert.equal(bare.hlsJs, false);
  assert.equal(bare.audioTracks, false);
  assert.equal(bare.mseSupported('x'), false);
});
