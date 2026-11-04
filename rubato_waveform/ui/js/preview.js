// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// The live preview: one frame request in flight at a time, always for the newest time and look.
// States that arrive while a frame is on its way are dropped; the next request asks for the newest.
// The server sends straight (not premultiplied) RGBA, which is what ImageData takes as it is.

import { postFrame } from './api.js';
import { model, subscribe } from './state.js';
import { placeOverlays } from './inspector.js';

const MARGIN = 16;            // CSS px between the drawn preview and the edge of the stage
const MAX_SIDE = 4096;
const RETRY_MS = 1000;        // after a failed request, no new one sooner than this

let stage = null;
let canvas = null;
let report = () => {};
let source = null;            // { version, duration } while there is an analysis; null otherwise
let time = 0;
let inFlight = false;
let paintedKey = '';
let epoch = 0;                // changes with the source: an answer for an older one is not painted
let blockedUntil = 0;
let retryTimer = 0;
let problem = false;
let awaitingState = false;    // the server said "no analysis": no frames until the state says what is loaded now
let refresh = () => {};

export function initPreview(options) {
  ({ stage, canvas } = options);
  report = options.onProblem;
  refresh = options.refresh;
  // Rules such as a layout's square frame commit again from inside a listener: ask once they are done.
  subscribe(() => queueMicrotask(requestFrame));
  new ResizeObserver(() => {
    fit();
    requestFrame();
  }).observe(stage);
}

export function setSource(next) {
  const changed = (next === null) !== (source === null) || (next && source && next.version !== source.version);
  source = next;
  if (changed) {
    epoch += 1;
    awaitingState = false;
    if (next === null) clear();
  }
  requestFrame();
}

export function setTime(seconds) {
  time = seconds;
  requestFrame();
}

function available() {
  return {
    width: Math.max(0, stage.clientWidth - 2 * MARGIN),
    height: Math.max(0, stage.clientHeight - 2 * MARGIN),
  };
}

// Scales the canvas to the stage, aspect preserved, up as well as down; then moves the guides.
function fit() {
  const room = available();
  if (room.width > 0 && room.height > 0 && canvas.width > 0 && canvas.height > 0) {
    const scale = Math.min(room.width / canvas.width, room.height / canvas.height);
    canvas.style.width = Math.max(1, Math.round(canvas.width * scale)) + 'px';
    canvas.style.height = Math.max(1, Math.round(canvas.height * scale)) + 'px';
  }
  placeOverlays();
}

function clear() {
  paintedKey = '';
  delete canvas.dataset.frame;
  delete canvas.dataset.t;
  canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
  placeOverlays();
}

function wanted() {
  if (!source) return null;
  const dpr = window.devicePixelRatio || 1;
  const room = available();
  const maxW = Math.min(MAX_SIDE, Math.floor(room.width * dpr));
  const maxH = Math.min(MAX_SIDE, Math.floor(room.height * dpr));
  if (maxW < 16 || maxH < 16) return null;
  const t = Math.min(Math.max(time, 0), source.duration);
  const look = { ...model.look };
  const key = [source.version, JSON.stringify(look), t, maxW, maxH].join('/');
  return { key, body: { look, t, max_w: maxW, max_h: maxH } };
}

function requestFrame() {
  if (inFlight || awaitingState) return;         // the answer asks again if anything changed meanwhile
  const next = wanted();
  if (!next || next.key === paintedKey) return;
  const wait = blockedUntil - performance.now();
  if (wait > 0) {
    if (!retryTimer) {
      retryTimer = setTimeout(() => {
        retryTimer = 0;
        requestFrame();
      }, wait);
    }
    return;
  }
  send(next);
}

async function send(next) {
  inFlight = true;
  const sentEpoch = epoch;
  try {
    const frame = await postFrame('/api/frame', next.body);
    if (sentEpoch === epoch) paint(frame, next);
    if (problem) {
      problem = false;
      report('');
    }
  } catch (error) {
    if (error.status === 409) {                 // the file changed under the page: ask what is loaded now
      awaitingState = true;
      refresh();
    } else {
      blockedUntil = performance.now() + RETRY_MS;
      if (!problem) {
        problem = true;
        report('Preview unavailable: ' + error.message);
      }
    }
  } finally {
    inFlight = false;
  }
  requestFrame();
}

function paint({ buffer, width, height }, next) {
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  canvas.getContext('2d').putImageData(new ImageData(new Uint8ClampedArray(buffer), width, height), 0, 0);
  paintedKey = next.key;
  canvas.dataset.frame = width + 'x' + height;
  canvas.dataset.t = String(next.body.t);
  fit();
}
