// Playback strategy for finished (local) downloads. Pure functions, no DOM.
// Exposed as window.NetvLocal and as a CommonJS module for tests.
//
// A local file never needs a server side transcode session: the player either
// hands the raw file to <video> (native seeking) or loads a COMPLETE VOD HLS
// playlist whose segments the server cuts on demand (any seek is just a
// currentTime change). Segments are classic MPEG-TS cut by ffmpeg, like the transcode path. See local_media.py for the server half.
(function(root) {
  'use strict';

  // Capabilities of the current browser, gathered by the caller.
  //   canPlayType(mime) -> '' | 'maybe' | 'probably'   (video.canPlayType)
  //   mseSupported(mime) -> bool                       (MediaSource.isTypeSupported)
  //   hlsJs: hls.js can run (MSE available)
  //   nativeHls: <video> plays HLS by itself (Safari / iOS)
  //   audioTracks: video.audioTracks exists (needed to pick audio in a raw multi audio file)
  function detectCaps(video, win) {
    const w = win || root;
    const MS = w.MediaSource || w.ManagedMediaSource;
    return {
      canPlayType: (mime) => (video && video.canPlayType ? video.canPlayType(mime) : ''),
      mseSupported: (mime) => !!(MS && MS.isTypeSupported && MS.isTypeSupported(mime)),
      hlsJs: !!(w.Hls && w.Hls.isSupported && w.Hls.isSupported()),
      nativeHls: !!(video && video.canPlayType && video.canPlayType('application/vnd.apple.mpegurl')),
      audioTracks: !!(video && typeof video.audioTracks !== 'undefined'),
    };
  }

  // Ordered list of ways to play the plan, best first. EVERY way the server offers is
  // included (the unsupported ones last) so that a failure can always fall back.
  //   { kind: 'direct', url }                      raw file
  //   { kind: 'hls', variant, url }                complete VOD HLS (copy or x264 video)
  function chooseModes(plan, caps) {
    const modes = [];
    const seen = new Set();
    const add = (m) => {
      const key = m.kind + ':' + (m.variant || '');
      if (seen.has(key)) return;
      seen.add(key);
      modes.push(m);
    };
    const audioCount = (plan.audio || []).length;
    const direct = plan.direct || {};
    const directMime = (direct.mimes || []).some((m) => caps.canPlayType(m) !== '');
    // A raw file with several audio tracks needs the browser to expose them
    const directUsable = !!direct.ok && directMime && (audioCount <= 1 || caps.audioTracks);
    const hls = (plan.hls || []).map((h) => ({ kind: 'hls', variant: h.variant, url: h.master, mime: h.mime }));
    const hlsPlayable = (h) => (caps.hlsJs && caps.mseSupported(h.mime)) || (caps.nativeHls && !caps.hlsJs);

    if (directUsable) add({ kind: 'direct', url: direct.url });
    for (const h of hls) if (hlsPlayable(h)) add(h);
    // Last resorts: things the capability probes did not vouch for
    if (direct.ok) add({ kind: 'direct', url: direct.url });
    for (const h of hls) add(h);
    return modes;
  }

  // Resume position worth restoring, or 0.
  function startAt(resume, duration) {
    if (!resume || resume <= 5) return 0;
    if (duration && resume >= duration - 10) return 0;
    return resume;
  }

  // Subtitle entries for the player's text track list (text subtitles only, forced ones last).
  function subtitleEntries(plan) {
    const subs = (plan.subs || []).slice();
    subs.sort((a, b) => Number(!!a.forced) - Number(!!b.forced) || a.idx - b.idx);
    return subs.map((s) => ({
      idx: s.idx,
      url: s.url,
      lang: s.lang || '',
      label: s.title || (s.forced ? 'forcés' : ''),
    }));
  }

  const api = { detectCaps, chooseModes, startAt, subtitleEntries };
  root.NetvLocal = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
