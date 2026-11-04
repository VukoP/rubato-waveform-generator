// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// The look, in the wire format of the API, plus the little that goes with it (theme, fill, notices).
// Every change goes through commit(): it tells the listeners (the preview among
// them) and saves the settings (debounced).

import { get, post } from './api.js';

const SAVE_MS = 400;

export const model = {
  look: {},          // the full look as the server takes it: color, color2 hex strings, color2 null for Solid
  theme: null,       // the selected theme name; null means custom colors
  lastTheme: '',     // what to save as the theme while the colors are custom
  gradient: true,
  colorB: '',        // color B survives a switch to Solid, where look.color2 is null
  autoFormat: null,  // { w, h }: the format a square layout replaced; null when the user chose it
  notice: '',        // a short message for the status line
};

const listeners = new Set();
let saveTimer = 0;

export function subscribe(listener) {
  listeners.add(listener);
}

function sameColor(a, b) {
  return typeof a === 'string' && typeof b === 'string' && a.toUpperCase() === b.toUpperCase();
}

// Starts from the catalog defaults, then the saved settings (already validated by the server).
export function initState(catalog, saved) {
  const defaults = catalog.defaults;
  const settings = saved || {};
  const look = { ...defaults.look, ...(settings.look || {}) };
  const gradient = typeof settings.gradient === 'boolean' ? settings.gradient : defaults.gradient;
  const named = catalog.themes.find((t) => t.name === (settings.theme || defaults.theme));
  const matches = named && sameColor(named.color, look.color) && (!gradient || sameColor(named.color2, look.color2));
  const square = catalog.rules.square_format;
  const auto = settings.auto_format;          // saved with the look: a relaunch must still know what Orbit replaced
  model.autoFormat = auto && look.width === square.w && look.height === square.h ? { w: auto.w, h: auto.h } : null;
  model.look = look;
  model.theme = matches ? named.name : null;
  model.lastTheme = named ? named.name : defaults.theme;
  model.gradient = gradient;
  model.colorB = look.color2 || (named ? named.color2 : defaults.look.color2);
  model.look.color2 = gradient ? model.colorB : null;
}

// patch: any look field, plus theme, gradient, colorB, autoFormat, notice.
export function commit(patch) {
  const { theme, gradient, colorB, autoFormat, notice, ...fields } = patch;
  Object.assign(model.look, fields);
  if (theme !== undefined) {
    model.theme = theme;
    if (theme !== null) model.lastTheme = theme;
  }
  if (gradient !== undefined) model.gradient = gradient;
  if (colorB !== undefined) model.colorB = colorB;
  if (autoFormat !== undefined) model.autoFormat = autoFormat;
  if (notice !== undefined) model.notice = notice;
  model.look.color2 = model.gradient ? model.colorB : null;
  for (const listener of listeners) listener();
  scheduleSave();
}

export async function loadSettings() {
  try {
    return await get('/api/settings');
  } catch (error) {
    return null;       // the defaults are fine; the app still works
  }
}

function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const body = { look: { ...model.look }, theme: model.theme || model.lastTheme, gradient: model.gradient, auto_format: model.autoFormat };
    try {
      await post('/api/settings', body);
    } catch (error) {
      // a failed save must not disturb the session; the next change tries again
    }
  }, SAVE_MS);
}
