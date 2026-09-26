const assert = require('node:assert/strict');
const { test } = require('node:test');
const U = require('../static/js/downloads-utils.js');

test('formatBytes uses French units and decimal comma', () => {
  assert.equal(U.formatBytes(0), '0 o');
  assert.equal(U.formatBytes(512), '512 o');
  assert.equal(U.formatBytes(1536), '1,50 Ko');
  assert.equal(U.formatBytes(5 * 1024 * 1024), '5,00 Mo');
  assert.equal(U.formatBytes(1.5 * 1024 ** 3), '1,50 Go');
  assert.equal(U.formatBytes(25 * 1024 ** 3), '25,0 Go');
  assert.equal(U.formatBytes(500 * 1024 ** 2), '500 Mo');
  assert.equal(U.formatBytes(-1), '');
  assert.equal(U.formatBytes(null) === '0 o' || U.formatBytes(null) === '', true);
  assert.equal(U.formatBytes('x'), '');
});

test('formatSpeed', () => {
  assert.equal(U.formatSpeed(0), '');
  assert.equal(U.formatSpeed(undefined), '');
  assert.equal(U.formatSpeed(2 * 1024 * 1024), '2,00 Mo/s');
});

test('formatEta', () => {
  assert.equal(U.formatEta(null), '');
  assert.equal(U.formatEta(-5), '');
  assert.equal(U.formatEta(0), '0 s');
  assert.equal(U.formatEta(45), '45 s');
  assert.equal(U.formatEta(185), '3 min 05 s');
  assert.equal(U.formatEta(3725), '1 h 02 min');
});

test('percent clamps and handles unknown totals', () => {
  assert.equal(U.percent({ bytes_done: 50, bytes_total: 200 }), 25);
  assert.equal(U.percent({ bytes_done: 300, bytes_total: 200 }), 100);
  assert.equal(U.percent({ bytes_done: 10, bytes_total: 0 }), null);
  assert.equal(U.percent({}), null);
  assert.equal(U.percent(null), null);
});

test('uiState and activeCount', () => {
  assert.equal(U.uiState('queued'), 'active');
  assert.equal(U.uiState('downloading'), 'active');
  assert.equal(U.uiState('paused'), 'paused');
  assert.equal(U.uiState('done'), 'done');
  assert.equal(U.uiState('error'), 'error');
  assert.equal(U.uiState('cancelled'), 'idle');
  assert.equal(U.uiState(undefined), 'idle');
  assert.equal(U.activeCount([{ status: 'queued' }, { status: 'done' }, { status: 'downloading' }, { status: 'paused' }]), 2);
  assert.equal(U.activeCount(null), 0);
});

test('lookup keys and url', () => {
  assert.equal(U.lookupKey('movie', 12), 'movie:12');
  assert.equal(U.lookupUrl(['movie:1', 'episode:2']), '/api/downloads/lookup?keys=movie%3A1,episode%3A2');
  assert.equal(U.statusLabel('paused'), 'En pause');
});
