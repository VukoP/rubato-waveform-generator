// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// Application shell: reads the catalog and the session state and keeps the page in step with them.
// Everything that comes from the server (file names, error text) is written with textContent only.

import { get, post } from './api.js';
import { model, commit, initState, loadSettings, subscribe } from './state.js';
import { initInspector, placeOverlays } from './inspector.js';
import { initPreview, setSource } from './preview.js';
import { initTransport, update as updateTransport, toggle as togglePlayback, playbackNotice, clock } from './transport.js';
import { revealLabel, revealSaved, renderFfmpeg, initFfmpegPanel, initAbout, setVersion } from './flows.js';

const $ = (id) => document.getElementById(id);
const el = {
  steps: [...document.querySelectorAll('.step')],
  stage: $('stage'),
  canvas: $('preview'),
  empty: $('empty'),
  source: $('source'),
  sourceName: $('source-name'),
  sourceMeta: $('source-meta'),
  sizeLabel: $('size-label'),
  badgeText: $('badge-text'),
  play: $('play-btn'),
  scrub: $('scrub'),
  timeNow: $('time-now'),
  timeTotal: $('time-total'),
  status: $('status'),
  announce: $('announce'),
  progress: $('progress'),
  progressBar: $('progress-bar'),
  formats: $('formats'),
  exportBtn: $('export-btn'),
  cancelBtn: $('cancel-btn'),
  revealBtn: $('reveal-btn'),
  chooseBtn: $('choose-btn'),
  changeBtn: $('change-btn'),
};

const FAST_MS = 250;
const SLOW_MS = 1000;
const LOST_AFTER = 3;         // failed polls in a row before the connection counts as lost
const PROBLEM_MS = 8000;      // how long a refusal from an action stays in the status line
const LOST_TEXT = 'Lost connection to the app. Please restart it.';

let catalog = null;
let lastState = null;
let pollTimer = 0;
let polling = false;
let previewProblem = '';
let actionProblem = '';       // an API refusal (export, reveal) that the state does not carry
let problemTimer = 0;
let problemVersion = null;    // the audio_version the refusal belongs to
let failedPolls = 0;
let lost = false;             // the app stopped answering: nothing can be started any more
let starting = false;         // the save dialog is open

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

function setTitled(node, text) {
  setText(node, text);
  node.title = text;
}

// A refusal stays until something new happens (another file, an export) or it has been read.
function setActionProblem(text) {
  actionProblem = text;
  problemVersion = lastState ? lastState.audio_version : null;
  clearTimeout(problemTimer);
  if (text) {
    problemTimer = setTimeout(() => {
      actionProblem = '';
      if (lastState) render(lastState);
    }, PROBLEM_MS);
  }
}

// --------------------------------------------------------------------- catalog

function renderFormats() {
  el.formats.replaceChildren();
  catalog.formats.forEach((format, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = format.label;
    button.dataset.index = String(index);
    el.formats.append(button);
  });
  syncFormats();
}

function syncFormats() {
  const { width, height, fps } = model.look;
  catalog.formats.forEach((format, index) => {
    el.formats.children[index].setAttribute('aria-pressed', String(format.w === width && format.h === height));
  });
  setText(el.sizeLabel, width + ' × ' + height + ' · ' + fps + ' FPS');
}

el.formats.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (!button || button.disabled) return;
  const format = catalog.formats[Number(button.dataset.index)];
  commit({ width: format.w, height: format.h, autoFormat: null, notice: '' });   // a manual choice is final
});

// ----------------------------------------------------------------------- state

function statusFor(state) {
  const { analysis, export: job } = state;
  const playFailed = state.playback.mode === 'failed' && state.error === state.playback.error;
  if (failedPolls > 0) return { text: lost ? LOST_TEXT : 'Cannot reach the app', error: true, progress: null };
  // A running export comes first: no stale refusal may hide its progress. `announce` is what a screen
  // reader hears; the line itself changes every few hundred milliseconds and is not a live region.
  if (job.state === 'rendering') {
    const fraction = job.total > 0 ? job.current / job.total : 0;
    const count = (n) => n.toLocaleString('en-US');
    return {
      text: 'Rendering ' + count(job.current) + ' / ' + count(job.total) + ' frames', progress: fraction,
      announce: 'Rendering started',
    };
  }
  if (job.state === 'finalizing') return { text: 'Finalizing file…', progress: 1, quiet: true, announce: 'Finalizing file' };
  if (state.error && !playFailed) return { text: state.error, error: true, progress: null };
  if (actionProblem) return { text: actionProblem, error: true, progress: null };
  if (analysis.state === 'analyzing') {
    return {
      text: 'Analyzing audio · ' + Math.round(analysis.progress * 100) + '%', progress: analysis.progress,
      announce: 'Analyzing audio',
    };
  }
  if (model.notice) return { text: model.notice, progress: null };
  if (previewProblem) return { text: previewProblem, error: true, progress: null };
  const playback = playbackNotice();
  if (playback) return { text: playback, progress: null };
  if (job.state === 'done') return { text: 'Saved ' + (job.output_name || 'the file'), progress: null };
  if (job.state === 'cancelled') return { text: 'Export cancelled', progress: null };
  if (analysis.state === 'ready') return { text: 'Ready to export', progress: null };
  if (state.ffmpeg && state.ffmpeg.ok === false) return { text: 'FFmpeg is required', progress: null };
  return { text: 'Drop an audio file to begin', progress: null };
}

function render(state) {
  lastState = state;
  const { audio, analysis, export: job } = state;
  const rendering = job.state === 'rendering' || job.state === 'finalizing';
  const ready = analysis.state === 'ready';
  if (rendering || state.audio_version !== problemVersion) setActionProblem('');   // an old refusal is not news

  el.steps.forEach((step, index) => {
    const done = index < state.step || (index === 3 && job.state === 'done');
    step.dataset.state = done ? 'done' : index === state.step ? 'current' : 'todo';
    setText(step.querySelector('.step-done'), done ? 'completed' : '');
    if (index === state.step) step.setAttribute('aria-current', 'step');
    else step.removeAttribute('aria-current');
  });

  el.empty.hidden = audio !== null;
  el.source.hidden = audio === null;
  if (audio) {
    setTitled(el.sourceName, audio.name);
    const meta = [audio.duration === null ? '--:--' : clock(audio.duration), audio.extension.toUpperCase()];
    setText(el.sourceMeta, meta.join(' · '));
  }

  const duration = audio && audio.duration !== null ? audio.duration : null;
  setText(el.timeTotal, duration === null ? '00:00' : clock(duration));
  el.timeTotal.classList.toggle('is-off', !ready);
  el.canvas.hidden = !ready;
  setSource(ready ? { version: state.audio_version, duration } : null);
  updateTransport(state);

  const status = statusFor(state);
  setTitled(el.status, status.text);
  setText(el.announce, status.announce || status.text);
  el.status.classList.toggle('is-error', Boolean(status.error));
  el.progress.classList.toggle('is-active', status.progress !== null);
  el.progress.classList.toggle('is-quiet', Boolean(status.quiet));
  if (status.progress === null || status.quiet) el.progress.removeAttribute('aria-valuenow');
  else el.progress.setAttribute('aria-valuenow', String(Math.round(status.progress * 100)));
  el.progressBar.style.width = status.progress === null ? '0%' : Math.round(status.progress * 1000) / 10 + '%';

  // Finalizing cannot be cancelled: the button is gone then, and Export waits, disabled.
  el.exportBtn.hidden = job.state === 'rendering';
  el.cancelBtn.hidden = job.state !== 'rendering';
  el.exportBtn.disabled = !ready || starting || rendering || lost;
  el.revealBtn.hidden = job.state !== 'done' || !job.can_reveal;
  for (const button of el.formats.children) button.disabled = rendering;
  el.chooseBtn.disabled = rendering || lost;
  el.changeBtn.disabled = rendering || lost;

  renderFfmpeg(state);
  placeOverlays();
}

function busy(state) {
  return state.analysis.state === 'analyzing' || state.playback.mode === 'transcoding' || state.export.state === 'rendering' || state.export.state === 'finalizing';
}

// --------------------------------------------------------------------- polling

// The app did not answer. A few misses in a row mean it is gone (or this page lost its token, which a
// reload does): Export and Choose file are switched off, and only a restart helps.
function unreachable() {
  failedPolls += 1;
  lost = failedPolls >= LOST_AFTER;
  if (lastState) {
    render(lastState);
    return;
  }
  const text = lost ? LOST_TEXT : 'Cannot reach the app';
  setTitled(el.status, text);
  setText(el.announce, text);
  el.status.classList.add('is-error');
  for (const button of [el.exportBtn, el.chooseBtn, el.changeBtn]) button.disabled = button.disabled || lost;
}

async function poll() {
  if (polling) return;
  polling = true;
  clearTimeout(pollTimer);
  let next = SLOW_MS;
  try {
    let state = null;
    try {
      state = await get('/api/state');
    } catch (error) {
      unreachable();           // a request that failed: the app, not this page's code
    }
    if (state) {
      failedPolls = 0;
      lost = false;
      try {
        next = busy(state) ? FAST_MS : SLOW_MS;
        render(state);
      } catch (error) {
        console.error('Could not draw the app state:', error);   // a bug in this page, not an outage
      }
    }
  } finally {
    polling = false;
    pollTimer = setTimeout(poll, next);
  }
}

// Called by the native drop handler with a fixed script.
window.__rubatoRefresh = poll;

// --------------------------------------------------------------------- preview

initPreview({
  stage: el.stage,
  canvas: el.canvas,
  refresh: poll,
  onProblem(text) {
    previewProblem = text;
    if (lastState) render(lastState);
  },
});

initTransport({
  stage: el.stage,
  play: el.play,
  scrub: el.scrub,
  timeNow: el.timeNow,
  badgeText: el.badgeText,
  refresh: poll,
  changed() {
    if (lastState) render(lastState);
  },
});

// -------------------------------------------------------------------- actions

async function chooseFile() {
  setActionProblem('');
  try {
    await post('/api/open');
  } catch (error) {
    setTitled(el.status, error.message);
    el.status.classList.add('is-error');
  }
  poll();
}

// The footer button and Cmd/Ctrl+E share this. The save dialog is native: the request returns once it
// is closed, with started false when it was cancelled, which changes nothing.
async function startExport() {
  if (el.exportBtn.hidden || el.exportBtn.disabled) return;
  setActionProblem('');
  starting = true;
  if (lastState) render(lastState);
  try {
    await post('/api/export', { look: { ...model.look } });
  } catch (error) {
    setActionProblem(error.message);
  }
  starting = false;
  if (lastState) render(lastState);
  poll();
}

async function cancelExport() {
  try {
    await post('/api/export/cancel');
  } catch (error) {
    setActionProblem(error.message);
  }
  poll();
}

async function revealExport() {
  try {
    if (!await revealSaved()) setActionProblem('Could not show the file.');
  } catch (error) {
    setActionProblem(error.message);
  }
  if (lastState) render(lastState);
}

el.revealBtn.textContent = revealLabel;
el.chooseBtn.addEventListener('click', chooseFile);
el.changeBtn.addEventListener('click', chooseFile);
el.exportBtn.addEventListener('click', startExport);
el.cancelBtn.addEventListener('click', cancelExport);
el.revealBtn.addEventListener('click', revealExport);
initAbout();
initFfmpegPanel((ffmpeg) => {
  if (lastState) render({ ...lastState, ffmpeg });
  poll();
});

// -------------------------------------------------------------------- keyboard

const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const isModalOpen = () => document.querySelector('dialog[open], [aria-modal="true"]') !== null;
const ownsSpace = (node) => node.closest('button, a[href], summary, select, [role="radio"], [role="button"]') !== null;
const isTextField = (node) => node.matches('input:not([type="range"]), textarea, [contenteditable]');
let pointerFocus = false;

// A control focused by a click gives the focus up, so that Space plays instead of pressing it again.
// A control focused with Tab keeps it: Space then activates it, as the platform does.
document.addEventListener('pointerdown', () => { pointerFocus = true; }, true);
document.addEventListener('keydown', () => { pointerFocus = false; }, true);
document.addEventListener('pointerup', (event) => {
  const range = event.target instanceof Element ? event.target.closest('input[type="range"]') : null;
  if (range && !range.disabled) range.focus({ preventScroll: true });     // WebKit leaves a clicked slider unfocused
  const target = event.target instanceof Element ? event.target.closest('button') : null;
  // Not in a dialog: closing About must leave the focus where the dialog can give it back. Sliders keep
  // theirs: the arrow keys work after a click, and Space plays anyway (ownsSpace).
  if (target && pointerFocus && !target.disabled && !target.closest('dialog')) target.blur();
}, true);

document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented || isModalOpen()) return;
  const target = event.target instanceof Element ? event.target : document.body;
  const command = isMac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  if (command && !event.altKey && !event.shiftKey) {
    const key = event.key.toLowerCase();
    if (key === 'o') {
      event.preventDefault();
      if (!el.chooseBtn.disabled) chooseFile();
    } else if (key === 'e' && !isTextField(target)) {
      event.preventDefault();
      startExport();
    }
    return;
  }
  if (event.code !== 'Space' || event.repeat || event.ctrlKey || event.metaKey || event.altKey) return;
  if (isTextField(target) || ownsSpace(target)) return;
  event.preventDefault();
  togglePlayback();
});

// ------------------------------------------------------------------------ drag

// Without preventDefault on dragenter and dragover the browser never fires "drop".
let dragDepth = 0;
const hasFiles = (event) => Boolean(event.dataTransfer && [...event.dataTransfer.types].includes('Files'));

for (const name of ['dragenter', 'dragover']) {
  document.addEventListener(name, (event) => {
    event.preventDefault();
    if (name === 'dragenter') dragDepth += 1;
    if (hasFiles(event)) el.stage.classList.add('drag');
  });
}
document.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) el.stage.classList.remove('drag');
});
for (const name of ['drop', 'dragend']) {
  // Capture: the native drop handler stops propagation.
  document.addEventListener(name, () => {
    dragDepth = 0;
    el.stage.classList.remove('drag');
  }, true);
}

// ------------------------------------------------------------------------ boot

async function boot() {
  let data;
  let saved;
  try {
    [data, saved] = await Promise.all([get('/api/catalog'), loadSettings()]);
  } catch (error) {
    unreachable();
    poll();
    return;
  }
  try {
    catalog = data;
    setVersion(catalog.version);
    initState(catalog, saved);
    renderFormats();
    initInspector(catalog);
    subscribe(() => {
      syncFormats();
      if (lastState) render(lastState);
    });
    syncFormats();
  } catch (error) {
    console.error('Could not start the app page:', error);
  }
  poll();
}

boot();
