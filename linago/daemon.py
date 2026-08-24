"""Resident-mode IPC: a Unix socket protocol shared by daemon and clients.

Clients send one newline-delimited JSON command and receive one JSON
acknowledgement. Subscribers keep the connection open and receive an
event line after every completed translation, which makes the daemon
pipeable into OBS overlays, logs, or TTS pipelines:

    echo '{"cmd":"subscribe"}' | nc -U "$XDG_RUNTIME_DIR/linago-$UID.sock"

The module deliberately knows nothing about GTK: the UI side injects a
``show`` callback, everything here runs against plain sockets.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

REQUEST_TIMEOUT_S = 10


def default_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = runtime_dir if runtime_dir else "/tmp"
    return os.path.join(base, f"linago-{os.getuid()}.sock")


def daemon_alive(socket_path: str) -> bool:
    """True when another LinaGo instance answers on this socket."""
    try:
        sock = _connect(socket_path)
    except OSError:
        return False
    try:
        sock.sendall(b'{"cmd":"ping"}\n')
        sock.recv(4096)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _connect(socket_path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(REQUEST_TIMEOUT_S)
    try:
        sock.connect(socket_path)
    except OSError:
        sock.close()
        raise
    return sock


def send_request(socket_path: str, payload: dict) -> dict:
    """Send one command and return the acknowledgement."""
    sock = _connect(socket_path)
    try:
        sock.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode()) if buf.strip() else {"ok": False}
    finally:
        sock.close()


def handle_request(
    state,
    msg: dict,
) -> dict:
    """Map a client command to a UI payload via ``state.show``.

    ``state.show(payload)`` must marshal onto the UI thread itself.
    Unknown or malformed commands yield an ``{"ok": false}`` reply and
    never reach the UI.
    """
    if not isinstance(msg, dict):
        return {"ok": False, "error": "malformed request"}

    cmd = msg.get("cmd")

    if cmd == "ping":
        return {"ok": True}

    if cmd == "translate":
        state.show({"kind": "translate", "text": msg.get("text")})
        return {"ok": True}

    if cmd == "selection":
        state.show({"kind": "selection"})
        return {"ok": True}

    if cmd == "ocr":
        state.show({"kind": "ocr", "multi": bool(msg.get("multi"))})
        return {"ok": True}

    return {"ok": False, "error": f"unknown command: {cmd!r}"}


@dataclass
class EventBus:
    """Fan-out of completed-translation events to subscriber sockets."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _subscribers: list[socket.socket] = field(default_factory=list, init=False)

    def subscribe(self, sock: socket.socket) -> None:
        with self._lock:
            self._subscribers.append(sock)

    def unsubscribe(self, sock: socket.socket) -> None:
        with self._lock:
            if sock in self._subscribers:
                self._subscribers.remove(sock)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def publish(self, event: dict) -> None:
        data = (json.dumps(event) + "\n").encode()
        with self._lock:
            subscribers = list(self._subscribers)
        dead: list[socket.socket] = []
        for sock in subscribers:
            try:
                sock.sendall(data)
            except OSError:
                dead.append(sock)
        for sock in dead:
            self.unsubscribe(sock)


class Server:
    """Accept loop dispatching commands to the UI thread."""

    def __init__(
        self,
        socket_path: str,
        on_request: Callable[[dict], None],
    ):
        self.socket_path = socket_path
        self.on_request = on_request
        self.events = EventBus()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        path = self.socket_path
        if os.path.exists(path):
            # A leftover socket only survives a crashed daemon; drop it
            # when nothing answers, refuse to hijack a live instance.
            if daemon_alive(path):
                raise RuntimeError(f"another daemon is listening on {path}")
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen(8)
        sock.settimeout(0.5)
        self._sock = sock
        thread = threading.Thread(target=self._serve, name="linago-daemon", daemon=True)
        thread.start()
        self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        with self.events._lock:
            subscribers = list(self.events._subscribers)
        for sock in subscribers:
            self.events.unsubscribe(sock)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(REQUEST_TIMEOUT_S)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            try:
                msg = json.loads(buf.decode())
            except json.JSONDecodeError:
                conn.sendall(b'{"ok": false, "error": "invalid json"}\n')
                return
            if isinstance(msg, dict) and msg.get("cmd") == "subscribe":
                self.events.subscribe(conn)
                # Acknowledge only after registering so subscribers that
                # see this line are guaranteed a place on the bus.
                try:
                    conn.sendall(b'{"ok": true, "subscribed": true}\n')
                except OSError:
                    self.events.unsubscribe(conn)
                    return
                # Ownership transfers to the event bus; block until the
                # bus closes us on shutdown or the peer disconnects.
                try:
                    while not self._stop.is_set():
                        if conn.recv(4096) == b"":
                            break
                        threading.Event().wait(0.2)
                except OSError:
                    pass
                self.events.unsubscribe(conn)
                return
            reply = handle_request(_RequestDispatcher(self.on_request), msg)
            conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


class _RequestDispatcher:
    """Adapts a bare callback to the shape handle_request expects."""

    def __init__(self, show: Callable[[dict], None]):
        self.show = show
