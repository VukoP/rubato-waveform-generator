// Copyright 2026 VukoP
// SPDX-License-Identifier: Apache-2.0
// Local API client. The per-run token comes from the URL once, is removed from the address bar
// and lives only in this module variable: never in web storage.

const params = new URLSearchParams(location.search);
const token = params.get('token') || '';
if (params.has('token')) {
  history.replaceState(null, '', location.pathname);
}

async function request(method, path, body) {
  const headers = { 'X-Rubato-Token': token };
  const init = { method, headers };
  if (method === 'POST') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  return fetch(path, init);
}

async function failure(response) {
  let message = 'HTTP ' + response.status;
  try {
    const data = await response.json();
    if (data && typeof data.error === 'string') message = data.error;
  } catch (error) {
    // not JSON: keep the status text
  }
  const error = new Error(message);
  error.status = response.status;
  return error;
}

async function json(method, path, body) {
  const response = await request(method, path, body);
  if (!response.ok) throw await failure(response);
  return response.json();
}

export const get = (path) => json('GET', path);
export const post = (path, body) => json('POST', path, body);

// A binary POST (preview frames): resolves to { buffer, width, height } from X-W and X-H.
export async function postFrame(path, body) {
  const response = await request('POST', path, body);
  if (!response.ok) throw await failure(response);
  return {
    buffer: await response.arrayBuffer(),
    width: Number(response.headers.get('X-W')),
    height: Number(response.headers.get('X-H')),
  };
}

// The audio element cannot send a header, so the audio route takes the token in the query; the
// version makes a new file or a new fallback a new URL.
export const audioUrl = (version) => '/api/audio?token=' + encodeURIComponent(token) + '&v=' + encodeURIComponent(version);
