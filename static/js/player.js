// IPTV Player Module
// Requires: Hls.js, window.PLAYER_CONFIG

(function() {
  'use strict';

  const cfg = window.PLAYER_CONFIG;
  const video = document.getElementById('video');
  const loading = document.getElementById('loading');
  const error = document.getElementById('error');
  const ccBtn = document.getElementById('toggle-cc');
  const settingsMenu = document.getElementById('settings-menu');
  const audioBtn = document.getElementById('toggle-audio');
  const audioMenu = document.getElementById('audio-menu');
  const ccMenu = document.getElementById('cc-menu');
  const Tracks = window.NetvTracks;
  const Local = window.NetvLocal;
  const AUDIO_LANG_KEY = 'playerAudioLang';

  // State
  let transcodeSessionId = null;
  let currentHls = null;
  let isTranscoding = false;
  let ccEnabled = cfg.captionsEnabled;
  let subtitlePollTimerId = null;
  let transcodedDuration = 0;
  let totalDuration = 0;
  let seekInProgress = false;
  let seekOffset = 0;
  let currentSubtitles = null;
  let activeTrackStates = null;
  let ccSelectedIdx = null; // explicit user pick in the CC menu, null = automatic
  let progressPollTimerId = null;
  let lastSavedPosition = 0;
  let savePositionTimeout = null;
  let autoMutedByPolicy = false;
  const transcodeSession = new window.NetvPlayback.TranscodeSession(cfg.isVod);
  let adaptiveSession = null;
  let adaptivePlayback = null;
  let transcodePlaylist = null;
  let programStart = cfg.programStart || 0;
  let programEnd = cfg.programEnd || 0;
  let programRefreshInFlight = false;
  let programRetryAt = 0;

  // ============================================================
  // Utilities
  // ============================================================

  // Arrow keys seek by this many seconds (streamed, live DVR and local files).
  const SEEK_STEP = 5;

  function formatTime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function parseVttTime(str) {
    const parts = str.split(':');
    if (parts.length === 3) return parseFloat(parts[0]) * 3600 + parseFloat(parts[1]) * 60 + parseFloat(parts[2]);
    if (parts.length === 2) return parseFloat(parts[0]) * 60 + parseFloat(parts[1]);
    return 0;
  }

  function parseVttCues(vttText) {
    const cues = [];
    const lines = vttText.split('\n');
    let i = 0;
    while (i < lines.length && !lines[i].includes('-->')) i++;
    while (i < lines.length) {
      const line = lines[i];
      if (line.includes('-->')) {
        const [startStr, endStr] = line.split('-->').map(s => s.trim().split(' ')[0]);
        const start = parseVttTime(startStr);
        const end = parseVttTime(endStr);
        i++;
        const textLines = [];
        while (i < lines.length && lines[i].trim() !== '') {
          textLines.push(lines[i]);
          i++;
        }
        if (textLines.length > 0) cues.push({start, end, text: textLines.join('\n')});
      }
      i++;
    }
    return cues;
  }

  // ============================================================
  // UI Helpers
  // ============================================================

  function hideLoading() {
    loading.classList.add('hidden');
  }

  function showLoading() {
    loading.classList.remove('hidden');
  }

  function showError(message, canDismiss) {
    adaptivePlayback?.destroy();
    adaptivePlayback = null;
    hideLoading();
    const text = document.getElementById('error-message');
    if (text) text.textContent = message || 'Failed to load stream';
    document.getElementById('error-dismiss')?.classList.toggle('hidden', !canDismiss);
    error.classList.remove('hidden');
  }

  function updateTranscodeCheck() {
    const check = document.getElementById('tc-check');
    if (check) check.textContent = isTranscoding ? '✓' : '';
  }

  function updateCcButton() {
    ccBtn.classList.toggle('active', ccEnabled);
  }

  function updatePlayIcon() {
    const playIcon = document.getElementById('play-icon');
    const pauseIcon = document.getElementById('pause-icon');
    if (playIcon && pauseIcon) {
      playIcon.classList.toggle('hidden', !video.paused);
      pauseIcon.classList.toggle('hidden', video.paused);
    }
  }

  function updateMuteIcon() {
    const volIcon = document.getElementById('vol-icon');
    const mutedIcon = document.getElementById('muted-icon');
    if (volIcon && mutedIcon) {
      volIcon.classList.toggle('hidden', video.muted);
      mutedIcon.classList.toggle('hidden', !video.muted);
    }
  }

  function updateFullscreenIcon() {
    const fsEnter = document.getElementById('fs-enter');
    const fsExit = document.getElementById('fs-exit');
    const isFs = !!document.fullscreenElement;
    if (fsEnter && fsExit) {
      fsEnter.classList.toggle('hidden', isFs);
      fsExit.classList.toggle('hidden', !isFs);
    }
  }

  function disableCcButton() {
    ccBtn.disabled = true;
    ccBtn.classList.remove('active');
  }

  function enableCcButton() {
    ccBtn.disabled = false;
    updateCcButton();
  }

  // ============================================================
  // HLS Configuration
  // ============================================================

  // Custom cueHandler for CEA-608 caption positioning
  // See: https://github.com/video-dev/hls.js/issues/654
  const customCueHandler = {
    newCue(track, startTime, endTime, captionScreen) {
      const lines = [];
      for (let r = 0; r < 15; r++) {
        const row = captionScreen.rows[r];
        let text = '';
        for (let c = 0; c < 32; c++) {
          text += row.chars[c]?.uchar || ' ';
        }
        text = text.trim();
        if (text) lines.push(text);
      }
      if (lines.length === 0) return [];
      const cue = new VTTCue(startTime, endTime, lines.join('\n'));
      cue.line = -2;
      cue.align = 'center';
      track.addCue(cue);
      return [cue];
    }
  };

  function createHlsConfig(options = {}) {
    const base = {
      enableWorker: true,
      lowLatencyMode: false,
      enableCEA708Captions: true,
      subtitleDisplay: ccEnabled,
      cueHandler: customCueHandler,
      manifestLoadingRetryDelay: 1000,
      levelLoadingRetryDelay: 1000,
      fragLoadingRetryDelay: 1000,
    };
    if (options.forSeek) {
      return {
        ...base,
        liveSyncDurationCount: 0,
        startPosition: 0,
        manifestLoadingMaxRetry: 30,
        levelLoadingMaxRetry: 30,
        fragLoadingMaxRetry: 30,
      };
    }
    return {
      ...base,
      liveSyncDurationCount: options.isVod ? 0 : 3,
      startPosition: options.isVod ? 0 : -1,
    };
  }

  // ============================================================
  // Captions
  // ============================================================

  function applyCaptionStyles() {
    const s = cfg.ccStyle || {};
    const hexToRgba = (hex, opacity) => {
      if (hex === 'transparent') return 'transparent';
      const r = parseInt(hex.slice(1,3), 16);
      const g = parseInt(hex.slice(3,5), 16);
      const b = parseInt(hex.slice(5,7), 16);
      return `rgba(${r},${g},${b},${opacity})`;
    };
    const color = hexToRgba(s.cc_color || '#ffffff', 1);
    const shadow = s.cc_shadow || '0 0 4px black, 0 0 4px black';
    const bg = hexToRgba(s.cc_bg || '#000000', s.cc_bg_opacity || 0.75);
    const size = s.cc_size || '1em';
    const font = s.cc_font || 'inherit';
    let style = document.getElementById('cc-style');
    if (!style) {
      style = document.createElement('style');
      style.id = 'cc-style';
      document.head.appendChild(style);
    }
    const sizeMultiplier = parseFloat(size) || 1;
    const infoSize = (2.5 * sizeMultiplier) + 'vh';
    style.textContent = `
      video::cue {
        color: ${color} !important;
        text-shadow: ${shadow} !important;
        background-color: ${bg} !important;
        font-size: ${size} !important;
        font-family: ${font} !important;
      }
      #info-overlay { font-size: ${infoSize}; max-width: 50em; }
    `;
  }

  function getPreferredSubtitleTrack(tracks) {
    // Explicit pick in the CC menu stays across seeks
    if (ccSelectedIdx !== null && ccSelectedIdx < tracks.length) return ccSelectedIdx;
    const prefLang = cfg.ccLang || '';
    if (!prefLang && tracks.length > 0) {
      // No configured language: English SDH, else English, else French
      const idx = Tracks.pickDefaultSubtitle(tracks.map(t => ({ lang: t.lang || t.language, name: t.name || t.label })));
      return idx >= 0 ? idx : 0;
    }
    if (!prefLang || tracks.length === 0) return 0;
    // Handle CC1-CC4 (CEA-608 channels)
    if (/^cc[1-4]$/i.test(prefLang)) {
      const ccNum = prefLang.toUpperCase();
      const idx = tracks.findIndex(t => (t.name || t.label || '').toUpperCase().includes(ccNum));
      if (idx >= 0) return idx;
      // Fallback: CC1 is usually index 0, CC2 is index 1, etc.
      const num = parseInt(prefLang.slice(2)) - 1;
      return num < tracks.length ? num : 0;
    }
    const langNames = {en: 'english', es: 'spanish', fr: 'french', de: 'german', it: 'italian', pt: 'portuguese', ja: 'japanese', ko: 'korean', zh: 'chinese'};
    let idx = tracks.findIndex(t => t.lang && t.lang.toLowerCase().startsWith(prefLang));
    if (idx >= 0) return idx;
    const prefName = langNames[prefLang];
    if (prefName) {
      idx = tracks.findIndex(t => (t.name || t.label) && (t.name || t.label).toLowerCase().includes(prefName));
      if (idx >= 0) return idx;
    }
    return 0;
  }

  function applyCaptionsSetting() {
    const tracks = Array.from(video.textTracks).filter(t =>
      (t.kind === 'subtitles' || t.kind === 'captions') && t.mode !== 'disabled');
    if (!ccEnabled) {
      tracks.forEach(t => t.mode = 'hidden');
      return;
    }
    if (tracks.length === 0) return;
    const prefLang = cfg.ccLang || '';
    const langNames = {en: 'english', es: 'spanish', fr: 'french', de: 'german', it: 'italian', pt: 'portuguese', ja: 'japanese', ko: 'korean', zh: 'chinese'};
    let preferredIdx = 0;
    if (prefLang) {
      const idx = tracks.findIndex(t => t.language && t.language.toLowerCase().startsWith(prefLang));
      if (idx >= 0) preferredIdx = idx;
      else {
        const prefName = langNames[prefLang];
        if (prefName) {
          const nameIdx = tracks.findIndex(t => t.label && t.label.toLowerCase().includes(prefName));
          if (nameIdx >= 0) preferredIdx = nameIdx;
        }
      }
    }
    if (ccSelectedIdx !== null && ccSelectedIdx < tracks.length) preferredIdx = ccSelectedIdx;
    tracks.forEach((t, i) => t.mode = i === preferredIdx ? 'showing' : 'hidden');
  }

  function startSubtitlePolling(subtitles, prefIdx) {
    if (subtitlePollTimerId) {
      clearInterval(subtitlePollTimerId);
      clearTimeout(subtitlePollTimerId);
      subtitlePollTimerId = null;
    }
    if (activeTrackStates && activeTrackStates.length === subtitles.length) {
      for (const ts of activeTrackStates) {
        if (ts.track.mode === 'disabled') ts.track.mode = 'hidden';
        const cues = ts.track.cues;
        if (cues) while (cues.length > 0) ts.track.removeCue(cues[0]);
        ts.addedCues.clear();
        ts.retryCount = 0;
      }
    } else {
      for (let i = 0; i < video.textTracks.length; i++) {
        video.textTracks[i].mode = 'disabled';
      }
      activeTrackStates = subtitles.map((sub) => ({
        url: sub.url,
        track: video.addTextTrack('subtitles', sub.label, sub.lang),
        addedCues: new Set(),
        retryCount: 0,
      }));
    }
    if (ccSelectedIdx !== null && ccSelectedIdx < activeTrackStates.length) prefIdx = ccSelectedIdx;
    activeTrackStates.forEach((ts, i) => {
      ts.track.mode = (ccEnabled && i === prefIdx) ? 'showing' : 'hidden';
    });

    const poll = async () => {
      for (let i = 0; i < activeTrackStates.length; i++) {
        const ts = activeTrackStates[i];
        if (ts.retryCount > 120) continue;
        try {
          const resp = await fetch(ts.url + '?t=' + Date.now());
          if (!resp.ok) { ts.retryCount++; continue; }
          const vtt = await resp.text();
          const cues = parseVttCues(vtt);
          for (const cue of cues) {
            const key = `${cue.start}-${cue.end}`;
            if (!ts.addedCues.has(key)) {
              try {
                ts.track.addCue(new VTTCue(cue.start, cue.end, cue.text));
                ts.addedCues.add(key);
              } catch (e) {}
            }
          }
          ts.retryCount = 0;
        } catch (e) { ts.retryCount++; }
      }
    };

    let pollCount = 0;
    const doPoll = async () => {
      await poll();
      pollCount++;
      subtitlePollTimerId = pollCount < 20 ? setTimeout(doPoll, 500) : setInterval(poll, 5000);
    };
    doPoll();
  }

  // ============================================================
  // Position Tracking
  // ============================================================

  function savePosition() {
    const actualTime = video.currentTime + seekOffset;
    if (!cfg.isVod || actualTime < 5) return;
    if (video.currentTime < 1 && seekOffset > 0) return;
    if (Math.abs(actualTime - lastSavedPosition) < 5) return;
    lastSavedPosition = actualTime;
    const data = JSON.stringify({ url: cfg.rawUrl, position: actualTime, duration: totalDuration });
    if (document.visibilityState === 'hidden') {
      navigator.sendBeacon('/api/watch-position', new Blob([data], {type: 'application/json'}));
    } else {
      fetch('/api/watch-position', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: data });
    }
  }

  function restorePosition() {
    if (!cfg.isVod) return;
    const savedTime = cfg.serverResumePosition;
    if (!savedTime || savedTime <= 5) return;
    if (transcodedDuration < 30) return;
    const rangeStart = seekOffset;
    const rangeEnd = seekOffset + Math.max(0, transcodedDuration - 10);
    if (savedTime < rangeStart) return;
    const targetTime = Math.min(savedTime, rangeEnd);
    video.currentTime = targetTime - seekOffset;
  }

  function setupPositionTracking() {
    if (!cfg.isVod) return;
    video.addEventListener('timeupdate', () => {
      if (savePositionTimeout) return;
      savePositionTimeout = setTimeout(() => {
        savePosition();
        savePositionTimeout = null;
      }, 10000);
    });
    video.addEventListener('pause', savePosition);
    video.addEventListener('ended', savePosition);
    video.addEventListener('ended', () => {
      if (autoNextEnabled && cfg.nextEpisodeUrl) window.location.href = cfg.nextEpisodeUrl;
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') savePosition();
    });
  }

  // ============================================================
  // Live DVR Resume
  // ============================================================

  // MSE reports seekable as [0, duration] for live streams, so the
  // retained window must come from the active playlist's first
  // fragment. Native HLS (Safari) reports the real window in seekable.
  function liveWindowStart() {
    if (currentHls) {
      const levels = currentHls.levels || [];
      const level = levels[currentHls.currentLevel] || levels[currentHls.loadLevel] || levels[0];
      const fragments = level && level.details && level.details.fragments;
      if (fragments && fragments.length && Number.isFinite(fragments[0].start)) {
        return fragments[0].start;
      }
      return null;
    }
    return video.seekable.length ? video.seekable.start(0) : null;
  }

  // Playback position of the live edge: what plays when fully caught up.
  function liveEdge() {
    if (currentHls) {
      return Number.isFinite(currentHls.liveSyncPosition) ? currentHls.liveSyncPosition : null;
    }
    return video.seekable.length ? video.seekable.end(video.seekable.length - 1) : null;
  }

  // The server keeps recording while paused, within the DVR retention
  // window (cfg.liveDvrMins). If the paused position has fallen out of
  // the window by resume time, continue at the oldest available
  // position instead of jumping to the live edge.
  function setupLiveDvrResume() {
    if (cfg.isVod || !cfg.liveDvrMins) return;

    let pausedDuringSession = false;
    video.addEventListener('pause', () => { pausedDuringSession = true; });
    video.addEventListener('play', () => {
      if (!pausedDuringSession) return;
      const start = liveWindowStart();
      if (start === null || video.currentTime >= start) return;
      video.currentTime = start + 0.1;
    });
  }

  // ============================================================
  // Live Program Timeline
  // ============================================================

  // Live progress spans the scheduled EPG program, not the DVR segment
  // window: elapsed and remaining come from the program's start and
  // stop times, so pausing or rewinding moves the position back through
  // the program instead of restarting a segment counter.
  function hasLiveProgram() {
    return !cfg.isVod && programEnd > programStart;
  }

  // Wall-clock time of the frame on screen. Live playback trails the
  // broadcast by however far the current position sits behind the live
  // edge, which is also how a paused picture keeps its place.
  function playbackWallClock() {
    const now = Date.now() / 1000;
    const edge = liveEdge();
    if (edge === null || !Number.isFinite(video.currentTime)) return now;
    return now - Math.max(0, edge - video.currentTime);
  }

  // Seek to a moment of the broadcast, clamped to what the DVR still holds.
  function seekToWallClock(target) {
    const edge = liveEdge();
    if (edge === null) return;
    const start = liveWindowStart();
    const oldest = start === null ? 0 : start + 0.1;
    const position = edge - (Date.now() / 1000 - target);
    video.currentTime = Math.min(Math.max(position, oldest), edge);
  }

  // The guide only supplies the program that was on at page load, so
  // pull the next one once its stop time passes; a gap in the listings
  // is retried at a slow cadence.
  async function refreshProgram() {
    const now = Date.now() / 1000;
    if (programRefreshInFlight || !cfg.streamId || now < programRetryAt) return;
    programRefreshInFlight = true;
    programRetryAt = now + 30;
    try {
      const resp = await fetch('/api/live/program/' + encodeURIComponent(cfg.streamId));
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.end || data.end <= Date.now() / 1000) return;
      programStart = data.start;
      programEnd = data.end;
      const title = document.getElementById('program-title');
      if (title) title.textContent = data.title ? ' \u2014 ' + data.title : '';
      const desc = document.getElementById('program-desc');
      if (desc) {
        desc.textContent = data.desc || '';
        desc.classList.toggle('hidden', !data.desc);
      }
      updateProgress();
    } catch (e) {
      // Keep the current listing and try again on a later tick.
    } finally {
      programRefreshInFlight = false;
    }
  }

  function setupLiveProgram() {
    if (cfg.isVod) return;
    const container = document.getElementById('progress-container');
    // Wall-clock driven, so the bar keeps up while paused or stalled.
    const tick = () => {
      // Channels the guide knows nothing about stay off the timeline.
      if (programEnd && Date.now() / 1000 >= programEnd) refreshProgram();
      container?.classList.toggle('hidden', !hasLiveProgram());
      if (hasLiveProgram()) updateProgress();
    };
    tick();
    setInterval(tick, 1000);
  }

  // ============================================================
  // Progress Bar
  // ============================================================

  function renderProgress(pct, currentText, rightText) {
    const played = document.getElementById('progress-played');
    const handle = document.getElementById('progress-handle');
    const timeCurrent = document.getElementById('time-current');
    const timeDuration = document.getElementById('time-duration');
    if (played) played.style.width = pct + '%';
    if (handle) handle.style.left = pct + '%';
    if (timeCurrent) timeCurrent.textContent = currentText;
    if (timeDuration) timeDuration.textContent = rightText;
  }

  // Shade the part of the program the DVR can still seek to.
  function renderDvrWindow(length) {
    const buffered = document.getElementById('progress-buffered');
    if (!buffered) return;
    const edge = liveEdge();
    const start = liveWindowStart();
    const now = Date.now() / 1000;
    const live = Math.min(Math.max(now - programStart, 0), length);
    const oldest = edge === null || start === null
      ? live
      : Math.min(Math.max(now - (edge - start) - programStart, 0), length);
    buffered.style.left = (oldest / length) * 100 + '%';
    buffered.style.width = ((live - oldest) / length) * 100 + '%';
  }

  function updateProgress() {
    if (hasLiveProgram()) {
      const length = programEnd - programStart;
      const elapsed = Math.min(Math.max(playbackWallClock() - programStart, 0), length);
      renderProgress((elapsed / length) * 100, formatTime(elapsed), '-' + formatTime(length - elapsed));
      renderDvrWindow(length);
      return;
    }
    const duration = totalDuration || video.duration || 0;
    if (!duration) return;
    const currentTime = video.currentTime + seekOffset;
    renderProgress((currentTime / duration) * 100, formatTime(currentTime), formatTime(duration));
  }

  // ============================================================
  // Progress Polling
  // ============================================================

  function startProgressPolling() {
    if (progressPollTimerId) clearInterval(progressPollTimerId);
    const poll = async () => {
      if (!transcodeSessionId) return;
      try {
        const resp = await fetch('/transcode/progress/' + transcodeSessionId);
        if (resp.ok) {
          const data = await resp.json();
          transcodedDuration = data.duration || 0;
        }
      } catch (e) {}
    };
    poll();
    progressPollTimerId = setInterval(poll, 2000);
  }

  function stopProgressPolling() {
    if (progressPollTimerId) {
      clearInterval(progressPollTimerId);
      progressPollTimerId = null;
    }
  }

  // ============================================================
  // Transcode Management
  // ============================================================

  async function cleanupTranscode() {
    if (document.pictureInPictureElement === video) return;
    adaptivePlayback?.destroy();
    adaptivePlayback = null;
    adaptiveSession = null;
    transcodePlaylist = null;
    stopProgressPolling();
    if (subtitlePollTimerId) {
      clearInterval(subtitlePollTimerId);
      subtitlePollTimerId = null;
    }
    if (currentHls) {
      currentHls.destroy();
      currentHls = null;
    }
    stopSeekWait();
    failedSeekTarget = null;
    transcodedDuration = 0;
    totalDuration = 0;
    seekInProgress = false;
    seekOffset = 0;
    currentSubtitles = null;
    activeTrackStates = null;
    ccSelectedIdx = null;
    closeTrackMenus();
    updateAudioButton();
    await transcodeSession.stop();
    transcodeSessionId = null;
  }

  function cleanupTranscodeSync() {
    if (document.pictureInPictureElement === video) return;
    adaptivePlayback?.destroy();
    adaptivePlayback = null;
    stopProgressPolling();
    if (subtitlePollTimerId) {
      clearInterval(subtitlePollTimerId);
      subtitlePollTimerId = null;
    }
    if (currentHls) {
      currentHls.destroy();
      currentHls = null;
    }
    transcodeSession.close();
    transcodeSessionId = null;
  }

  // Streamed seeks restart ffmpeg on the server, which can take a while (or stall
  // on the provider side). The newest click wins, the old picture stays on screen
  // until the new stream is ready, and a failure can be retried in place.
  const SEEK_DEBOUNCE_MS = 200;
  const SEEK_CLIENT_RETRIES = 1;
  let seekRequestId = 0;
  let seekAbort = null;
  let failedSeekTarget = null;

  function showSeekStatus(text) {
    const label = document.getElementById('loading-label');
    if (!label) return;
    label.textContent = text || '';
    label.classList.toggle('hidden', !text);
  }

  function seekDelay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  // Ask the server to restart at targetTime; one automatic retry on failure
  // (the server already retries with a fresh ffmpeg before answering 504).
  async function requestSeekOnServer(targetTime, signal) {
    let lastError = null;
    for (let attempt = 0; attempt <= SEEK_CLIENT_RETRIES; attempt++) {
      try {
        const resp = await fetch('/transcode/seek/' + transcodeSessionId + '?time=' + targetTime, {signal});
        if (resp.ok) return;
        lastError = new Error('Seek failed: ' + resp.status);
        // session gone or replaced by a newer seek: retrying cannot help
        if (resp.status === 404 || resp.status === 409 || resp.status === 401) break;
      } catch (e) {
        if (e.name === 'AbortError') throw e;
        lastError = e;
      }
      if (attempt < SEEK_CLIENT_RETRIES) {
        showSeekStatus('Retrying...');
        await seekDelay(400);
        if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      }
    }
    throw lastError;
  }

  function stopSeekWait() {
    seekRequestId++;
    if (seekAbort) {
      seekAbort.abort();
      seekAbort = null;
    }
  }

  async function handleSeekToPosition(targetTime) {
    if (!transcodeSessionId) return false;
    // Whole seconds: the clicked pixel has no finer meaning, and it keeps
    // repeated seeks to the same spot identical for the server's segment reuse.
    targetTime = Math.max(0, Math.floor(targetTime));
    stopSeekWait();
    const myId = seekRequestId;
    const abort = new AbortController();
    seekAbort = abort;
    seekInProgress = true;
    failedSeekTarget = null;
    showLoading();
    showSeekStatus('Seeking...');
    error.classList.add('hidden');
    video.pause();
    // Keep the last picture: only stop the old stream from loading while we wait
    currentHls?.stopLoad();
    if (subtitlePollTimerId) {
      clearInterval(subtitlePollTimerId);
      clearTimeout(subtitlePollTimerId);
      subtitlePollTimerId = null;
    }
    try {
      await seekDelay(SEEK_DEBOUNCE_MS);
      if (myId !== seekRequestId) return false;
      await requestSeekOnServer(targetTime, abort.signal);
    } catch (e) {
      if (myId !== seekRequestId) return false; // a newer seek took over
      console.error('[SEEK] Error:', e);
      seekInProgress = false;
      failedSeekTarget = targetTime;
      showSeekStatus('');
      showError('Seek failed', true);
      return false;
    }
    if (myId !== seekRequestId) return false;
    seekAbort = null;
    showSeekStatus('');
    video.src = '';
    if (currentHls) {
      currentHls.destroy();
      currentHls = null;
    }
    transcodedDuration = 0;
    seekOffset = targetTime;
    const hls = new Hls(createHlsConfig({forSeek: true}));
    currentHls = hls;
    wireTrackPrefs(hls); // a new hls.js instance starts on the default audio: re-apply the language

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      hideLoading();
      error.classList.add('hidden');
      seekInProgress = false;
      savePosition();
      if (currentSubtitles && currentSubtitles.length > 0) {
        startSubtitlePolling(currentSubtitles, getPreferredSubtitleTrack(currentSubtitles));
      }
      video.play().catch(() => {});
    });

    let recoveryAttempts = 0;
    hls.on(Hls.Events.ERROR, (event, data) => {
      if (!data.fatal) return;
      recoveryAttempts++;
      if (recoveryAttempts <= 3) {
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else failSeekPlayback(targetTime, data);
      } else {
        failSeekPlayback(targetTime, data);
      }
    });

    hls.loadSource('/transcode/' + transcodeSessionId + '/stream.m3u8');
    hls.attachMedia(video);
    return true;
  }

  function failSeekPlayback(targetTime, data) {
    console.error('[SEEK] HLS error:', data);
    // A stuck flag used to swallow every later seek after a fatal error
    seekInProgress = false;
    failedSeekTarget = targetTime;
    showError('Seek failed', true);
  }

  async function startTranscode(onError) {
    showLoading();
    try {
      await cleanupTranscode();
      let url = '/transcode/start?url=' + encodeURIComponent(cfg.rawUrl) + '&content_type=' + cfg.streamType;
      if (cfg.seriesId) url += '&series_id=' + cfg.seriesId;
      if (cfg.episodeId) url += '&episode_id=' + cfg.episodeId;
      if (cfg.seriesName) url += '&series_name=' + encodeURIComponent(cfg.seriesName);
      if (cfg.deinterlaceFallback !== undefined) url += '&deinterlace_fallback=' + (cfg.deinterlaceFallback ? '1' : '0');
      if (cfg.sourceId) url += '&source_id=' + encodeURIComponent(cfg.sourceId);
      if (!cfg.isVod) url += '&fast_start=true';
      const data = await transcodeSession.start(url);
      if (!data) return;
      transcodeSessionId = data.session_id;
      transcodePlaylist = data.playlist;
      adaptiveSession = !cfg.isVod && data.playlist.endsWith('/low.m3u8') ? data : null;
      isTranscoding = true;
      updateTranscodeCheck();
      totalDuration = data.duration || 0;
      seekOffset = data.seek_offset || 0;
      transcodedDuration = data.transcoded_duration || 0;
      currentSubtitles = data.subtitles || null;
      if (cfg.isVod) {
        document.getElementById('progress-container')?.classList.remove('hidden');
        if (totalDuration > 0) {
          document.getElementById('time-duration').textContent = formatTime(totalDuration);
        }
        enableCcButton();
      }
      playWithUrl(data.master_playlist || data.playlist, onError, data.subtitles);
    } catch (e) {
      console.error('[TC] Error:', e);
      if (onError) onError();
      else showError();
    }
  }

  // ============================================================
  // Playback
  // ============================================================

  function playWithUrl(url, onError, subtitles) {
    showLoading();
    adaptivePlayback?.destroy();
    adaptivePlayback = null;
    const useHls = Hls.isSupported() && (
      url.includes('.m3u8') || url.includes('/live/') || url.includes('/transcode')
    );
    if (!useHls) {
      // Native HLS without download telemetry stays on the safe initial rendition.
      video.src = adaptiveSession ? adaptiveSession.playlist : url;
      if (adaptiveSession) startAdaptivePlayback(null);
      video.addEventListener('loadedmetadata', function() {
        hideLoading();
        error.classList.add('hidden');
        if (video.textTracks.length === 0) disableCcButton();
        applyCaptionsSetting();
        setupNativeAudioTracks();
        restorePosition();
        if (transcodeSessionId && !adaptiveSession) startProgressPolling();
        video.play().catch(() => { if (!video.muted) { autoMutedByPolicy = true; video.muted = true; } video.play(); });
      }, { once: true });
      video.addEventListener('error', function() {
        if (onError) onError();
        else showError();
      }, { once: true });
      return;
    }

    const hls = new Hls({
      ...createHlsConfig({isVod: cfg.isVod}),
      ...(adaptiveSession ? { liveSyncDuration: 12, liveSyncDurationCount: undefined } : {}),
    });
    currentHls = hls;
    let recoveryAttempts = 0;
    let hasLoaded = false;

    if (adaptiveSession) {
      // Older servers can supply low.m3u8 without a master; keep their session
      // alive but do not request upgrades that cannot be switched safely.
      startAdaptivePlayback(adaptiveSession.master_playlist ? hls : null);
    }

    // Timeout for initial load (Auto mode only)
    let loadTimeout = null;
    if (onError) {
      loadTimeout = setTimeout(() => {
        if (!hasLoaded) {
          console.log('[AUTO] Load timeout, triggering transcode');
          hls.destroy();
          currentHls = null;
          onError();
        }
      }, 10000);
    }

    // Check for missing audio (Auto mode only)
    if (onError) {
      let audioChecked = false;
      video.addEventListener('timeupdate', function checkAudio() {
        if (audioChecked || video.currentTime < 1) return;
        audioChecked = true;
        video.removeEventListener('timeupdate', checkAudio);
        let hasAudio = false;
        if (typeof video.webkitAudioDecodedByteCount !== 'undefined') {
          hasAudio = video.webkitAudioDecodedByteCount > 0;
        } else if (typeof video.mozHasAudio !== 'undefined') {
          hasAudio = video.mozHasAudio;
        } else if (video.audioTracks && video.audioTracks.length > 0) {
          hasAudio = true;
        }
        console.log('[AUTO] Audio check: hasAudio=' + hasAudio + ', webkitAudioDecodedByteCount=' + video.webkitAudioDecodedByteCount);
        if (!hasAudio) {
          console.log('[AUTO] No audio detected, triggering transcode');
          hls.destroy();
          currentHls = null;
          onError();
        }
      });
    }

    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (loadTimeout) clearTimeout(loadTimeout);
      hideLoading();
      error.classList.add('hidden');
      hasLoaded = true;
      recoveryAttempts = 0;
      if (subtitles && subtitles.length > 0) {
        startSubtitlePolling(subtitles, getPreferredSubtitleTrack(subtitles));
      }
      if (cfg.captionsEnabled && hls.subtitleTracks.length > 0) {
        hls.subtitleTrack = getPreferredSubtitleTrack(hls.subtitleTracks);
      }
      if (transcodeSessionId && !adaptiveSession) startProgressPolling();
      restorePosition();
      video.play().catch(() => { if (!video.muted) { autoMutedByPolicy = true; video.muted = true; } video.play(); });
      setTimeout(() => {
        if (hls.subtitleTracks.length === 0 && video.textTracks.length === 0) disableCcButton();
      }, 1000);
    });

    wireTrackPrefs(hls);

    hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED, () => {
      refreshOpenMenu();
      if (hls.subtitleTracks.length > 0 && ccBtn.disabled) {
        enableCcButton();
        if (cfg.captionsEnabled) hls.subtitleTrack = getPreferredSubtitleTrack(hls.subtitleTracks);
      } else if (hls.subtitleTracks.length === 0 && !ccBtn.disabled) {
        disableCcButton();
      }
    });

    video.textTracks.addEventListener('addtrack', (e) => {
      if (e.track.kind === 'captions' || e.track.kind === 'subtitles') {
        applyCaptionsSetting();
        if (ccBtn.disabled) enableCcButton();
      }
    });

    hls.on(Hls.Events.ERROR, (event, data) => {
      if (data.fatal) {
        recoveryAttempts++;
        if (recoveryAttempts <= 3) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
          else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
          else {
            hls.destroy();
            currentHls = null;
            if (!hasLoaded && onError) onError();
            else showError();
          }
        } else {
          hls.destroy();
          currentHls = null;
          if (!hasLoaded && onError) onError();
          else showError();
        }
      }
    });
    hls.loadSource(url);
    hls.attachMedia(video);
  }

  function startAdaptivePlayback(hls) {
    adaptivePlayback = new window.NetvPlayback.AdaptiveLivePlayback({
      video,
      hls,
      sessionId: transcodeSessionId,
      playlist: adaptiveSession.playlist,
      onPlaylist: playlist => { transcodePlaylist = playlist; },
    });
  }

  // ============================================================
  // Audio and subtitle menus
  // ============================================================

  const TRACK_MENUS = {
    audio: { btn: audioBtn, menu: audioMenu, title: 'Audio' },
    cc: { btn: ccBtn, menu: ccMenu, title: 'Sous-titres' },
  };

  function anyMenuOpen() {
    return settingsMenu.classList.contains('open') ||
      audioMenu.classList.contains('open') || ccMenu.classList.contains('open');
  }

  function openMenuKind() {
    return Object.keys(TRACK_MENUS).find(k => TRACK_MENUS[k].menu.classList.contains('open')) || null;
  }

  // Language picked in this page (kept in memory too, so it survives seeks even without storage)
  let sessionAudioLang = '';

  function loadSavedAudioLang() {
    if (sessionAudioLang) return sessionAudioLang;
    try { return localStorage.getItem(AUDIO_LANG_KEY) || ''; } catch { return ''; }
  }

  function saveAudioLang(lang) {
    sessionAudioLang = lang;
    try { localStorage.setItem(AUDIO_LANG_KEY, lang); } catch { /* storage unavailable */ }
  }

  // Audio choice for one hls.js instance. Seeks create a new instance that starts on the
  // manifest default, so this runs again after every seek. Order: language picked last,
  // original language, English, French, first track.
  function wireTrackPrefs(hls) {
    let applied = false;
    const apply = () => {
      if (!applied && hls.audioTracks?.length > 1) {
        applied = true;
        const idx = Tracks.pickDefaultAudio(hls.audioTracks, {
          preferred: loadSavedAudioLang(),
          original: cfg.originalLang,
        });
        if (idx >= 0 && idx !== hls.audioTrack) hls.audioTrack = idx;
      }
      updateAudioButton();
    };
    hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, apply);
    hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, () => updateAudioButton());
    hls.on(Hls.Events.MANIFEST_PARSED, apply);
  }

  // Audio tracks from hls.js, or from the native API (Safari) as a fallback.
  function getAudioTracks() {
    if (currentHls && currentHls.audioTracks && currentHls.audioTracks.length > 0) {
      const active = currentHls.audioTrack;
      return currentHls.audioTracks.map((t, i) => ({
        idx: i, lang: t.lang, name: t.name, active: i === active,
        original: t.original === true || t.attrs?.['X-ORIGINAL'] === 'YES',
        label: Tracks.trackLabel(t.lang, t.name, `Piste ${i + 1}`),
      }));
    }
    const native = video.audioTracks;
    if (native && native.length > 0) {
      return Array.from(native).map((t, i) => ({
        idx: i, lang: t.language, name: t.label, active: !!t.enabled,
        original: t.original === true,
        label: Tracks.trackLabel(t.language, t.label, `Piste ${i + 1}`),
      }));
    }
    return [];
  }

  const AUDIO_BTN_TITLE = audioBtn.title || 'Audio (l)';

  // Original version (VO) among the given audio tracks, or null. The backend
  // supplies cfg.originalLang / cfg.originalLangSource; both may be absent.
  function getOriginalInfo(tracks) {
    return Tracks.findOriginal(tracks, {
      originalLang: cfg.originalLang || '',
      source: cfg.originalLangSource || '',
    });
  }

  function updateAudioButton() {
    const show = getAudioTracks().length >= 2;
    audioBtn.classList.toggle('hidden', !show);
    audioBtn.title = Tracks.audioButtonTitle(AUDIO_BTN_TITLE, show ? getOriginalInfo(getAudioTracks()) : null);
    if (!show && audioMenu.classList.contains('open')) closeTrackMenus();
    refreshOpenMenu();
  }

  function setupNativeAudioTracks() {
    const native = video.audioTracks;
    if (!native || currentHls) return;
    updateAudioButton();
    if (native.addEventListener) {
      native.addEventListener('addtrack', updateAudioButton);
      native.addEventListener('removetrack', updateAudioButton);
    }
    const tracks = getAudioTracks();
    const idx = Tracks.findAudioByLang(tracks, loadSavedAudioLang());
    if (tracks.length > 1 && idx >= 0 && !tracks[idx].active) selectAudio(idx, false);
  }

  function selectAudio(idx, persist = true) {
    const tracks = getAudioTracks();
    const track = tracks[idx];
    if (!track) return;
    if (currentHls && currentHls.audioTracks && currentHls.audioTracks.length > 0) {
      currentHls.audioTrack = idx;
    } else if (video.audioTracks) {
      Array.from(video.audioTracks).forEach((t, i) => { t.enabled = i === idx; });
    }
    if (persist) {
      const key = Tracks.langKey(track);
      if (key) saveAudioLang(key);
    }
    updateAudioButton();
  }

  function getCcTracks() {
    const tracks = [];
    if (activeTrackStates?.length > 0) {
      activeTrackStates.forEach((ts, i) => tracks.push({ idx: i, lang: ts.track.language, name: ts.track.label }));
    } else if (currentHls?.subtitleTracks?.length > 0) {
      currentHls.subtitleTracks.forEach((t, i) => tracks.push({ idx: i, lang: t.lang, name: t.name }));
    } else {
      Array.from(video.textTracks).filter(t => t.kind === 'subtitles' || t.kind === 'captions')
        .forEach((t, i) => tracks.push({ idx: i, lang: t.language, name: t.label }));
    }
    return tracks.map(t => ({ ...t, label: Tracks.trackLabel(t.lang, t.name, `Piste ${t.idx + 1}`) }));
  }

  // Index of the subtitle track currently shown, -1 when subtitles are off.
  function getActiveCcIdx() {
    if (!ccEnabled) return -1;
    if (activeTrackStates?.length > 0) return activeTrackStates.findIndex(ts => ts.track.mode === 'showing');
    if (currentHls?.subtitleTracks?.length > 0 && currentHls.subtitleTrack >= 0) return currentHls.subtitleTrack;
    const tracks = Array.from(video.textTracks).filter(t => t.kind === 'subtitles' || t.kind === 'captions');
    return tracks.findIndex(t => t.mode === 'showing');
  }

  function setCcTrack(idx) {
    // idx -1 means subtitles off
    ccEnabled = idx >= 0;
    ccSelectedIdx = idx >= 0 ? idx : ccSelectedIdx;
    const filtered = () => Array.from(video.textTracks).filter(t => t.kind === 'subtitles' || t.kind === 'captions');
    if (activeTrackStates?.length > 0) {
      activeTrackStates.forEach((ts, i) => ts.track.mode = i === idx ? 'showing' : 'hidden');
    } else {
      if (currentHls?.subtitleTracks?.length > 0) {
        currentHls.subtitleDisplay = ccEnabled;
        currentHls.subtitleTrack = idx;
      }
      // iOS: also set TextTrack.mode directly (HLS.js property change doesn't always trigger render)
      filtered().forEach((t, i) => t.mode = i === idx ? 'showing' : 'hidden');
    }
    updateCcButton();
    saveSettings({ ccEnabled });
  }

  // Shift+C: turn subtitles off, or on with the default track (English SDH, else English, else French).
  function toggleDefaultCaptions() {
    if (ccBtn.disabled) return;
    if (ccEnabled && getActiveCcIdx() >= 0) { setCcTrack(-1); return; }
    const idx = Tracks.pickDefaultSubtitle(getCcTracks());
    if (idx >= 0) setCcTrack(idx);
  }

  function menuEntries(kind) {
    if (kind === 'audio') {
      const tracks = getAudioTracks();
      const vo = getOriginalInfo(tracks);
      return tracks.map((t, i) => ({
        label: t.label, checked: t.active, onSelect: () => selectAudio(t.idx),
        original: vo && vo.idx === i ? vo : null,
      }));
    }
    const active = getActiveCcIdx();
    const entries = [{ label: 'Désactivés', checked: active < 0, onSelect: () => setCcTrack(-1) }];
    getCcTracks().forEach(t => entries.push({ label: t.label, checked: t.idx === active, onSelect: () => setCcTrack(t.idx) }));
    return entries;
  }

  function renderTrackMenu(kind) {
    const { menu } = TRACK_MENUS[kind];
    menu.textContent = '';
    for (const entry of menuEntries(kind)) {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'settings-item';
      item.setAttribute('role', 'menuitemradio');
      item.setAttribute('aria-checked', entry.checked ? 'true' : 'false');
      const check = document.createElement('span');
      check.className = 'settings-check';
      check.textContent = entry.checked ? '✓' : '';
      item.appendChild(check);
      const text = document.createElement('span');
      text.className = 'track-label';
      text.textContent = entry.label;
      item.appendChild(text);
      if (entry.original) {
        const tip = Tracks.originalTitle(entry.original);
        const badge = document.createElement('span');
        badge.className = 'vo-badge' + (entry.original.probable ? ' probable' : '');
        badge.textContent = entry.original.probable ? 'VO ?' : 'VO';
        badge.setAttribute('aria-hidden', 'true');
        item.appendChild(badge);
        item.classList.add('is-original');
        item.title = tip;
        item.setAttribute('aria-label', `${entry.label}, ${tip.charAt(0).toLowerCase() + tip.slice(1)}`);
      }
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        entry.onSelect();
        closeTrackMenus();
      });
      menu.appendChild(item);
    }
  }

  function refreshOpenMenu() {
    const kind = openMenuKind();
    if (!kind) return;
    const menu = TRACK_MENUS[kind].menu;
    const focusedIdx = Array.from(menu.children).indexOf(document.activeElement);
    renderTrackMenu(kind);
    if (focusedIdx >= 0) menu.children[focusedIdx]?.focus();
  }

  function closeTrackMenus(refocus = false) {
    const kind = openMenuKind();
    for (const k of Object.keys(TRACK_MENUS)) {
      TRACK_MENUS[k].menu.classList.remove('open');
      TRACK_MENUS[k].btn.setAttribute('aria-expanded', 'false');
    }
    if (kind && refocus) TRACK_MENUS[kind].btn.focus();
  }

  function toggleTrackMenu(kind) {
    const wasOpen = TRACK_MENUS[kind].menu.classList.contains('open');
    closeTrackMenus();
    settingsMenu.classList.remove('open');
    if (wasOpen) return;
    const { btn, menu } = TRACK_MENUS[kind];
    renderTrackMenu(kind);
    menu.classList.add('open');
    btn.setAttribute('aria-expanded', 'true');
    document.getElementById('player-container').classList.add('controls-visible');
    const items = Array.from(menu.children);
    (items.find(i => i.getAttribute('aria-checked') === 'true') || items[0])?.focus();
  }

  // Keyboard navigation inside an open track menu. Returns true when the key was consumed.
  function handleTrackMenuKey(e) {
    const kind = openMenuKind();
    if (!kind) return false;
    const menu = TRACK_MENUS[kind].menu;
    const items = Array.from(menu.children);
    const cur = items.indexOf(document.activeElement);
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        items[cur < 0 ? 0 : (cur + 1) % items.length]?.focus();
        return true;
      case 'ArrowUp':
        e.preventDefault();
        items[cur < 0 ? items.length - 1 : (cur - 1 + items.length) % items.length]?.focus();
        return true;
      case 'Home': e.preventDefault(); items[0]?.focus(); return true;
      case 'End': e.preventDefault(); items[items.length - 1]?.focus(); return true;
      case 'Escape':
        e.preventDefault();
        closeTrackMenus(true);
        return true;
      case 'Tab':
        closeTrackMenus();
        return false;
      case 'Enter':
      case ' ':
        // Native button activation handles a focused item; without focus, pick nothing.
        if (cur >= 0) return true;
        e.preventDefault();
        return true;
      case 'ArrowLeft':
      case 'ArrowRight':
        // Do not seek behind an open menu.
        e.preventDefault();
        return true;
      default:
        return false;
    }
  }

  // ============================================================
  // Controls
  // ============================================================

  function setupKeyboardControls() {
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
        if (e.key === 'Escape') e.target.blur();
        return;
      }
      if (handleTrackMenuKey(e)) return;
      switch(e.key) {
        case ' ':
        case 'k':
          if (e.target.tagName === 'BUTTON' || e.target.tagName === 'A') return;
          e.preventDefault();
          video.paused ? video.play() : video.pause();
          break;
        case 'ArrowLeft': e.preventDefault(); video.currentTime -= SEEK_STEP; break;
        case 'ArrowRight': e.preventDefault(); video.currentTime += SEEK_STEP; break;
        case 'ArrowUp': e.preventDefault(); video.volume = Math.min(1, video.volume + 0.1); break;
        case 'ArrowDown': e.preventDefault(); video.volume = Math.max(0, video.volume - 0.1); break;
        case 'f':
          e.preventDefault();
          suppressShowControls = true;
          setTimeout(() => suppressShowControls = false, 150);
          if (document.fullscreenElement) document.exitFullscreen();
          else document.getElementById('player-container').requestFullscreen();
          break;
        case 'm': video.muted = !video.muted; updateMuteIcon(); break;
        case 't': document.getElementById('menu-transcode')?.click(); break;
        case 'c': if (!ccBtn.disabled) ccBtn.click(); break;
        case 'C': toggleDefaultCaptions(); break; // Shift+C: subtitles on (English SDH) / off
        case 'l': if (!audioBtn.classList.contains('hidden')) audioBtn.click(); break;
        case 'i': document.getElementById('info-btn')?.click(); break;
        case 'd': {
          const dlBtn = document.getElementById('download-btn');
          if (dlBtn && !dlBtn.hidden) dlBtn.click();
          break;
        }
        case 'a': document.getElementById('cast-btn')?.click(); break;
        case 'x': document.getElementById('menu-restart')?.click(); break;
        case 'n': document.getElementById('autonext-btn')?.click(); break;
        case 'p': document.getElementById('pip-btn')?.click(); break;
        case 'h':
          const container = document.getElementById('player-container');
          if (container.classList.contains('controls-visible')) {
            container.classList.remove('controls-visible');
            clearTimeout(activityTimeoutId);
            activityTimeoutId = null;
          } else {
            container.classList.add('controls-visible');
            clearTimeout(activityTimeoutId);
            activityTimeoutId = setTimeout(() => {
              if (!anyMenuOpen()) {
                container.classList.remove('controls-visible');
                activityTimeoutId = null;
              }
            }, 3000);
          }
          break;
        case 'Escape': {
          const infoOverlay = document.getElementById('info-overlay');
          if (infoOverlay?.classList.contains('pinned')) {
            infoOverlay.classList.remove('pinned');
            document.getElementById('info-btn')?.classList.remove('active');
            saveSettings({ infoPinned: false });
          } else if (settingsMenu.classList.contains('open')) {
            settingsMenu.classList.remove('open');
          } else if (!document.fullscreenElement) {
            if (history.length > 1) history.back();
            else window.location.href = '/guide';
          }
          break;
        }
      }
    });
  }

  let activityTimeoutId = null;
  let autoNextEnabled = true;
  let suppressShowControls = false;

  // Persistent settings
  const STORAGE_KEY = 'playerSettings';
  function loadSettings() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
    } catch { return {}; }
  }
  function saveSettings(updates) {
    const settings = { ...loadSettings(), ...updates };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  }

  function setupActivityTracking() {
    const container = document.getElementById('player-container');
    const HIDE_DELAY = 3000;

    function showControls() {
      if (suppressShowControls) return;
      container.classList.add('controls-visible');
      clearTimeout(activityTimeoutId);
      activityTimeoutId = setTimeout(hideControls, HIDE_DELAY);
    }

    function hideControls() {
      if (anyMenuOpen()) return;
      container.classList.remove('controls-visible');
      activityTimeoutId = null;
    }

    container.addEventListener('mousemove', showControls);
    container.addEventListener('mouseenter', showControls);
    container.addEventListener('click', showControls);
    container.addEventListener('mouseleave', () => {
      clearTimeout(activityTimeoutId);
      activityTimeoutId = null;
      hideControls();
    });
    showControls();
  }

  function setupButtonHandlers() {
    // Play/Pause button
    document.getElementById('play-btn')?.addEventListener('click', () => {
      video.paused ? video.play() : video.pause();
    });

    // Click video to toggle play/pause and show controls
    video.addEventListener('click', (e) => {
      if (e.target !== video) return;
      video.paused ? video.play() : video.pause();
      const container = document.getElementById('player-container');
      container.classList.add('controls-visible');
      clearTimeout(activityTimeoutId);
      activityTimeoutId = setTimeout(() => {
        if (!anyMenuOpen()) {
          container.classList.remove('controls-visible');
          activityTimeoutId = null;
        }
      }, 3000);
    });

    // Mute button
    document.getElementById('mute-btn')?.addEventListener('click', () => {
      video.muted = !video.muted;
      updateMuteIcon();
      saveSettings({ muted: video.muted });
    });

    // Volume slider
    const volSlider = document.getElementById('volume-slider');
    volSlider?.addEventListener('input', () => {
      video.volume = parseFloat(volSlider.value);
      video.muted = false;
      updateMuteIcon();
      saveSettings({ volume: video.volume, muted: false });
    });
    video.addEventListener('volumechange', () => {
      if (volSlider) volSlider.value = video.muted ? 0 : video.volume;
    });

    // Auto-next button
    const autoNextBtn = document.getElementById('autonext-btn');
    autoNextBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      autoNextEnabled = !autoNextEnabled;
      autoNextBtn.classList.toggle('active', autoNextEnabled);
    });

    // Info button
    const infoOverlay = document.getElementById('info-overlay');
    const infoBtn = document.getElementById('info-btn');
    infoBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasPinned = infoOverlay.classList.contains('pinned');
      infoOverlay.classList.toggle('pinned');
      infoBtn.classList.toggle('active', !wasPinned);
      saveSettings({ infoPinned: !wasPinned });
      if (wasPinned && !activityTimeoutId) {
        document.getElementById('player-container').classList.remove('controls-visible');
      }
    });

    // CC and audio buttons open their dropdown menus
    ccBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleTrackMenu('cc'); });
    audioBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleTrackMenu('audio'); });

    // PiP button
    const pipBtn = document.getElementById('pip-btn');
    if (pipBtn && document.pictureInPictureEnabled) {
      pipBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          if (document.pictureInPictureElement) {
            await document.exitPictureInPicture();
          } else {
            await video.requestPictureInPicture();
          }
        } catch (err) {
          console.error('[PiP] Error:', err);
        }
      });
    } else if (pipBtn) {
      pipBtn.style.display = 'none';
    }

    // Settings menu toggle
    document.getElementById('settings-btn')?.addEventListener('click', () => {
      closeTrackMenus();
      settingsMenu.classList.toggle('open');
    });

    // Close settings menu when clicking outside
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#settings-btn') && !e.target.closest('#settings-menu')) {
        settingsMenu.classList.remove('open');
      }
      if (!e.target.closest('.track-menu')) closeTrackMenus();
    });

    // Settings menu items
    document.getElementById('menu-transcode')?.addEventListener('click', async () => {
      settingsMenu.classList.remove('open');
      video.pause();
      video.src = '';
      error.classList.add('hidden');
      if (isTranscoding) {
        try {
          await cleanupTranscode();
          isTranscoding = false;
          updateTranscodeCheck();
          playWithUrl(cfg.rawUrl);
        } catch (e) {
          console.error('[TC] Unable to stop transcode:', e);
          showError();
        }
      } else {
        await startTranscode();
      }
    });

    document.getElementById('menu-restart')?.addEventListener('click', async () => {
      settingsMenu.classList.remove('open');
      video.pause();
      video.src = '';
      error.classList.add('hidden');
      try {
        await fetch('/transcode-clear?url=' + encodeURIComponent(cfg.rawUrl), {method: 'DELETE'});
        await cleanupTranscode();
        isTranscoding = false;
        await startTranscode();
      } catch (e) {
        console.error('[X] Error:', e);
        showError();
      }
    });

    document.getElementById('menu-url')?.addEventListener('click', () => {
      settingsMenu.classList.remove('open');
      if (navigator.clipboard) {
        navigator.clipboard.writeText(cfg.rawUrl).catch(fallback);
      } else {
        fallback();
      }
      function fallback() {
        const ta = document.createElement('textarea');
        ta.value = cfg.rawUrl;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
    });

    document.getElementById('menu-external')?.addEventListener('click', () => {
      settingsMenu.classList.remove('open');
      video.pause();
      video.src = '';
      window.location.href = '/playlist.xspf?url=' + encodeURIComponent(cfg.rawUrl);
    });

    // Fullscreen button
    document.getElementById('fullscreen-btn')?.addEventListener('click', () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.getElementById('player-container').requestFullscreen();
    });

    // Fullscreen change listener
    document.addEventListener('fullscreenchange', updateFullscreenIcon);

    // Video state listeners
    video.addEventListener('play', updatePlayIcon);
    video.addEventListener('pause', updatePlayIcon);
    video.addEventListener('volumechange', updateMuteIcon);

    // Prevent video element's native spacebar handling
    video.addEventListener('keydown', (e) => {
      if (e.key === ' ') e.preventDefault();
    });

    // Retry re-seeks to the same spot after a failed seek instead of reloading the page
    document.getElementById('error-retry')?.addEventListener('click', () => {
      if (failedSeekTarget !== null && transcodeSessionId) {
        handleSeekToPosition(failedSeekTarget);
      } else {
        location.reload();
      }
    });
    document.getElementById('error-dismiss')?.addEventListener('click', () => {
      error.classList.add('hidden');
      hideLoading();
    });

    // Progress bar
    const progressBar = document.getElementById('progress-bar');

    video.addEventListener('timeupdate', updateProgress);
    video.addEventListener('loadedmetadata', () => {
      if (cfg.isVod && !transcodeSessionId) {
        document.getElementById('progress-container')?.classList.remove('hidden');
      }
      updateProgress();
    });

    progressBar?.addEventListener('click', async (e) => {
      const rect = progressBar.getBoundingClientRect();
      const pct = (e.clientX - rect.left) / rect.width;
      // Live clicks land on a moment of the broadcast, not a file offset.
      if (hasLiveProgram()) {
        seekToWallClock(programStart + pct * (programEnd - programStart));
        updateProgress();
        return;
      }
      const duration = totalDuration || video.duration || 0;
      if (!duration) return;
      const targetTime = pct * duration;
      if (transcodeSessionId) {
        const actualTranscodedEnd = seekOffset + transcodedDuration;
        if (targetTime >= seekOffset && targetTime <= actualTranscodedEnd + 10) {
          video.currentTime = targetTime - seekOffset;
        } else {
          await handleSeekToPosition(targetTime);
        }
      } else {
        video.currentTime = targetTime;
      }
    });
  }

  // ============================================================
  // Cast (Chromecast)
  // ============================================================

  function setupCast() {
    if (!cfg.isHttps) return;
    const castBtn = document.getElementById('cast-btn');
    if (!castBtn) return;

    function getCastUrl() {
      const host = cfg.castHost || window.location.host;
      const proto = window.location.protocol;
      if (transcodeSessionId) {
        const path = transcodePlaylist || '/transcode/' + transcodeSessionId + '/stream.m3u8';
        return proto + '//' + host + path;
      }
      if (cfg.rawUrl.includes('localhost') || cfg.rawUrl.includes('127.0.0.1')) {
        return cfg.rawUrl.replace(/localhost|127\.0\.0\.1/, host.split(':')[0]);
      }
      return cfg.rawUrl;
    }

    function castLog(msg) {
      fetch('/api/cast-log', {method: 'POST', body: msg}).catch(() => {});
    }

    function initCast() {
      cast.framework.CastContext.getInstance().setOptions({
        receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
        autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED,
      });
      castBtn.disabled = false;
      cast.framework.CastContext.getInstance().addEventListener(
        cast.framework.CastContextEventType.SESSION_STATE_CHANGED, (e) => {
          const connected = e.sessionState === cast.framework.SessionState.SESSION_STARTED ||
                            e.sessionState === cast.framework.SessionState.SESSION_RESUMED;
          castBtn.classList.toggle('active', connected);
        }
      );
    }

    function loadMediaToCast() {
      const session = cast.framework.CastContext.getInstance().getCurrentSession();
      if (!session) {
        castLog('No active session');
        return;
      }
      const url = getCastUrl();
      castLog('URL: ' + url + ' isVod=' + cfg.isVod + ' seek=' + (video.currentTime + seekOffset).toFixed(1));
      const mediaInfo = new chrome.cast.media.MediaInfo(url, 'application/x-mpegurl');
      mediaInfo.streamType = chrome.cast.media.StreamType.LIVE;
      mediaInfo.metadata = new chrome.cast.media.GenericMediaMetadata();
      mediaInfo.metadata.title = cfg.mediaTitle;
      if (chrome.cast.media.HlsSegmentFormat) mediaInfo.hlsSegmentFormat = chrome.cast.media.HlsSegmentFormat.TS;
      if (chrome.cast.media.HlsVideoSegmentFormat) mediaInfo.hlsVideoSegmentFormat = chrome.cast.media.HlsVideoSegmentFormat.MPEG2_TS;
      const request = new chrome.cast.media.LoadRequest(mediaInfo);
      request.autoplay = true;
      request.currentTime = video.currentTime + seekOffset;
      castLog('streamType=' + mediaInfo.streamType);
      session.loadMedia(request).then(
        () => {
          castLog('Media loaded OK');
          video.pause();
          const media = session.getMediaSession();
          if (media) {
            media.addUpdateListener((isAlive) => {
              if (!isAlive) { castLog('Session ended'); return; }
              const ps = media.playerState;
              const idle = media.idleReason;
              castLog('State: ' + ps + (idle ? ' (' + idle + ')' : ''));
            });
          }
        },
        (e) => {
          const code = e?.code || 'unknown';
          const desc = e?.description || e?.message || String(e);
          castLog('LOAD FAILED: code=' + code + ' desc=' + desc);
        }
      );
    }

    let castDialogClosedAt = 0;
    castBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      e.preventDefault();
      this.blur();
      settingsMenu.classList.remove('open');
      if (!window.cast || !cast.framework) {
        alert('Cast not available.\n\nTry accessing via your LAN IP instead of 0.0.0.0');
        return;
      }
      if (Date.now() - castDialogClosedAt < 1000) return;
      const ctx = cast.framework.CastContext.getInstance();
      ctx.requestSession().then(
        () => { castDialogClosedAt = Date.now(); loadMediaToCast(); },
        () => { castDialogClosedAt = Date.now(); }
      );
    });

    let pollCount = 0;
    const castPoll = setInterval(() => {
      if (window.cast && cast.framework) {
        clearInterval(castPoll);
        console.log('[CAST] SDK ready');
        initCast();
      } else if (++pollCount > 30) {
        clearInterval(castPoll);
        console.log('[CAST] SDK timeout');
        castBtn.disabled = false;
      }
    }, 100);
  }

  // ============================================================
  // Initialization
  // ============================================================

  // Download button (VOD/series only; hidden until /api/downloads answers)
  function setupDownloadButton() {
    const btn = document.getElementById('download-btn');
    if (!btn || !cfg.isVod || !window.NetvDL) return;
    const isEpisode = cfg.streamType === 'series';
    const id = isEpisode ? cfg.episodeId : cfg.streamId;
    if (!id) return;
    const media = cfg.mediaTitle || '';
    const title = cfg.seriesName && !media.includes(cfg.seriesName) ? cfg.seriesName + ' ' + media : media;
    window.NetvDL.bind(btn, isEpisode ? 'episode' : 'movie', id, {
      title: (isEpisode ? title : media) || '',
      sourceId: cfg.sourceId || '',
      seriesId: isEpisode ? cfg.seriesId : '',
      ext: ((String(cfg.rawUrl || '').split('?')[0].match(/\.([A-Za-z0-9]{2,5})$/) || [])[1] || '').toLowerCase(),
    });
  }

  // ============================================================
  // Local download playback (finished offline downloads)
  // ============================================================
  // A finished download is a local file, so playback never goes through /transcode/*.
  // The server describes the file (/api/local/{id}/plan); the player then plays the raw
  // file when the browser can, else a COMPLETE VOD HLS playlist whose segments the server
  // cuts on demand. Either way seeking is a plain video.currentTime change: no session,
  // no restart, no server round trip beyond the segment fetch. When a way fails the next
  // one is tried automatically (see NetvLocal.chooseModes).

  let localModes = [];
  let localModeIdx = -1;
  let localToken = 0;
  let localAbort = null;
  let localWatchdog = null;
  let localResume = 0;
  let localSubtitleTimers = [];

  function showLocalError(message) {
    const raw = document.getElementById('error-raw');
    if (raw) {
      raw.href = '/downloads/files/' + encodeURIComponent(cfg.localDownloadId);
      raw.classList.remove('hidden');
    }
    showSeekStatus('');
    showError(message, false);
  }

  function teardownLocalMode() {
    clearTimeout(localWatchdog);
    localWatchdog = null;
    if (localAbort) localAbort.abort();
    localAbort = null;
    if (currentHls) {
      currentHls.destroy();
      currentHls = null;
    }
    video.removeAttribute('src');
    video.load();
  }

  // The current way of playing failed: remember where we were and try the next one.
  function failLocalMode(reason) {
    console.warn('[LOCAL] ' + JSON.stringify(localModes[localModeIdx]) + ' failed: ' + reason);
    const at = video.currentTime > 5 ? video.currentTime : localResume;
    teardownLocalMode();
    localModeIdx++;
    if (localModeIdx >= localModes.length) {
      showLocalError('Ce fichier ne peut pas être lu dans le navigateur.');
      return;
    }
    startLocalMode(localModes[localModeIdx], at);
  }

  function playAfterLoad() {
    video.play().catch(() => {
      if (!video.muted) { autoMutedByPolicy = true; video.muted = true; }
      video.play().catch(() => {});
    });
  }

  function startLocalMode(mode, resume) {
    const token = ++localToken;
    localResume = resume;
    const ac = new AbortController();
    localAbort = ac;
    const fail = (why) => { if (token === localToken) failLocalMode(why); };
    const ready = () => {
      clearTimeout(localWatchdog);
      hideLoading();
      showSeekStatus('');
      error.classList.add('hidden');
    };
    showLoading();
    showSeekStatus(mode.kind === 'hls' ? 'Préparation de la lecture locale…' : '');

    const playNative = (timeoutMs) => {
      video.src = mode.url;
      video.addEventListener('loadedmetadata', () => {
        ready();
        if (resume > 0 && resume < (video.duration || Infinity)) video.currentTime = resume;
        setupNativeAudioTracks();
        playAfterLoad();
      }, { once: true, signal: ac.signal });
      video.addEventListener('error', () => {
        fail('media error ' + (video.error ? video.error.code : '?'));
      }, { signal: ac.signal });
      localWatchdog = setTimeout(() => fail('timeout'), timeoutMs);
    };

    if (mode.kind === 'direct') {
      playNative(15000);
      return;
    }
    if (!(window.Hls && Hls.isSupported())) {
      if (video.canPlayType('application/vnd.apple.mpegurl')) playNative(30000);
      else fail('no HLS support');
      return;
    }
    const hls = new Hls({
      ...createHlsConfig({ isVod: true }),
      startPosition: resume > 0 ? resume : 0,
      enableCEA708Captions: false,
      maxBufferLength: 45,
      backBufferLength: 90,
      manifestLoadingMaxRetry: 2,
      levelLoadingMaxRetry: 2,
      fragLoadingMaxRetry: 4,
      fragLoadingRetryDelay: 500,
    });
    currentHls = hls;
    wireTrackPrefs(hls);
    let recoveries = 0;
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      ready();
      playAfterLoad();
    });
    hls.on(Hls.Events.FRAG_BUFFERED, () => { recoveries = 0; });
    hls.on(Hls.Events.ERROR, (event, data) => {
      if (!data.fatal || token !== localToken) return;
      recoveries++;
      // A playlist that cannot load is not worth retrying (it is local): next way. A segment can be.
      if (recoveries <= 2 && data.type === Hls.ErrorTypes.NETWORK_ERROR && /^frag/.test(data.details)) hls.startLoad();
      else if (recoveries <= 2 && data.type === Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
      else fail('hls ' + data.details);
    });
    hls.loadSource(mode.url);
    hls.attachMedia(video);
    localWatchdog = setTimeout(() => fail('timeout'), 30000);
  }

  // Text subtitles are complete WebVTT files (not part of the HLS), loaded the first time
  // a track is shown, so the CC menu works the same for the raw file and for HLS.
  function setupLocalSubtitles(plan) {
    const entries = Local.subtitleEntries(plan);
    if (entries.length === 0) {
      disableCcButton();
      return;
    }
    activeTrackStates = entries.map((s) => ({
      url: s.url,
      track: video.addTextTrack('subtitles', s.label, s.lang),
      addedCues: new Set(),
      retryCount: 0,
      loaded: false,
    }));
    activeTrackStates.forEach((ts) => { ts.track.mode = 'hidden'; });
    enableCcButton();
    const load = async (ts) => {
      if (ts.loaded) return;
      ts.loaded = true;
      try {
        const resp = await fetch(ts.url);
        if (!resp.ok) throw new Error('subtitles ' + resp.status);
        for (const cue of parseVttCues(await resp.text())) {
          try { ts.track.addCue(new VTTCue(cue.start, cue.end, cue.text)); } catch (e) { /* bad cue */ }
        }
      } catch (e) {
        console.warn('[LOCAL] subtitles failed:', e);
        ts.loaded = false;
        if (++ts.retryCount <= 3) {
          localSubtitleTimers.push(setTimeout(() => { if (ts.track.mode === 'showing') load(ts); }, 4000));
        }
      }
    };
    video.textTracks.addEventListener('change', () => {
      activeTrackStates.forEach((ts) => { if (ts.track.mode === 'showing') load(ts); });
    });
    if (ccEnabled) {
      const idx = getPreferredSubtitleTrack(entries);
      if (idx >= 0) setCcTrack(idx);
    }
  }

  // Click, drag and keyboard scrubbing: plain currentTime writes, coalesced while dragging.
  function setupLocalScrub() {
    const bar = document.getElementById('progress-bar');
    if (!bar) return;
    let dragging = false;
    let pending = null;
    let timer = null;
    const timeAt = (e) => {
      const rect = bar.getBoundingClientRect();
      const pct = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      return pct * (totalDuration || video.duration || 0);
    };
    const apply = () => {
      timer = null;
      if (pending !== null && Number.isFinite(pending)) video.currentTime = pending;
      pending = null;
      updateProgress();
    };
    const schedule = (e) => {
      pending = timeAt(e);
      if (!timer) timer = setTimeout(apply, 120);
    };
    bar.addEventListener('pointerdown', (e) => {
      if (!(totalDuration || video.duration)) return;
      dragging = true;
      try { bar.setPointerCapture(e.pointerId); } catch (err) { /* not capturable */ }
      schedule(e);
    });
    bar.addEventListener('pointermove', (e) => { if (dragging) schedule(e); });
    const end = (e) => {
      if (!dragging) return;
      dragging = false;
      if (e.type === 'pointerup') pending = timeAt(e);
      clearTimeout(timer);
      apply();
    };
    bar.addEventListener('pointerup', end);
    bar.addEventListener('pointercancel', end);
    // Buffering after a seek shows the spinner instead of a frozen frame
    video.addEventListener('waiting', () => { if (!video.paused || video.seeking) showLoading(); });
    for (const ev of ['playing', 'canplay', 'seeked']) {
      video.addEventListener(ev, () => { if (!video.seeking) hideLoading(); });
    }
  }

  async function startLocalPlayback() {
    // There is no transcode session: hide its controls, keep everything else
    document.getElementById('menu-transcode')?.classList.add('hidden');
    document.getElementById('menu-restart')?.classList.add('hidden');
    setupLocalScrub();
    showLoading();
    showSeekStatus('Analyse du fichier…');
    let plan;
    try {
      const resp = await fetch('/api/local/' + encodeURIComponent(cfg.localDownloadId) + '/plan');
      if (!resp.ok) throw new Error('plan ' + resp.status);
      plan = await resp.json();
    } catch (e) {
      console.error('[LOCAL] plan failed:', e);
      showLocalError('Impossible d’analyser ce fichier.');
      return;
    }
    totalDuration = plan.duration || 0;
    transcodedDuration = totalDuration;
    seekOffset = 0;
    if (totalDuration > 0) {
      document.getElementById('time-duration').textContent = formatTime(totalDuration);
    }
    document.getElementById('progress-container')?.classList.remove('hidden');
    setupLocalSubtitles(plan);
    localModes = Local.chooseModes(plan, Local.detectCaps(video, window));
    localModeIdx = 0;
    if (localModes.length === 0) {
      showLocalError('Ce fichier ne peut pas être lu dans le navigateur.');
      return;
    }
    startLocalMode(localModes[0], Local.startAt(cfg.serverResumePosition, totalDuration));
  }

  function init() {
    setupDownloadButton();
    // Restore persistent settings
    const settings = loadSettings();
    if (settings.volume !== undefined) video.volume = settings.volume;
    if (settings.muted !== undefined) video.muted = settings.muted;
    // Subtitles start off on every video (turned on from the CC menu or Shift+C);
    // the saved ccEnabled is deliberately not restored.

    applyCaptionStyles();
    setupPositionTracking();
    setupLiveDvrResume();
    setupLiveProgram();
    setupKeyboardControls();
    setupButtonHandlers();
    setupActivityTracking();
    setupCast();
    updateTranscodeCheck();
    updateCcButton();
    updateMuteIcon();
    document.getElementById('volume-slider').value = video.muted ? 0 : video.volume;

    // Restore volume on first user interaction if auto-muted by browser policy
    function restoreVolumeOnInteraction() {
      if (autoMutedByPolicy) {
        video.muted = false;
        updateMuteIcon();
        autoMutedByPolicy = false;
      }
    }
    document.addEventListener('click', restoreVolumeOnInteraction, { once: true });
    document.addEventListener('keydown', restoreVolumeOnInteraction, { once: true });

    // Restore info pinned state
    if (settings.infoPinned) {
      document.getElementById('info-overlay')?.classList.add('pinned');
      document.getElementById('info-btn')?.classList.add('active');
    }


    window.addEventListener('beforeunload', cleanupTranscodeSync);
    window.addEventListener('pagehide', cleanupTranscodeSync);
    window.addEventListener('pageshow', event => {
      if (event.persisted && transcodeSession.closed) {
        if (cfg.localDownloadId) { location.reload(); return; }
        transcodeSession.reopen();
        if (isTranscoding) startTranscode();
        else playWithUrl(cfg.rawUrl);
      }
    });

    // Start playback based on transcode mode (finished downloads never transcode)
    if (cfg.localDownloadId) {
      startLocalPlayback();
    } else if (cfg.transcodeMode === 'always') {
      startTranscode();
    } else if (cfg.transcodeMode === 'never') {
      playWithUrl(cfg.rawUrl);
    } else {
      playWithUrl(cfg.rawUrl, () => {
        error.classList.add('hidden');
        startTranscode();
      });
    }
  }

  init();
})();
