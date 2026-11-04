// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// The inspector: every control is generated from the catalog and writes to the state; nothing here
// knows a layout, a theme or a range by name except the one layout that has a reflection.
// Text from the catalog is set with textContent or attributes only.

import { model, commit, subscribe } from './state.js';

const REFLECTION_LAYOUT = 'mirror-reflect';   // the catalog gives the baseline fraction, not the layout
const HEX = /^#?[0-9a-f]{6}$/i;

let catalog = null;
let nextId = 0;
const syncs = [];                 // each control's way of showing the current state
const parts = {};                 // the pieces the layout rules switch on and off
let overlays = { playhead: false, baseline: null };
let ruleLayout = null;
const canvas = document.getElementById('preview');
const playhead = document.getElementById('playhead');
const baseline = document.getElementById('baseline');

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === 'class') node.className = value;
    else if (name === 'text') node.textContent = value;
    else node.setAttribute(name, value);
  }
  node.append(...children);
  return node;
}

const uid = (prefix) => prefix + '-' + nextId++;

// ------------------------------------------------------------------ radio groups

// A radiogroup of real buttons: one checked, arrow keys move and select, disabled ones are skipped.
function radioGroup({ label, options, selected, choose, className, decorate }) {
  const labelId = uid('lbl');
  const caption = h('span', { class: 'field-label', id: labelId, text: label });
  const root = h('div', { class: className, role: 'radiogroup', 'aria-labelledby': labelId });
  const buttons = options.map((option) => {
    const button = h('button', { type: 'button', role: 'radio', 'data-value': String(option.value) });
    decorate(button, option);
    button.addEventListener('click', () => choose(option.value));
    root.append(button);
    return button;
  });

  root.addEventListener('keydown', (event) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    const live = buttons.filter((b) => !b.disabled);
    if (!live.length || (step === undefined && event.key !== 'Home' && event.key !== 'End')) return;
    event.preventDefault();
    const at = live.indexOf(document.activeElement);
    let to = step === undefined ? (event.key === 'Home' ? 0 : live.length - 1) : at + step;
    to = (to + live.length) % live.length;
    live[to].focus();
    choose(options[buttons.indexOf(live[to])].value);
  });

  const sync = () => {
    const checked = buttons.findIndex((_, i) => selected(options[i].value));
    buttons.forEach((button, i) => button.setAttribute('aria-checked', String(i === checked)));
    const stop = buttons[checked] && !buttons[checked].disabled ? buttons[checked] : buttons.find((b) => !b.disabled);
    buttons.forEach((button) => { button.tabIndex = button === stop ? 0 : -1; });
  };
  syncs.push(sync);
  return {
    field: h('div', { class: 'field' }, caption, root),
    root,
    setDisabled(flag) {
      for (const button of buttons) button.disabled = flag;
      sync();
    },
  };
}

const plainLabel = (button, option) => { button.textContent = option.label; };

// -------------------------------------------------------------------- sliders

const display = (def, value) => {
  const f = def.format;
  return (value / f.div * f.mul).toFixed(f.decimals) + f.suffix;
};

// One display unit, as a raw step: 0.01 for a percentage of a 0..1 range, 0.1 for tenths of a pixel.
const stepOf = (def) => def.step ?? Number(((10 ** -def.format.decimals) * def.format.div / def.format.mul).toPrecision(6));

function slider(def) {
  const id = uid('rng');
  const input = h('input', {
    type: 'range', class: 'rng', id, min: String(def.min), max: String(def.max), step: String(stepOf(def)),
  });
  const value = h('span', { class: 'val' });
  input.addEventListener('input', () => commit({ [def.field]: Number(input.value) }));
  syncs.push(() => {
    const raw = model.look[def.field];
    const text = display(def, raw);
    input.value = String(raw);
    input.style.setProperty('--fill', ((raw - def.min) / (def.max - def.min)) * 100 + '%');
    input.setAttribute('aria-valuetext', text);
    if (value.textContent !== text) value.textContent = text;
  });
  return h('div', { class: 'slider-row' }, h('label', { for: id, text: def.label }), input, value);
}

// --------------------------------------------------------------------- colors

function colorRow(letter, read, write, disabledWhen) {
  const picker = h('input', { type: 'color', class: 'picker', 'aria-label': 'Color ' + letter });
  const hex = h('input', {
    type: 'text', class: 'hex', maxlength: '7', spellcheck: 'false', autocomplete: 'off',
    'aria-label': 'Color ' + letter + ' hex',
  });
  const apply = (text) => {
    const clean = '#' + text.replace('#', '').toUpperCase();
    write(clean);
  };
  picker.addEventListener('input', () => apply(picker.value));
  hex.addEventListener('input', () => {
    if (HEX.test(hex.value)) {
      hex.removeAttribute('aria-invalid');
      apply(hex.value);
    } else {
      hex.setAttribute('aria-invalid', 'true');
    }
  });
  hex.addEventListener('change', () => {          // leaving the field puts the applied color back
    hex.removeAttribute('aria-invalid');
    hex.value = read();
  });
  syncs.push(() => {
    const color = read();
    picker.value = color.toLowerCase();
    if (hex.value.toUpperCase() !== color && !hex.hasAttribute('aria-invalid')) hex.value = color;
    picker.disabled = hex.disabled = disabledWhen();
  });
  return h('div', { class: 'color-row' }, h('span', { class: 'color-letter', 'aria-hidden': 'true', text: letter }), picker, hex);
}

// ------------------------------------------------------------------------ build

function buildSignal(body) {
  const presetLabel = h('span', { class: 'field-label', id: 'preset-label', text: 'Preset' });
  const presets = h('div', { class: 'presets', role: 'group', 'aria-labelledby': 'preset-label' });
  const buttons = catalog.presets.map((preset) => {
    const button = h('button', { type: 'button', class: 'chip', text: preset.label });
    button.addEventListener('click', () => commit({ ...preset.values }));
    presets.append(button);
    return button;
  });
  syncs.push(() => {
    catalog.presets.forEach((preset, i) => {
      const on = Object.entries(preset.values).every(([field, value]) => model.look[field] === value);
      buttons[i].setAttribute('aria-pressed', String(on));
    });
  });
  const metric = radioGroup({
    label: 'Measure', className: 'seg-ctl', decorate: plainLabel,
    options: catalog.metrics.map((m) => ({ value: m.key, label: m.label })),
    selected: (value) => model.look.metric === value,
    choose: (value) => commit({ metric: value }),
  });
  body.append(h('div', { class: 'field' }, presetLabel, presets), metric.field,
    ...catalog.sliders.filter((s) => s.section === 'signal').map(slider));
}

function buildForm(body) {
  const layout = radioGroup({
    label: 'Layout', className: 'layout-grid', decorate: plainLabel,
    options: catalog.layouts.map((l) => ({ value: l.key, label: l.label })),
    selected: (value) => model.look.layout === value,
    choose: (value) => commit({ layout: value }),
  });
  const motion = radioGroup({
    label: 'Motion', className: 'seg-ctl', decorate: plainLabel,
    options: catalog.motion.map((m) => ({ value: m.stationary, label: m.label })),
    selected: (value) => model.look.stationary === value,
    choose: (value) => commit({ stationary: value }),
  });
  const style = radioGroup({
    label: 'Style', className: 'seg-ctl', decorate: plainLabel,
    options: catalog.styles.map((s) => ({ value: s.key, label: s.label })),
    selected: (value) => model.look.style === value,
    choose: (value) => commit({ style: value }),
  });
  const sliders = catalog.sliders.filter((s) => s.section === 'form');
  const voice = h('div', { class: 'group', id: 'voice-sliders' }, ...sliders.filter((s) => s.voice_only).map(slider));
  Object.assign(parts, { motion, style, voice });
  body.append(layout.field, motion.field, style.field, voice, ...sliders.filter((s) => !s.voice_only).map(slider));
}

function buildColor(body) {
  const themes = radioGroup({
    label: 'Theme', className: 'swatches',
    options: catalog.themes.map((t) => ({ value: t.name, label: t.name, theme: t })),
    selected: (value) => model.theme === value,
    choose: (value) => {
      const theme = catalog.themes.find((t) => t.name === value);
      commit({ color: theme.color, colorB: theme.color2, theme: theme.name });
    },
    decorate(button, option) {
      button.className = 'swatch';
      button.title = option.label;
      button.setAttribute('aria-label', option.label);
      button.style.background = 'linear-gradient(135deg, ' + option.theme.color + ' 50%, ' + option.theme.color2 + ' 50%)';
    },
  });
  const name = h('p', { class: 'theme-name', id: 'theme-name' });
  syncs.push(() => { name.textContent = model.theme || 'Custom'; });
  const fill = radioGroup({
    label: 'Fill', className: 'seg-ctl', decorate: plainLabel,
    options: catalog.fill.map((f) => ({ value: f.gradient, label: f.label })),
    selected: (value) => model.gradient === value,
    choose: (value) => commit({ gradient: value }),
  });
  const custom = h('div', { class: 'field' }, h('span', { class: 'field-label', text: 'Custom colors' }),
    colorRow('A', () => model.look.color, (hex) => commit({ color: hex, theme: null }), () => false),
    colorRow('B', () => model.colorB, (hex) => commit({ colorB: hex, theme: null }), () => !model.gradient));
  themes.field.append(name);
  body.append(themes.field, fill.field, custom);
}

// ------------------------------------------------------------------------ rules

const formatLabel = (f) => f.w + ' × ' + f.h;

// Every layout rule, from the catalog's tables, in one place.
function applyRules() {
  const rules = catalog.rules;
  const layout = model.look.layout;
  const among = (list) => list.includes(layout);

  overlays = {
    playhead: among(rules.playhead_layouts),
    baseline: among(rules.no_baseline_layouts) ? null : layout === REFLECTION_LAYOUT ? rules.reflection_baseline : 0.5,
  };
  parts.motion.setDisabled(!among(rules.stationary_optional));
  parts.style.setDisabled(among(rules.style_disabled_layouts));
  const voice = among(rules.voice_only_layouts);
  parts.motion.field.hidden = parts.style.field.hidden = voice;
  parts.voice.hidden = !voice;

  if (layout !== ruleLayout) {       // the format follows the layout only when the layout changes
    ruleLayout = layout;
    const square = rules.square_format;
    const isSquare = model.look.width === square.w && model.look.height === square.h;
    if (among(rules.square_layouts)) {
      if (!isSquare) {
        const name = catalog.layouts.find((l) => l.key === layout).label;
        commit({
          width: square.w, height: square.h, autoFormat: { w: model.look.width, h: model.look.height },
          notice: name + ' needs a square frame. Switched to ' + formatLabel(square) + '.',
        });
      }
    } else if (model.autoFormat && isSquare) {
      const previous = model.autoFormat;
      commit({ width: previous.w, height: previous.h, autoFormat: null, notice: 'Restored ' + formatLabel(previous) + '.' });
    } else if (model.autoFormat || model.notice) {
      commit({ autoFormat: null, notice: '' });
    }
  }
  placeOverlays();
}

// The guides sit on the preview canvas, not on the stage around it.
export function placeOverlays() {
  const shown = !canvas.hidden && canvas.dataset.frame !== undefined;
  const { offsetLeft: x, offsetTop: y, offsetWidth: w, offsetHeight: hgt } = canvas;
  playhead.hidden = !(shown && overlays.playhead);
  playhead.style.left = x + w / 2 + 'px';
  playhead.style.top = y + hgt * 0.05 + 'px';
  playhead.style.height = hgt * 0.9 + 'px';
  baseline.hidden = !(shown && overlays.baseline !== null);
  baseline.style.left = x + 'px';
  baseline.style.width = w + 'px';
  baseline.style.top = y + hgt * (overlays.baseline ?? 0.5) + 'px';
  playhead.dataset.on = String(overlays.playhead);
  baseline.dataset.at = overlays.baseline === null ? 'none' : String(overlays.baseline);
}

export function initInspector(data) {
  catalog = data;
  buildSignal(document.getElementById('signal-body'));
  buildForm(document.getElementById('form-body'));
  buildColor(document.getElementById('color-body'));
  subscribe(() => {
    applyRules();
    for (const sync of syncs) sync();
  });
  applyRules();
  for (const sync of syncs) sync();
}
