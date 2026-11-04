# Copyright 2026 VukoP
# SPDX-License-Identifier: Apache-2.0
"""Security of the local HTTP server: Host/Origin/token gates, body limits, static files, error hygiene."""

import http.client
import socket
import time

import pytest
from conftest import wait_until
from server_support import REQUIRED_HEADERS, get, make_server, post, request, select_audio


@pytest.fixture
def env(tmp_path, monkeypatch):
    srv, session, dialogs = make_server(tmp_path, monkeypatch)
    yield srv, session, dialogs
    srv.stop()
    session.shutdown()


@pytest.fixture
def srv(env):
    return env[0]


def assert_alive(srv) -> None:
    assert get(srv, "/api/state").status == 200


def assert_headers(resp) -> None:
    for name, value in REQUIRED_HEADERS.items():
        assert resp.headers.get(name) == value, name


# --------------------------------------------------------------------- basics

def test_binds_loopback_only_with_random_port(srv):
    assert srv._httpd.server_address[0] == "127.0.0.1"
    assert srv.url == f"http://127.0.0.1:{srv.port}/?token={srv.token}"
    assert len(srv.token) >= 43


def test_normal_requests_work(srv):
    assert get(srv, "/api/state").status == 200
    assert get(srv, "/api/catalog").status == 200
    assert get(srv, "/api/settings").status == 200
    assert get(srv, "/").status == 200


# ----------------------------------------------------------------------- Host

@pytest.mark.parametrize("path", ["/", "/css/a.css", "/missing", "/api/state", "/api/nope"])
@pytest.mark.parametrize("host", ["wrong", "localhost:{port}", "127.0.0.1", "127.0.0.1:1", "evil.test:{port}", ""])
def test_wrong_host_is_403(srv, path, host):
    resp = get(srv, path, host=host.format(port=srv.port))
    assert resp.status == 403
    assert_headers(resp)
    assert_alive(srv)


@pytest.mark.parametrize("path", ["/", "/api/state"])
def test_missing_host_is_403(srv, path):
    assert get(srv, path, host=None).status == 403
    assert_alive(srv)


def test_wrong_host_on_post_and_options(srv):
    assert post(srv, "/api/open", host=f"localhost:{srv.port}").status == 403
    assert request(srv, "OPTIONS", "/api/state", host="evil.test").status == 403


# --------------------------------------------------------------------- Origin

@pytest.mark.parametrize("origin", ["http://evil.test", "null", "http://localhost:{port}", "https://127.0.0.1:{port}", ""])
def test_foreign_origin_is_403(srv, origin):
    resp = get(srv, "/api/state", headers={"Origin": origin.format(port=srv.port)})
    assert resp.status == 403
    assert post(srv, "/api/settings", headers={"Origin": origin.format(port=srv.port)}).status == 403
    assert get(srv, "/", headers={"Origin": origin.format(port=srv.port)}).status == 403
    assert_alive(srv)


def test_correct_origin_passes(srv):
    assert get(srv, "/api/state", headers={"Origin": srv.origin}).status == 200
    assert post(srv, "/api/settings", headers={"Origin": srv.origin}).status in (200, 400)


# ---------------------------------------------------------------------- token

@pytest.mark.parametrize("token", [None, "", "wrong", "caf\u00e9"])
def test_bad_token_is_401(srv, token):
    resp = get(srv, "/api/state", token=token)   # wider non-ASCII is covered with raw bytes below
    assert resp.status == 401
    assert_headers(resp)
    assert_alive(srv)


def test_non_ascii_token_bytes_do_not_crash(srv):
    for raw in (b"\xff\xfe\xc3\xa9", "\u0441\u043b\u043e\u0432\u043e".encode()):
        with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
            sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: " + srv.host.encode() + b"\r\nX-Rubato-Token: " + raw
                         + b"\r\nConnection: close\r\n\r\n")
            data = b""
            while chunk := sock.recv(4096):
                data += chunk
        assert data.startswith(b"HTTP/1.1 401")
    assert get(srv, "/api/audio?token=%FF%C3%A9&v=0").status == 401
    assert get(srv, "/api/audio?token=%D1%81", token=None).status == 401
    assert_alive(srv)


def test_unauthenticated_cannot_tell_routes_apart(srv):
    existing = get(srv, "/api/state", token=None)
    missing = get(srv, "/api/does-not-exist", token=None)
    dev = get(srv, "/api/dev/select", token=None)
    post_only = get(srv, "/api/open", token=None)
    for resp in (existing, missing, dev, post_only):
        assert resp.status == 401
        assert resp.body == existing.body
    assert post(srv, "/api/frame", token=None).status == 401
    assert get(srv, "/api/audio", token=None).status == 401


def test_token_in_query_is_not_enough_for_json_routes(srv):
    assert get(srv, f"/api/state?token={srv.token}", token=None).status == 401


# ----------------------------------------------------------------------- POST

def test_post_content_type(srv):
    assert post(srv, "/api/settings", content_type="text/plain").status == 415
    assert post(srv, "/api/settings", content_type="application/x-www-form-urlencoded").status == 415
    assert post(srv, "/api/settings", content_type="multipart/form-data; boundary=x").status == 415
    assert post(srv, "/api/settings", content_type=None).status == 415
    assert post(srv, "/api/settings", content_type="application/json-x").status == 415
    assert post(srv, "/api/settings", content_type="application/json; charset=latin-1").status == 415
    assert post(srv, "/api/settings", content_type="application/json; foo=bar").status == 415
    assert post(srv, "/api/settings", content_type="application/json; charset=utf-8").status == 200
    assert post(srv, "/api/settings", content_type="Application/JSON").status == 200
    assert_alive(srv)


def test_post_body_limits(srv):
    big = b'{"a":"' + b"x" * (64 * 1024) + b'"}'
    assert request(srv, "POST", "/api/settings", body=big).status == 413
    assert request(srv, "POST", "/api/settings", body=b"{}", headers={"Content-Length": "65537"}).status == 413
    assert request(srv, "POST", "/api/settings").status == 411
    for junk in ("abc", "-1", "1.5", "0x10", "1 2", "", "\xb2"):
        resp = request(srv, "POST", "/api/settings", body=b"{}", headers={"Content-Length": junk})
        assert resp.status == 400, junk
    assert_alive(srv)


def test_chunked_is_rejected(srv):
    resp = request(srv, "POST", "/api/settings", headers={"Transfer-Encoding": "chunked"},
                   body=b"2\r\n{}\r\n0\r\n\r\n")
    assert resp.status == 400
    resp = request(srv, "POST", "/api/settings", headers={"Transfer-Encoding": "chunked"})
    assert resp.status in (400, 411)
    assert_alive(srv)


def test_invalid_json_is_400(srv):
    for body in (b"{", b"", b"not json", b"[1]", b'"x"', b"\xff\xfe", b"[" * 30000):
        resp = request(srv, "POST", "/api/settings", body=body)
        assert resp.status == 400, body[:20]
        assert "Traceback" not in resp.body.decode()
    assert_alive(srv)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_json_is_400(srv, literal):
    body = ('{"look": {"fps": ' + literal + "}}").encode()
    assert request(srv, "POST", "/api/settings", body=body).status == 400
    body = ('{"look": {}, "t": ' + literal + ', "max_w": 100, "max_h": 100}').encode()
    assert request(srv, "POST", "/api/frame", body=body).status == 400


def test_options_is_405_without_cors(srv):
    for path in ("/", "/api/state", "/api/open", "/nope"):
        resp = request(srv, "OPTIONS", path, token=None,
                       headers={"Access-Control-Request-Method": "POST", "Origin": srv.origin})
        assert resp.status == 405
        assert_headers(resp)
    assert_alive(srv)


def test_other_methods_are_refused(srv):
    for method in ("PUT", "DELETE", "PATCH"):
        assert request(srv, method, "/api/state").status == 405
    assert request(srv, "POST", "/").status == 405
    assert request(srv, "POST", "/api/state", json_body={}).status == 405
    assert get(srv, "/api/open").status == 405
    assert request(srv, "HEAD", "/api/state").status == 405
    assert_alive(srv)


# --------------------------------------------------------------------- static

TRAVERSAL = ["/../secret.txt", "/..%2fsecret.txt", "/%2e%2e/secret.txt", "//etc/passwd", "/index.html/../../secret.txt",
             "/..\\secret.txt", "/C:\\Windows\\win.ini", "/C:/Windows/win.ini", "\\\\host\\share\\x", "/css/../../secret.txt",
             "/%2e%2e%2fsecret.txt", "/../../../../etc/passwd", "/index.html%00.css", "/secret.txt", "/css/", "/css",
             "http://127.0.0.1/secret.txt", "*", "/notes.exe", "/api"]


@pytest.mark.parametrize("target", TRAVERSAL)
def test_traversal_is_404(srv, target):
    resp = get(srv, target, token=None)
    assert resp.status in (404, 401), target
    assert b"TOP SECRET" not in resp.body
    assert b"root:" not in resp.body
    assert_alive(srv)


def test_static_files_and_queries(srv):
    resp = get(srv, "/", token=None)
    assert resp.status == 200 and b"<title>" in resp.body
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert get(srv, f"/?token={srv.token}", token=None).status == 200
    assert get(srv, "/index.html?x=1", token=None).body == resp.body
    css = get(srv, "/css/a.css?v=2", token=None)
    assert css.status == 200 and css.headers["content-type"].startswith("text/css")
    assert get(srv, "/css/a.css", token=None, ).body == b"body{}"
    head = request(srv, "HEAD", "/css/a.css", token=None)
    assert head.status == 200 and head.body == b"" and head.headers["content-length"] == "6"


def test_missing_ui_dir_still_starts(tmp_path, monkeypatch):
    srv, session, _ = make_server(tmp_path, monkeypatch, ui=False)
    try:
        assert get(srv, "/", token=None).status == 404
        assert get(srv, "/api/state").status == 200
    finally:
        srv.stop()
        session.shutdown()


# -------------------------------------------------------------------- headers

def test_headers_on_success_and_errors(srv):
    csp = None
    for resp in (get(srv, "/"), get(srv, "/css/a.css"), get(srv, "/api/state"), get(srv, "/missing"),
                 get(srv, "/", host="evil"), get(srv, "/api/state", token="x"), post(srv, "/api/settings", content_type="text/plain"),
                 request(srv, "OPTIONS", "/"), post(srv, "/api/frame", {"look": {}, "t": 0, "max_w": 100, "max_h": 100})):
        assert_headers(resp)
        if resp.headers["content-type"].startswith("text/html"):
            csp = resp.headers.get("content-security-policy")
    assert csp is not None
    for part in ("default-src 'none'", "script-src 'self'", "frame-ancestors 'none'", "base-uri 'none'",
                 "form-action 'none'", "connect-src 'self'", "media-src 'self'"):
        assert part in csp
    for resp in (get(srv, "/"), get(srv, "/missing")):
        assert resp.headers.get("server") == "Rubato"           # no Python or library version
        assert not [v for v in resp.headers.values() if "python" in v.lower() or "basehttp" in v.lower()]


# ------------------------------------------------------------------------ dev

def test_dev_select_hidden_in_normal_mode(env, tmp_path):
    srv, session, _ = env
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"x" * 100)
    missing = get(srv, "/api/dev/other")
    resp = post(srv, "/api/dev/select", {"path": str(audio)})
    assert resp.status == 404 and resp.body == missing.body
    assert session.state()["audio"] is None
    for route in ("/api/dev/next-output", "/api/dev/reveals"):
        resp = post(srv, route, {"path": "x.mov"})
        assert resp.status == 404 and resp.body == missing.body


def test_dev_select_works_in_dev_mode(tmp_path, monkeypatch):
    srv, session, _ = make_server(tmp_path, monkeypatch, dev=True)
    try:
        audio = tmp_path / "talk.wav"
        audio.write_bytes(b"x" * 100)
        assert post(srv, "/api/dev/select", {"path": str(audio)}).status == 200
        assert wait_until(lambda: session.state()["analysis"]["state"] == "ready")
        assert post(srv, "/api/dev/select", {"path": str(tmp_path / "nope.wav")}).status == 400
        assert post(srv, "/api/dev/select", {"path": 5}).status == 400
        assert post(srv, "/api/dev/select", {}).status == 400
    finally:
        srv.stop()
        session.shutdown()


def test_dev_dialog_routes_queue_one_output_and_record_reveals(tmp_path, monkeypatch):
    from rubato_waveform.app import _NoDialogs
    dialogs = _NoDialogs()
    srv, session, _ = make_server(tmp_path, monkeypatch, dev=True, dev_dialogs=dialogs)
    try:
        assert post(srv, "/api/dev/next-output", {"path": "a.mov"}).status == 200
        assert dialogs.pick_output(None, "x.mov") == "a.mov"
        assert dialogs.pick_output(None, "x.mov") is None            # used once
        assert post(srv, "/api/dev/next-output", {"path": 5}).status == 400
        assert post(srv, "/api/dev/next-output", {}).status == 400
        dialogs.reveal("/some/where/a.mov")
        assert post(srv, "/api/dev/reveals").json() == {"paths": ["/some/where/a.mov"]}
    finally:
        srv.stop()
        session.shutdown()


# --------------------------------------------------------------------- errors

def test_internal_error_is_generic(env, monkeypatch, caplog):
    srv, session, _ = env

    def boom():
        raise RuntimeError("secret detail /srv/hidden-dir/file.wav")

    monkeypatch.setattr(session, "state", boom)
    resp = get(srv, "/api/state")
    assert resp.status == 500
    text = resp.body.decode()
    assert resp.json() == {"error": "Internal error."}
    assert "Traceback" not in text and "hidden-dir" not in text and "secret" not in text
    assert_headers(resp)
    monkeypatch.undo()
    assert get(srv, "/api/catalog").status == 200


def test_session_errors_map_to_4xx(env):
    srv, _session, _ = env
    assert post(srv, "/api/frame", {"look": {}, "t": 0, "max_w": 100, "max_h": 100}).status == 409
    assert post(srv, "/api/export", {"look": {}}).status == 409
    assert post(srv, "/api/export", {"look": {"nope": 1}}).status == 400
    assert post(srv, "/api/audio/fallback").status == 400
    assert "error" in post(srv, "/api/export", {}).json()


def test_state_never_contains_the_path(env, tmp_path):
    srv, session, _ = env
    audio = tmp_path / "private-folder" / "talk.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"x" * 100)
    select_audio(session, audio)
    text = get(srv, "/api/state").body.decode()
    assert "talk.wav" in text
    assert str(tmp_path) not in text and "private-folder" not in text
    assert str(tmp_path) not in get(srv, "/api/settings").body.decode()


def test_simple_routes(env, tmp_path):
    srv, _session, dialogs = env
    assert post(srv, "/api/open").json() == {"opened": False}
    assert post(srv, "/api/export/cancel").json() == {"ok": False}      # nothing is running
    assert post(srv, "/api/reveal").json() == {"revealed": False}
    assert "ok" in post(srv, "/api/ffmpeg/recheck").json()
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"x" * 10)
    dialogs.audio = str(audio)
    assert post(srv, "/api/open").json() == {"opened": True}
    assert wait_until(lambda: get(srv, "/api/state").json()["analysis"]["state"] == "ready")
    settings = get(srv, "/api/settings").json()
    assert post(srv, "/api/settings", {"theme": settings["theme"]}).json() == settings


# -------------------------------------------------------------- hostile sockets

def test_garbage_does_not_kill_the_server(srv):
    payloads = [b"\x00\x01\x02garbage\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"\xff" * 5000 + b"\r\n\r\n",
                b"GET /" + b"a" * 100000 + b" HTTP/1.1\r\n\r\n", b"POST /api/state HTTP/1.1\r\n" + b"X: y\r\n" * 500 + b"\r\n",
                b"GET / HTTP/1.1\r\nHost: " + srv.host.encode() + b"\r\nHost: " + srv.host.encode() + b"\r\n\r\n"]
    for payload in payloads:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
            sock.sendall(payload)
            sock.settimeout(5)
            try:
                reply = sock.recv(65536)
            except (TimeoutError, ConnectionError):
                reply = b""
        assert b"Traceback" not in reply
        assert_alive(srv)


def read_to_eof(sock) -> bytes:
    sock.settimeout(10)
    data = b""
    while chunk := sock.recv(65536):
        data += chunk
    return data


def test_http09_request_is_refused_cleanly(srv):
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
        sock.sendall(b"GET /api/state\r\n")
        reply = read_to_eof(sock)
    assert b"Internal error" not in reply and b"Bad request" in reply
    assert_alive(srv)


def test_duplicate_host_header_is_forbidden(srv):
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
        sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: " + srv.host.encode() + b"\r\nHost: " + srv.host.encode()
                     + b"\r\nX-Rubato-Token: " + srv.token.encode() + b"\r\n\r\n")
        reply = read_to_eof(sock)
    assert reply.startswith(b"HTTP/1.1 403 ")
    assert_alive(srv)


def test_rejected_request_body_is_not_parsed_as_a_second_request(srv):
    smuggled = (b"GET /api/state HTTP/1.1\r\nHost: " + srv.host.encode()
                + b"\r\nX-Rubato-Token: " + srv.token.encode() + b"\r\n\r\n")
    head = (b"POST /api/settings HTTP/1.1\r\nHost: " + srv.host.encode() + b"\r\nX-Rubato-Token: " + srv.token.encode()
            + b"\r\nContent-Type: text/plain\r\nContent-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n")
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
        sock.sendall(head + smuggled)
        reply = read_to_eof(sock)            # returns only because the server closed the connection
    assert reply.count(b"HTTP/1.1 ") == 1
    assert reply.startswith(b"HTTP/1.1 415 ")
    assert_alive(srv)


def test_idle_connection_does_not_block_others(srv):
    idle = [socket.create_connection(("127.0.0.1", srv.port), timeout=10) for _ in range(5)]
    half = socket.create_connection(("127.0.0.1", srv.port), timeout=10)
    half.sendall(b"GET /api/state HTTP/1.1\r\nHost: ")
    try:
        start = time.monotonic()
        assert_alive(srv)
        assert time.monotonic() - start < 10
    finally:
        for sock in (*idle, half):
            sock.close()


def test_connection_timeout_ends_idle_connections(tmp_path, monkeypatch):
    from rubato_waveform import server as server_mod
    monkeypatch.setattr(server_mod._Handler, "timeout", 0.3)
    srv, session, _ = make_server(tmp_path, monkeypatch)
    try:
        with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
            sock.settimeout(10)
            assert sock.recv(10) == b""      # the server closed the idle connection
        assert_alive(srv)
    finally:
        srv.stop()
        session.shutdown()


def test_stop_releases_the_port(tmp_path, monkeypatch):
    srv, session, _ = make_server(tmp_path, monkeypatch)
    port = srv.port
    srv.stop()
    session.shutdown()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


# ------------------------------------------------------------------ big numbers

def test_huge_numbers_are_a_400_not_a_500(env, tmp_path):
    srv, session, _ = env
    audio = tmp_path / "talk.wav"
    audio.write_bytes(b"x" * 100)
    select_audio(session, audio)
    huge = 10**400
    frame = {"look": {}, "t": 0, "max_w": 100, "max_h": 100}
    assert post(srv, "/api/frame", frame).status == 200
    for field, value in (("t", huge), ("t", -huge), ("max_w", huge), ("max_h", -huge)):
        assert post(srv, "/api/frame", {**frame, field: value}).status == 400, field
    for name in ("width", "height", "fps", "bars", "opacity", "window_seconds", "gain", "contrast", "smoothing",
                 "max_height_ratio", "reactivity", "drift", "line_width"):
        for value in (huge, -huge):
            assert post(srv, "/api/frame", {**frame, "look": {name: value}}).status == 400, name
            assert post(srv, "/api/export", {"look": {name: value}}).status == 400, name
    assert post(srv, "/api/settings", {"look": {"gain": huge}}).status == 200    # lenient: falls back to the default
    assert_alive(srv)


# ------------------------------------------------------------- port and connections

def test_windows_bind_is_exclusive_and_set_before_binding(monkeypatch):
    from rubato_waveform import server as server_mod
    events = []

    class FakeSocket:
        def setsockopt(self, level, name, value):
            events.append(("setsockopt", level, name, value))

        def bind(self, address):
            events.append(("bind", address))

        def getsockname(self):
            return ("127.0.0.1", 4321)

    def bind_on(platform):
        events.clear()
        fake = server_mod._Server.__new__(server_mod._Server)
        fake.socket, fake.server_address = FakeSocket(), ("127.0.0.1", 0)
        monkeypatch.setattr(server_mod.sys, "platform", platform)
        monkeypatch.setattr(server_mod.socket, "getfqdn", lambda host: host)
        server_mod._Server.server_bind(fake)

    monkeypatch.setattr(server_mod.socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    bind_on("win32")
    assert events == [("setsockopt", server_mod.socket.SOL_SOCKET, -5, 1), ("bind", ("127.0.0.1", 0))]
    bind_on("linux")
    assert events == [("bind", ("127.0.0.1", 0))]


def served(srv) -> bool:
    try:
        return get(srv, "/api/state").status == 200
    except (http.client.RemoteDisconnected, ConnectionError):      # still over the cap for a moment
        return False


def test_connection_cap_closes_the_excess_and_never_leaks(tmp_path, monkeypatch):
    from rubato_waveform import server as server_mod
    monkeypatch.setattr(server_mod, "MAX_CONNECTIONS", 3)
    srv, session, _ = make_server(tmp_path, monkeypatch)
    try:
        held = [socket.create_connection(("127.0.0.1", srv.port), timeout=10) for _ in range(3)]
        for sock in held:
            sock.sendall(b"GET /api/state HTTP/1.1\r\nHost: ")        # a half request keeps its slot
        time.sleep(0.3)
        for _ in range(5):
            with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as extra:
                extra.settimeout(10)
                try:
                    assert extra.recv(100) == b""             # closed at once, nothing served
                except ConnectionResetError:
                    pass
        for sock in held:
            sock.close()
        assert wait_until(lambda: served(srv))
        for index in range(300):                              # churn: good, garbage and abruptly closed
            with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as sock:
                if index % 3 == 1:
                    sock.sendall(b"\x00garbage\r\n\r\n")
                elif index % 3 == 2:
                    sock.sendall(b"GET /api/st")
        assert wait_until(lambda: served(srv))
        slots = srv._httpd._slots
        assert wait_until(lambda: slots._value == 3)          # every slot came back
    finally:
        srv.stop()
        session.shutdown()
