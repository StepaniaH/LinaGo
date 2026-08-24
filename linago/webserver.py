"""Loopback HTTP console for configuring LinaGo.

The server binds ``127.0.0.1`` exclusively and guards ``/api/*`` with
a per-install token stored next to the cache (mode 0600). The static
console lives in ``linago/webui`` and is served without a token so a
browser can load the shell first; every data request carries
``X-LinaGo-Token``.

Writes go through :mod:`linago.configstore` (comments preserved) and
:mod:`linago.theme` (stylesheet regeneration). Provider keys are
write-only: responses report presence, never values.
"""

from __future__ import annotations

import json
import logging
import re
import secrets as pysecrets
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import linago
import linago.configstore as configstore
from linago.backends import probe_ollama, probe_openai
from linago.config import (
    SUPPORTED_TYPES,
    load_config,
    load_ocr_settings,
    load_tts_provider,
    warn_secret_permissions,
)
from linago.doctor import run_checks
from linago.paths import cache_dir
from linago.theme import APPEARANCE_KEYS, save_appearance

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def ensure_token(token_path: Path | None = None) -> str:
    """Per-install console token, created on first use."""
    path = token_path or cache_dir() / "web-token"
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    token = pysecrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    _restrict(path)
    return token


def _restrict(path: Path) -> None:
    import os

    if os.name == "posix":
        os.chmod(path, 0o600)


@dataclass
class ConsoleContext:
    """Everything a request handler needs; swappable for tests."""

    config_dir: Path
    which: Callable[[str], str | None] = field(default=shutil.which)
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    probe_ollama_fn: Callable = probe_ollama
    probe_openai_fn: Callable = probe_openai

    # ── shared loaders ────────────────────────────────────────────────
    def settings_file(self) -> Path:
        return self.config_dir / "settings.toml"

    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.toml"

    def current(self) -> dict:
        from linago.config import load_settings as _load

        return _load(self.settings_file())

    def active_config(self):

        return load_config(self.current())


def _provider_view(provider, has_key: bool) -> dict:
    return {
        "name": provider.name,
        "type": provider.type,
        "label": provider.label,
        "base_url": provider.base_url,
        "model": provider.model,
        "timeout": provider.timeout_s,
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "has_key": has_key,
    }


# ── field validation ──────────────────────────────────────────────
def _reject_unknown(body: dict, allowed: set[str]) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise ApiError(400, f"unknown fields: {sorted(unknown)}")


def _clean_provider_entry(body: dict) -> dict:
    _reject_unknown(
        body,
        {
            "type",
            "label",
            "base_url",
            "model",
            "timeout",
            "temperature",
            "max_tokens",
            "api_key",
        },
    )
    ptype = str(body.get("type", "")).strip()
    if ptype not in SUPPORTED_TYPES:
        raise ApiError(400, f"type must be one of {SUPPORTED_TYPES}")
    base_url = str(body.get("base_url", "")).strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ApiError(400, "base_url must be an http(s) URL")
    model = str(body.get("model", "")).strip()
    if not model:
        raise ApiError(400, "model is required")
    entry: dict = {
        "type": ptype,
        "label": str(body.get("label") or "").strip() or model,
        "base_url": base_url,
        "model": model,
    }
    for key in ("timeout", "max_tokens"):
        if body.get(key) is not None:
            try:
                value = int(body[key])
            except (TypeError, ValueError):
                raise ApiError(400, f"{key} must be an integer") from None
            if value <= 0:
                raise ApiError(400, f"{key} must be positive")
            entry[key] = value
    if body.get("temperature") is not None:
        try:
            temp = float(body["temperature"])
        except (TypeError, ValueError):
            raise ApiError(400, "temperature must be a number") from None
        if not 0.0 <= temp <= 2.0:
            raise ApiError(400, "temperature out of range")
        entry["temperature"] = temp
    return entry


# ── endpoint implementations ──────────────────────────────────────
def get_config(ctx: ConsoleContext, body=None) -> dict:
    settings = ctx.current()
    cfg = ctx.active_config()
    secrets = configstore.load_document(ctx.secrets_file())
    keys = (secrets.get("keys") or {}) if isinstance(secrets, dict) else {}

    providers = {}
    for name, provider in cfg.providers.items():
        providers[name] = _provider_view(provider, bool(keys.get(name)))

    table = settings.get("appearance") or {}
    return {
        "version": linago.__version__,
        "active": cfg.active,
        "providers": providers,
        "ocr": {
            "engine": load_ocr_settings(settings).engine,
            "tesseract_langs": load_ocr_settings(settings).tesseract_langs,
        },
        "tts_provider": load_tts_provider(settings),
        "memory_enabled": bool((settings.get("memory") or {}).get("enabled")),
        "history_enabled": bool((settings.get("history") or {}).get("enabled", True)),
        "compare": [
            str(n) for n in ((settings.get("compare") or {}).get("providers") or [])
        ],
        "appearance": {
            "preset": str(table.get("preset", "dark")),
            "accent": str(table.get("accent", "")),
            "bg_alpha": table.get("bg_alpha"),
            "font_scale": table.get("font_scale", 1.0),
        },
        "actions": dict(settings.get("actions") or {}),
        "lang": str((settings.get("app") or {}).get("lang", "")),
    }


def put_provider(ctx: ConsoleContext, name: str, body: dict) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ApiError(400, "invalid provider name")
    entry = _clean_provider_entry(body)
    doc = configstore.load_document(ctx.settings_file())
    if "providers" not in doc:
        doc["providers"] = {}
    table = doc["providers"]
    existing = table.get(name) if isinstance(table.get(name), dict) else None
    merged = dict(existing) if existing else {}
    merged.update(entry)
    table[name] = merged
    configstore.save_document(ctx.settings_file(), doc)
    api_key = str(body.get("api_key") or "")
    if api_key.strip():
        configstore.set_secret(ctx.secrets_file(), name, api_key.strip())
    return {"ok": True, "name": name}


def delete_provider(ctx: ConsoleContext, name: str) -> dict:
    doc = configstore.load_document(ctx.settings_file())
    removed = False
    if isinstance((doc.get("providers") or {}).get(name), dict):
        del doc["providers"][name]
        configstore.save_document(ctx.settings_file(), doc)
        removed = True
    if remove_key := configstore.remove_secret(ctx.secrets_file(), name):
        removed = removed or remove_key
    return {"ok": True, "removed": removed}


SETTINGS_SECTIONS = {
    "ocr": {"engine", "tesseract_langs", "provider"},
    "tts": {"provider"},
    "memory": {"enabled"},
    "history": {"enabled"},
    "app": {"provider", "lang", "action"},
}


def put_settings(ctx: ConsoleContext, body: dict) -> dict:
    _reject_unknown(body, set(SETTINGS_SECTIONS))
    doc = configstore.load_document(ctx.settings_file())
    for section, patch in body.items():
        _reject_unknown(patch, SETTINGS_SECTIONS[section])
        if section not in doc or not isinstance(doc.get(section), dict):
            doc[section] = {}
        for key, value in patch.items():
            doc[section][key] = value
    configstore.save_document(ctx.settings_file(), doc)

    if "compare" in body:
        raise ApiError(400, "use PUT /api/compare for compare providers")
    return {"ok": True}


def put_compare(ctx: ConsoleContext, body: dict) -> dict:
    _reject_unknown(body, {"providers"})
    names = body.get("providers")
    if not isinstance(names, list) or len(names) > 4:
        raise ApiError(400, "providers must be a list of at most four names")
    cleaned: list[str] = []
    for n in names:
        text = str(n).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    doc = configstore.load_document(ctx.settings_file())
    doc.setdefault("compare", {})["providers"] = cleaned
    configstore.save_document(ctx.settings_file(), doc)
    return {"ok": True, "providers": cleaned}


def put_actions(ctx: ConsoleContext, body: dict) -> dict:
    actions: dict = {}
    for name, template in body.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", str(name)):
            raise ApiError(400, f"invalid action name: {name!r}")
        text = str(template).strip()
        if text:
            actions[str(name)] = text
    doc = configstore.load_document(ctx.settings_file())
    doc["actions"] = actions
    configstore.save_document(ctx.settings_file(), doc)
    return {"ok": True, "actions": actions}


def get_appearance(ctx: ConsoleContext, body=None) -> dict:
    from linago.theme import resolve_params

    return resolve_params(ctx.current())


def put_appearance(ctx: ConsoleContext, body: dict) -> dict:
    _reject_unknown(body, APPEARANCE_KEYS)
    resolved = save_appearance(ctx.settings_file(), body)
    return {"ok": True, "resolved": resolved}


def post_test_provider(ctx: ConsoleContext, body: dict) -> dict:
    _reject_unknown(body, {"name"})
    cfg = ctx.active_config()
    try:
        provider = cfg.get(str(body.get("name")))
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    if provider.type == "ollama":
        ok, detail = ctx.probe_ollama_fn(provider)
    else:
        ok, detail = (
            ctx.probe_openai_fn(provider)
            if provider.api_key
            else (False, "no API key configured")
        )
    return {"ok": ok, "detail": detail}


def post_hotkey_apply(ctx: ConsoleContext, body: dict) -> dict:
    _reject_unknown(body, {"args"})
    args_list = body.get("args")
    if not isinstance(args_list, list) or not all(
        isinstance(a, str) for a in args_list
    ):
        raise ApiError(400, "args must be a list of strings")
    if args_list[0] not in ("bind", "unbind"):
        raise ApiError(400, "only bind/unbind keyword calls are allowed")
    if ctx.which("hyprctl") is None:
        raise ApiError(409, "hyprctl not available on this machine")
    argv = ["hyprctl", "keyword", *args_list]
    proc = ctx.runner(argv, capture_output=True, text=True)
    return {"ok": proc.returncode == 0, "output": (proc.stdout or "")[:400]}


HOTKEY_SUGGESTIONS = [
    {
        "keys": "SUPER, T",
        "line": "bind = SUPER, T, exec, <linago> --ocr --translate",
    },
    {
        "keys": "SUPER, S",
        "line": "bind = SUPER, S, exec, <linago> --selection --translate",
    },
]


def get_hotkeys(ctx: ConsoleContext, body=None) -> dict:
    launcher = "<path-to>/run.sh"
    suggestions = [
        {"keys": h["keys"], "line": h["line"].replace("<linago>", launcher)}
        for h in HOTKEY_SUGGESTIONS
    ]
    return {
        "suggestions": suggestions,
        "note": "Binds live in hyprland.conf; applying sets them for "
        "the current session only.",
    }


# ── routing ───────────────────────────────────────────────────────
class Router:
    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, Callable]] = []

    def add(self, method: str, pattern: str, fn: Callable) -> None:
        regex = re.compile(
            "^"
            + re.sub(r"\{(\w+)\}", lambda m: f"(?P<{m.group(1)}>[^/]+)", pattern)
            + "$"
        )
        self.routes.append((method, regex, fn))

    def match(self, method: str, path: str):
        for meth, regex, fn in self.routes:
            match = regex.match(path)
            if match and meth == method:
                return fn, match.groupdict()
        return None, None


def build_router(ctx: ConsoleContext) -> Router:
    router = Router()
    router.add("GET", "/api/config", lambda body, params: get_config(ctx))
    router.add(
        "PUT",
        "/api/providers/{name}",
        lambda b, p: put_provider(ctx, p["name"], b),
    )
    router.add(
        "DELETE",
        "/api/providers/{name}",
        lambda b, p: delete_provider(ctx, p["name"]),
    )
    router.add("PUT", "/api/settings", lambda body, params: put_settings(ctx, body))
    router.add("PUT", "/api/compare", lambda body, params: put_compare(ctx, body))
    router.add("PUT", "/api/actions", lambda body, params: put_actions(ctx, body))
    router.add("GET", "/api/appearance", lambda body, params: get_appearance(ctx))
    router.add("PUT", "/api/appearance", lambda body, params: put_appearance(ctx, body))
    router.add(
        "POST",
        "/api/test-provider",
        lambda b, p: post_test_provider(ctx, b),
    )
    router.add("GET", "/api/hotkeys", lambda body, params: get_hotkeys(ctx))
    router.add(
        "POST",
        "/api/hotkeys/apply",
        lambda b, p: post_hotkey_apply(ctx, b),
    )

    def doctor(body, params):
        checks = run_checks(ctx.active_config(), probe=True, which=ctx.which)
        return [
            {
                "name": c.name,
                "ok": c.ok,
                "detail": c.detail,
                "warning": c.warning_only,
            }
            for c in checks
        ]

    router.add("GET", "/api/doctor", doctor)
    return router


WEBUI_DIR = Path(__file__).resolve().parent / "webui"
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def make_handler(ctx: ConsoleContext, router: Router, token: str):
    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "linago-console"

        def log_message(self, fmt, *args):  # default stderr noise → debug
            logger.debug(fmt, *args)

        def _json(self, status: int, payload) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            entry = _STATIC.get(path)
            if entry is None:
                self._json(404, {"error": "not found"})
                return
            filename, ctype = entry
            try:
                data = (WEBUI_DIR / filename).read_bytes()
            except OSError:
                self._json(404, {"error": "asset missing"})
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?")[0]
            if not path.startswith("/api/"):
                if method == "GET":
                    return self._static(path)
                self._json(405, {"error": "method not allowed"})
                return
            if self.headers.get("X-LinaGo-Token") != token:
                self._json(401, {"error": "missing or invalid token"})
                return
            fn, params = router.match(method, path)
            if fn is None:
                self._json(404, {"error": "no such endpoint"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "body too large"})
                return
            try:
                body = json.loads(raw.decode()) if raw.strip() else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return
            try:
                self._json(200, fn(body, params))
            except ApiError as exc:
                self._json(exc.status, {"error": exc.message})

        def do_GET(self):
            self._dispatch("GET")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return ConsoleHandler


def make_server(
    ctx: ConsoleContext, *, port: int = 8777, token: str
) -> ThreadingHTTPServer:
    handler = make_handler(ctx, build_router(ctx), token)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    return server


def serve_forever(ctx: ConsoleContext, *, port: int = 8777) -> None:
    """Blocking loop for --web-only mode."""
    warn_secret_permissions()
    token = ensure_token()
    server = make_server(ctx, port=port, token=token)
    logger.info(
        "web console on http://127.0.0.1:%s (token: %s)",
        port,
        cache_dir() / "web-token",
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
