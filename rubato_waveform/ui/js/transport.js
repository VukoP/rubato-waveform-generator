// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// Audio transport: a hidden audio element is the clock, the preview follows its currentTime.
// If the engine cannot decode the file, the server converts it once (at load time, never on Play,
// so a slow conversion cannot use up the user gesture that play() needs) and the element is
// pointed at the converted file.

import { post, audioUrl } from './api.js';
import { setTime } from './preview.js';

const SCRUB_STEPS = 10000;        // the range input's max (index.html)
const END_SLACK = 0.05;           // seconds: closer to the end than this, Play starts over

const audio = document.createElement('audio');
audio.preload = 'metadata';
audio.hidden = true;

let el = null;
let refresh = () => {};
let changed = () => {};
let ready = false;
let duration = 0;
let version = null;               // the audio_version the element was given
let mode = 'native';              // the server's playback mode
let t = 0;
let playing = false;
let dragging = false;
let frameRequest = 0;
let fallbackFor = null;           // the version a conversion was asked for
let broken = false;               // not even the converted file plays
let playMessage = '';

export function clock(seconds) {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
}

// options: { stage, play, scrub, timeNow, badgeText } elements; refresh() polls the state now;
// changed() asks the shell to redraw the status line.
export function initTransport(options) {
  el = options;
  refresh = options.refresh;
  changed = options.changed;
  document.body.append(audio);
  el.play.addEventListener('click', toggle);
  el.scrub.addEventListener('pointerdown', () => { dragging = true; });
  for (const name of ['pointerup', 'pointercancel']) {
    el.scrub.addEventListener(name, () => { dragging = false; });
  }
  el.scrub.addEventListener('input', onScrub);
  audio.addEventListener('ended', onEnded);
  audio.addEventListener('error', onError);
  paint();
}

// The line for the status bar while playback needs the user's attention; '' otherwise.
export function playbackNotice() {
  if (broken || mode === 'failed') return 'Playback is unavailable for this file. Preview and export still work.';
  if (awaitingConversion()) return 'Preparing audio for playback…';
  return playMessage;
}

function awaitingConversion() {
  return mode === 'transcoding' || (fallbackFor !== null && fallbackFor === version);
}

// Called with every state the shell receives.
export function update(state) {
  const file = state.audio;
  ready = file !== null && state.analysis.state === 'ready' && file.duration > 0;
  duration = ready ? file.duration : 0;
  mode = state.playback.mode;
  if (file === null) unload();
  else if (state.audio_version !== version) load(state.audio_version);
  if (!ready) stop();
  paint();
}

function load(next) {
  const converted = mode === 'fallback' && fallbackFor !== null && fallbackFor === version;
  if (!converted) {                  // a new file, not the converted copy of this one
    stop();
    setPosition(0);
    broken = false;
    fallbackFor = null;
    playMessage = '';
  }
  version = next;
  audio.src = audioUrl(next);
}

function unload() {
  stop();
  setPosition(0);
  broken = false;
  fallbackFor = null;
  playMessage = '';
  version = null;
  audio.removeAttribute('src');
  audio.load();
}

// -------------------------------------------------------------------- fallback

async function onError() {
  if (version === null || !audio.getAttribute('src')) return;
  stop();
  if (mode === 'fallback') {
    broken = true;                   // the converted file failed too
  } else if (fallbackFor !== version) {
    fallbackFor = version;
    try {
      await post('/api/audio/fallback');
    } catch (error) {
      broken = true;
      playMessage = error.message;
    }
    refresh();
  }
  paint();
  changed();
}

// ------------------------------------------------------------------ transport

export async function toggle() {
  if (el.play.disabled) return;
  if (playing) {
    pause();
    return;
  }
  if (t >= duration - END_SLACK) setPosition(0);
  if (Math.abs(audio.currentTime - t) > END_SLACK) audio.currentTime = t;
  playMessage = '';
  playing = true;
  tick();
  paint();
  try {
    await audio.play();
  } catch (error) {
    if (!playing) return;            // paused or stopped meanwhile: the rejection is expected
    stop();
    playMessage = error.name === 'NotAllowedError'
      ? 'Playback was blocked by the system. Press Play again.'
      : 'Playback could not start (' + (error.name || 'error') + ').';
    paint();
    changed();
  }
}

function pause() {
  audio.pause();
  playing = false;
  paint();
}

function stop() {
  if (!audio.paused) audio.pause();
  playing = false;
  cancelAnimationFrame(frameRequest);
  frameRequest = 0;
}

function onEnded() {
  playing = false;
  setPosition(duration);
  paint();
}

function tick() {
  cancelAnimationFrame(frameRequest);
  frameRequest = requestAnimationFrame(() => {
    frameRequest = 0;
    if (!playing) return;
    if (!dragging) setPosition(Math.min(audio.currentTime, duration));
    tick();
  });
}

function onScrub() {
  if (!ready) return;
  setPosition(Number(el.scrub.value) / SCRUB_STEPS * duration);
  if (playing) audio.currentTime = t;
}

function setPosition(seconds) {
  t = seconds;
  paint();
  setTime(t);
}

// ------------------------------------------------------------------------ view

function paint() {
  if (!el) return;
  const fraction = duration > 0 ? Math.min(t / duration, 1) : 0;
  const text = ready ? clock(t) : '00:00';
  if (el.timeNow.textContent !== text) el.timeNow.textContent = text;
  el.timeNow.classList.toggle('is-off', !ready);
  if (!dragging) el.scrub.value = String(Math.round(fraction * SCRUB_STEPS));
  const spoken = ready ? clock(t) + ' of ' + clock(duration) : '';
  if ((el.scrub.getAttribute('aria-valuetext') || '') !== spoken) {
    if (spoken) el.scrub.setAttribute('aria-valuetext', spoken);
    else el.scrub.removeAttribute('aria-valuetext');
  }
  el.scrub.style.setProperty('--fill', fraction * 100 + '%');
  el.scrub.disabled = !ready;
  el.play.disabled = !ready || broken || mode === 'failed' || awaitingConversion();
  el.play.classList.toggle('playing', playing);
  el.play.setAttribute('aria-label', playing ? 'Pause' : 'Play');
  el.stage.classList.toggle('playing', playing);
  el.badgeText.textContent = playing ? 'Playing' : 'Transparent';
}
