// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// The flows around the preview: showing the saved file, the FFmpeg-missing panel and the About dialog.
// Text that came from the server or the file system is written with textContent only.

import { post } from './api.js';

const $ = (id) => document.getElementById(id);

const COPIED_MS = 2000;
const WHY = 'The waveform is read with FFmpeg and written as a ProRes 4444 movie, so it has to be installed.';

const platform = navigator.platform || navigator.userAgent || '';
export const revealLabel = /Mac|iPhone|iPad/.test(platform) ? 'Show in Finder'
  : /Win/.test(platform) ? 'Show in Explorer' : 'Show in folder';

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// --------------------------------------------------------------- saved file

// Resolves to true when the file manager was asked to show it.
export async function revealSaved() {
  const { revealed } = await post('/api/reveal');
  return revealed;
}

// --------------------------------------------------------------- FFmpeg panel

const prompt = $('drop-prompt');
const panel = $('ffmpeg-panel');
const command = $('ffmpeg-cmd');
const copyButton = $('ffmpeg-copy');
const recheckButton = $('ffmpeg-recheck');
const note = $('ffmpeg-note');
let copiedTimer = 0;

// Shows the panel in place of the drop prompt while there is no audio and FFmpeg is not usable.
export function renderFfmpeg(state) {
  const ffmpeg = state.ffmpeg || {};
  const missing = state.audio === null && ffmpeg.ok === false;
  panel.hidden = !missing;
  prompt.hidden = missing;
  if (!missing) return;
  setText($('ffmpeg-text'), ffmpeg.problem || WHY);
  setText(command, ffmpeg.install_command || '');
  setText($('ffmpeg-alt'), ffmpeg.install_alternative || '');
  command.hidden = !ffmpeg.install_command;
  copyButton.hidden = !ffmpeg.install_command;
}

function selectCommand() {
  const range = document.createRange();
  range.selectNodeContents(command);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

async function copyCommand() {
  let label = 'Copied';
  try {
    await navigator.clipboard.writeText(command.textContent);
  } catch (error) {
    selectCommand();                 // the clipboard is denied: leave the text selected for Cmd/Ctrl+C
    let copied = false;
    try {
      copied = document.execCommand('copy');
    } catch (failure) {
      copied = false;
    }
    if (!copied) label = 'Selected';
  }
  setText(copyButton, label);
  clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => setText(copyButton, 'Copy'), COPIED_MS);
}

// checked(status) gets the answer of the recheck, which has the shape of state.ffmpeg.
export function initFfmpegPanel(checked) {
  copyButton.addEventListener('click', copyCommand);
  recheckButton.addEventListener('click', async () => {
    recheckButton.disabled = true;
    try {
      const status = await post('/api/ffmpeg/recheck');
      setText(note, status.ok ? '' : 'FFmpeg was not found yet.');
      checked(status);
    } catch (error) {
      setText(note, error.message);
    } finally {
      recheckButton.disabled = false;
    }
  });
}

// ---------------------------------------------------------------------- About

export function initAbout() {
  const dialog = $('about');
  $('about-btn').addEventListener('click', () => {
    if (!dialog.open) dialog.showModal();
  });
  $('about-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => $('about-btn').focus());      // however it was closed
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();      // the backdrop; the content fills the dialog box
  });
}

export function setVersion(version) {
  setText($('about-version'), version ? 'Version ' + version : '');
}
