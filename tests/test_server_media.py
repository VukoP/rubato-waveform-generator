# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Preview frames and audio streaming (Range) over the local HTTP server."""

import http.client
import json
import os
import socket

import pytest
from conftest import wait_until
from server_support import get, make_server, post, request, select_audio

from rubato_waveform import server

SIZE = 300_000
DATA = bytes(i * 7 % 251 for i in range(SIZE))


@pytest.fixture
def env(tmp_path, monkeypatch):
    srv, session, dialogs = make_server(tmp_path, monkeypatch)
    yield srv, session, dialogs
    srv.stop()
    session.shutdown()


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "talk.wav"
    path.write_bytes(DATA)
    return path


@pytest.fixture
def loaded(env, audio):
    srv, session, _ = env
    select_audio(session, audio)
    return srv, session


def audio_url(srv, session, **overrides) -> str:
    version = session.state()["audio_version"]
    query = {"token": srv.token, "v": version, **overrides}
    return "/api/audio?" + "&".join(f"{k}={v}" for k, v in query.items() if v is not None)


# --------------------------------------------------------------------- frames

FRAME = {"look": {}, "t": 1.5, "max_w": 480, "max_h": 270}


def test_frame_is_raw_straight_rgba(loaded):
    srv, _ = loaded
    resp = post(srv, "/api/frame", FRAME)
    assert resp.status == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    w, h = int(resp.headers["x-w"]), int(resp.headers["x-h"])
    assert w <= 480 and h <= 270 and w * h > 0
    assert len(resp.body) == w * h * 4
    assert int(resp.headers["content-length"]) == w * h * 4
    pixels = [resp.body[i:i + 4] for i in range(0, len(resp.body), 4)]
    assert any(p[3] > 0 for p in pixels)                     # something is drawn
    assert any(p[3] == 0 for p in pixels)                    # and the background is transparent
    visible = [p for p in pixels if p[3] > 0]
    assert max(max(p[:3]) for p in visible) > 128            # straight alpha: colour is not darkened by alpha


def test_frame_before_analysis_is_409(env):
    srv, _session, _ = env
    assert post(srv, "/api/frame", FRAME).status == 409


@pytest.mark.parametrize("change", [
    {"t": "1"}, {"t": True}, {"t": None}, {"t": [1]}, {"max_w": 480.0}, {"max_w": True}, {"max_h": "270"},
    {"max_w": 15}, {"max_w": 4097}, {"max_h": -5}, {"max_h": 0}, {"look": "x"}, {"look": []}, {"look": {"nope": 1}},
    {"extra": 1}, {"max_w": 10 ** 30},
])
def test_frame_bad_fields_are_400(loaded, change):
    srv, _ = loaded
    assert post(srv, "/api/frame", {**FRAME, **change}).status == 400
    assert get(srv, "/api/state").status == 200


@pytest.mark.parametrize("missing", ["look", "t", "max_w", "max_h"])
def test_frame_missing_field_is_400(loaded, missing):
    srv, _ = loaded
    assert post(srv, "/api/frame", {k: v for k, v in FRAME.items() if k != missing}).status == 400


def test_frame_time_is_clamped(loaded):
    srv, _ = loaded
    assert post(srv, "/api/frame", {**FRAME, "t": -3}).status == 200
    assert post(srv, "/api/frame", {**FRAME, "t": 10 ** 9}).status == 200


# ---------------------------------------------------------------------- audio

def fetch(srv, session, rng=None, method="GET", **overrides):
    headers = {"Range": rng} if rng is not None else None
    return request(srv, method, audio_url(srv, session, **overrides), headers=headers, token=None)


def test_full_file(loaded):
    srv, session = loaded
    resp = fetch(srv, session)
    assert resp.status == 200
    assert resp.body == DATA
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["content-length"] == str(SIZE)
    assert resp.headers["cache-control"] == "no-store" and resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(("rng", "start", "end"), [
    ("bytes=0-1", 0, 1),
    ("bytes=0-0", 0, 0),
    ("bytes=5-", 5, SIZE - 1),
    ("bytes=-5", SIZE - 5, SIZE - 1),
    ("bytes=100-199", 100, 199),
    (f"bytes={SIZE - 1}-", SIZE - 1, SIZE - 1),
    (f"bytes=10-{SIZE + 500}", 10, SIZE - 1),
    ("bytes=0-99999999999999999999", 0, SIZE - 1),
    (f"bytes=-{SIZE * 10}", 0, SIZE - 1),
    ("bytes=-99999999999999999999", 0, SIZE - 1),
    ("bytes=70000-140000", 70000, 140000),
])
def test_ranges(loaded, rng, start, end):
    srv, session = loaded
    resp = fetch(srv, session, rng)
    assert resp.status == 206
    assert resp.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert resp.headers["content-length"] == str(end - start + 1)
    assert resp.body == DATA[start:end + 1]
    assert resp.headers["accept-ranges"] == "bytes"


def test_webkit_probe_returns_exactly_two_bytes(loaded):
    srv, session = loaded
    resp = fetch(srv, session, "bytes=0-1")
    assert (resp.status, len(resp.body)) == (206, 2)
    assert resp.headers["content-range"] == f"bytes 0-1/{SIZE}"


@pytest.mark.parametrize("rng", [f"bytes={SIZE}-", f"bytes={SIZE + 10}-{SIZE + 20}", "bytes=-0",
                                 "bytes=99999999999999999999-"])
def test_unsatisfiable_ranges(loaded, rng):
    srv, session = loaded
    resp = fetch(srv, session, rng)
    assert resp.status == 416
    assert resp.headers["content-range"] == f"bytes */{SIZE}"
    assert fetch(srv, session).status == 200


@pytest.mark.parametrize("rng", ["bytes=0-1,5-9", "bytes=0-1, 5-", "bytes=", "bytes=-", "bytes=abc-def", "items=0-1",
                                 "bytes=5-2", "0-1", "bytes=1-2-3", "bytes=-5-", "", "bytes=0x1-5", "bytes=+1-5",
                                 "bytes=" + "9" * 5000 + "-"])
def test_malformed_and_multi_ranges_return_everything(loaded, rng):
    srv, session = loaded
    resp = fetch(srv, session, rng)
    assert resp.status == 200
    assert resp.body == DATA
    assert "content-range" not in resp.headers


def test_head(loaded):
    srv, session = loaded
    resp = fetch(srv, session, method="HEAD")
    assert resp.status == 200 and resp.body == b""
    assert resp.headers["content-length"] == str(SIZE)
    assert resp.headers["accept-ranges"] == "bytes"
    ranged = fetch(srv, session, "bytes=0-9", method="HEAD")
    assert ranged.status == 206 and ranged.body == b""
    assert ranged.headers["content-length"] == "10"
    assert ranged.headers["content-range"] == f"bytes 0-9/{SIZE}"


def test_head_keeps_the_connection_usable(loaded):
    srv, session = loaded
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        for method in ("HEAD", "GET", "HEAD", "GET"):
            conn.request(method, audio_url(srv, session), headers={"Range": "bytes=0-9"})
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 206 and len(body) == (0 if method == "HEAD" else 10)
    finally:
        conn.close()


def test_nothing_served_before_selection(env):
    srv, _session, _ = env
    assert request(srv, "GET", f"/api/audio?token={srv.token}&v=0", token=None).status == 404
    assert request(srv, "GET", f"/api/audio?token={srv.token}", token=None).status == 404


def test_version_must_match(loaded, tmp_path):
    srv, session = loaded
    assert fetch(srv, session, v=None).status == 404
    assert fetch(srv, session, v="abc").status == 404
    assert fetch(srv, session, v=session.state()["audio_version"] + 1).status == 404
    old = audio_url(srv, session)
    assert request(srv, "GET", old, token=None).status == 200
    other = tmp_path / "other.wav"
    other.write_bytes(b"other bytes")
    select_audio(session, other)
    assert request(srv, "GET", old, token=None).status == 404
    resp = fetch(srv, session)
    assert resp.status == 200 and resp.body == b"other bytes"


def test_token_is_required_for_audio(loaded):
    srv, session = loaded
    version = session.state()["audio_version"]
    assert request(srv, "GET", f"/api/audio?v={version}", token=None).status == 401
    assert request(srv, "GET", f"/api/audio?token=wrong&v={version}", token=None).status == 401
    assert request(srv, "GET", f"/api/audio?token=&v={version}", token=None).status == 401
    assert request(srv, "GET", f"/api/audio?v={version}").status == 401        # header token is not accepted here
    assert request(srv, "GET", f"/api/audio?token={srv.token}x&v={version}", token=None).status == 401
    assert request(srv, "POST", f"/api/audio?token={srv.token}&v={version}", json_body={}).status in (401, 404, 405)


def test_request_cannot_choose_the_file(loaded, tmp_path):
    srv, session = loaded
    (tmp_path / "secret.bin").write_bytes(b"SECRET")
    for extra in ("&path=../secret.bin", "&file=/etc/passwd", f"&path={tmp_path}/secret.bin"):
        resp = request(srv, "GET", audio_url(srv, session) + extra, token=None)
        assert resp.status == 200 and resp.body == DATA


def test_file_vanished_is_404(loaded, audio):
    srv, session = loaded
    os.remove(audio)
    assert fetch(srv, session).status == 404
    assert get(srv, "/api/state").status == 200


def test_client_disconnect_mid_stream(env, tmp_path, caplog):
    srv, session, _ = env
    big = tmp_path / "big.wav"
    big.write_bytes(os.urandom(40 * 1024 * 1024))
    select_audio(session, big)
    version = session.state()["audio_version"]
    for _ in range(3):
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.sendall(f"GET /api/audio?token={srv.token}&v={version} HTTP/1.1\r\nHost: {srv.host}\r\n\r\n".encode())
        assert sock.recv(1024).startswith(b"HTTP/1.1 200")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        sock.close()                                         # reset while the server is still writing
    assert wait_until(lambda: get(srv, "/api/state").status == 200)
    resp = fetch(srv, session, "bytes=0-9")
    assert resp.status == 206 and len(resp.body) == 10
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]


def abort_frame_request(srv, count):
    """Asks for preview frames and resets the connection without reading the answer."""
    body = json.dumps(FRAME).encode()
    head = (f"POST /api/frame HTTP/1.1\r\nHost: {srv.host}\r\nX-Rubato-Token: {srv.token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    for _ in range(count):
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        try:
            sock.sendall(head + body)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass                                                  # the server already closed it
        sock.close()


def test_clients_that_abort_leave_stderr_empty(loaded, tmp_path, capsys):
    srv, session = loaded
    big = tmp_path / "big.wav"
    big.write_bytes(os.urandom(20 * 1024 * 1024))
    select_audio(session, big)
    version = session.state()["audio_version"]
    for _ in range(10):
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
        sock.sendall(f"GET /api/audio?token={srv.token}&v={version} HTTP/1.1\r\nHost: {srv.host}\r\n\r\n".encode())
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        sock.close()
    abort_frame_request(srv, 50)
    slots = srv._httpd._slots
    assert wait_until(lambda: slots._value == server.MAX_CONNECTIONS)       # every handler thread has finished
    assert capsys.readouterr().err == ""


def test_shrinking_file_closes_the_connection(env, tmp_path):
    srv, session, _ = env
    path = tmp_path / "shrink.wav"
    path.write_bytes(os.urandom(5 * 1024 * 1024))
    select_audio(session, path)
    version = session.state()["audio_version"]
    sock = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.sendall(f"GET /api/audio?token={srv.token}&v={version} HTTP/1.1\r\nHost: {srv.host}\r\n\r\n".encode())
    head = sock.recv(512)
    assert head.startswith(b"HTTP/1.1 200")
    os.truncate(path, 1000)
    received = len(head)
    sock.settimeout(10)
    while chunk := sock.recv(65536):
        received += len(chunk)
    sock.close()
    assert received < 5 * 1024 * 1024                        # ended short, and the socket was closed (no hang)
    assert get(srv, "/api/state").status == 200
