"""Tests for the daemon socket protocol and event bus."""

from __future__ import annotations

import json
import threading

import pytest

from linago import daemon
from linago.daemon import (
    Server,
    daemon_alive,
    handle_request,
    send_request,
)


class FakeState:
    def __init__(self):
        self.shown: list[dict] = []
        self._event = threading.Event()

    def show(self, payload: dict):
        self.shown.append(payload)
        self._event.set()

    def wait_shown(self, timeout=2.0) -> bool:
        return self._event.wait(timeout)


@pytest.fixture
def sock_path(tmp_path, monkeypatch):
    # AF_UNIX paths are capped (~104 bytes on macOS), so bind a short
    # relative name inside the tmp dir instead of its absolute path.
    monkeypatch.chdir(tmp_path)
    return "linago-test.sock"


class TestHandleRequest:
    def test_ping(self):
        assert handle_request(None, {"cmd": "ping"}) == {"ok": True}

    def test_translate_forwards_text(self):
        state = FakeState()
        reply = handle_request(state, {"cmd": "translate", "text": "hi"})
        assert reply == {"ok": True}
        assert state.shown == [{"kind": "translate", "text": "hi"}]

    def test_selection_and_ocr(self):
        state = FakeState()
        handle_request(state, {"cmd": "selection"})
        handle_request(state, {"cmd": "ocr", "multi": True})
        assert state.shown[0] == {"kind": "selection"}
        assert state.shown[1] == {"kind": "ocr", "multi": True}

    def test_unknown_command_rejected(self):
        state = FakeState()
        reply = handle_request(state, {"cmd": "explode"})
        assert reply["ok"] is False
        assert state.shown == []

    def test_malformed_rejected(self):
        assert handle_request(None, "not a dict")["ok"] is False


class TestServerRoundTrip:
    def test_request_ack_and_dispatch(self, sock_path):
        state = FakeState()
        server = Server(sock_path, on_request=state.show)
        server.start()
        try:
            assert daemon_alive(sock_path) is True
            assert send_request(sock_path, {"cmd": "ping"}) == {"ok": True}
            assert send_request(sock_path, {"cmd": "translate", "text": "x"}) == {
                "ok": True
            }
            assert state.wait_shown()
            assert state.shown[-1] == {"kind": "translate", "text": "x"}
        finally:
            server.stop()
        # allow accept loop to exit before tmp cleanup
        threading.Event().wait(0.05)

    def test_double_start_refuses_live_daemon(self, sock_path):
        state = FakeState()
        first = Server(sock_path, on_request=state.show)
        second = Server(sock_path, on_request=state.show)
        first.start()
        try:
            with pytest.raises(RuntimeError, match="another daemon"):
                second.start()
        finally:
            first.stop()

    def test_stale_socket_file_is_replaced(self, sock_path):
        # Simulate a crashed daemon: a socket file nobody answers on.
        open(sock_path, "wb").close()
        assert daemon_alive(sock_path) is False

        server = Server(sock_path, on_request=FakeState().show)
        server.start()
        try:
            assert daemon_alive(sock_path) is True
        finally:
            server.stop()


class TestSubscribe:
    def test_subscriber_receives_published_events(self, sock_path):
        server = Server(sock_path, on_request=lambda p: None)
        server.start()
        try:
            # send_request expects an ack line; subscribers get none, so
            # drive the raw socket manually instead.
            import socket as socket_mod

            sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(sock_path)
            sock.sendall(b'{"cmd": "subscribe"}\n')

            def read_line():
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                return json.loads(buf.decode())

            # The ack proves the bus registration happened before any
            # later publish can be observed from this client.
            assert read_line() == {"ok": True, "subscribed": True}
            server.events.publish({"event": "translation", "translated": "hi"})
            event = read_line()
            assert event == {"event": "translation", "translated": "hi"}
            sock.close()
        finally:
            server.stop()


def test_default_socket_path_uses_runtime_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = daemon.default_socket_path()
    assert path.startswith(str(tmp_path))
    assert path.endswith(f"linago-{__import__('os').getuid()}.sock")

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert "/tmp/linago-" in daemon.default_socket_path()


def test_client_helper_never_blocks_forever(tmp_path, sock_path):
    """send_request against nothing raises OSError (stale probe)."""
    with pytest.raises(OSError):
        send_request(str(tmp_path / "missing.sock"), {"cmd": "ping"})
