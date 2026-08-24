"""Integration tests for the web console backend."""

from __future__ import annotations

import json
import threading  # noqa: E402  (kept next to fixture usage)
import types
import urllib.error
import urllib.request

import pytest

from linago.webserver import ConsoleContext, ensure_token, make_server

SETTINGS = """
[app]
provider = "prov_a"

# a hand-written comment that must survive web edits
[providers.prov_a]
type = "ollama"
base_url = "http://127.0.0.1:11434"
model = "m1"

[providers.prov_b]
type = "openai"
base_url = "https://b.test/v1"
model = "m2"
api_key_env = "PROV_B_KEY"
"""


@pytest.fixture
def console(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "settings.toml").write_text(SETTINGS)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    ctx = ConsoleContext(config_dir=cfg_dir)
    token = ensure_token(tmp_path / "cache" / "web-token")
    server = make_server(ctx, port=0, token=token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    yield types.SimpleNamespace(
        base=base, token=token, ctx=ctx, cfg_dir=cfg_dir, server=server
    )
    server.shutdown()


def _request(base, method, path, token=None, body=None):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("X-LinaGo-Token", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        payload = err.read()
        return err.code, json.loads(payload) if payload else {}


class TestAuth:
    def test_api_requires_token(self, console):
        status, body = _request(console.base, "GET", "/api/config")
        assert status == 401

    def test_wrong_token_rejected(self, console):
        status, _ = _request(console.base, "GET", "/api/config", token="nope")
        assert status == 401

    def test_static_shell_served_without_token(self, console):
        status, body = _request(console.base, "GET", "/")
        # static shell is exempt from token auth; content lands with T6
        assert status != 401
        assert body.get("error") != "missing or invalid token"


class TestConfigApi:
    def test_config_reports_presence_not_keys(self, console):
        status, body = _request(console.base, "GET", "/api/config", token=console.token)
        assert status == 200
        assert body["providers"]["prov_b"]["has_key"] is False
        raw = json.dumps(body)
        assert "PROV_B_KEY" not in raw

    def test_provider_crud_roundtrip(self, console):
        status, body = _request(
            console.base,
            "PUT",
            "/api/providers/deepseek",
            token=console.token,
            body={
                "type": "openai",
                "label": "DS",
                "base_url": "https://api.deepseek.com/v1/",
                "model": "deepseek-chat",
                "api_key": "sk-test-value",
                "temperature": 0.4,
            },
        )
        assert status == 200

        settings_text = (console.cfg_dir / "settings.toml").read_text()
        assert "# a hand-written comment" in settings_text
        assert "https://api.deepseek.com/v1" in settings_text

        secret_file = console.cfg_dir / "secrets.toml"
        assert "sk-test-value" in secret_file.read_text()

        import stat

        mode = stat.S_IMODE(secret_file.stat().st_mode)
        assert mode == 0o600

        status, body = _request(
            console.base,
            "GET",
            "/api/config",
            token=console.token,
        )
        ds = body["providers"]["deepseek"]
        assert ds["has_key"] is True
        assert ds["temperature"] == 0.4
        assert "sk-test-value" not in json.dumps(body)

        status, _ = _request(
            console.base,
            "DELETE",
            "/api/providers/deepseek",
            token=console.token,
        )
        assert status == 200
        _, after = _request(console.base, "GET", "/api/config", token=console.token)
        assert "deepseek" not in after["providers"]

    def test_invalid_provider_rejected(self, console):
        status, body = _request(
            console.base,
            "PUT",
            "/api/providers/bad",
            token=console.token,
            body={"type": "alien", "base_url": "http://x", "model": "m"},
        )
        assert status == 400


class TestSettingsAndAppearance:
    def test_settings_toggle_persists(self, console):
        status, _ = _request(
            console.base,
            "PUT",
            "/api/settings",
            token=console.token,
            body={"memory": {"enabled": True}},
        )
        assert status == 200
        text = (console.cfg_dir / "settings.toml").read_text()
        assert "[memory]" in text and "enabled = true" in text

    def test_unknown_section_rejected(self, console):
        status, _ = _request(
            console.base,
            "PUT",
            "/api/settings",
            token=console.token,
            body={"frobnicate": {"a": 1}},
        )
        assert status == 400

    def test_appearance_regenerates_css(self, console):
        status, resolved = _request(
            console.base,
            "PUT",
            "/api/appearance",
            token=console.token,
            body={"preset": "paper"},
        )
        assert status == 200
        assert resolved["resolved"]["surface"] == "250, 250, 247"
        css = (console.cfg_dir / "style.css").read_text()
        assert "rgba(250, 250, 247, 0.97)" in css

    def test_compare_list_roundtrip(self, console):
        status, body = _request(
            console.base,
            "PUT",
            "/api/compare",
            token=console.token,
            body={"providers": ["prov_b", "prov_a"]},
        )
        assert status == 200
        _, cfg = _request(console.base, "GET", "/api/config", token=console.token)
        assert cfg["compare"] == ["prov_b", "prov_a"]


class TestDiagnostics:
    def test_doctor_endpoint_shape(self, console):
        status, report = _request(
            console.base, "GET", "/api/doctor", token=console.token
        )
        assert status == 200
        names = [c["name"] for c in report]
        assert "version" in names
        assert all(set(c) == {"name", "ok", "detail", "warning"} for c in report)

    def test_test_provider_uses_probe(self, console, monkeypatch):
        monkeypatch.setattr(
            "linago.backends.probe_ollama",
            lambda provider, **kw: (True, "mocked-ok"),
        )
        # ctx captured the function at import time; patch through it too
        console.ctx.probe_ollama_fn = lambda provider, **kw: (
            True,
            "mocked-ok",
        )
        status, body = _request(
            console.base,
            "POST",
            "/api/test-provider",
            token=console.token,
            body={"name": "prov_a"},
        )
        assert status == 200
        assert body == {"ok": True, "detail": "mocked-ok"}

    def test_actions_replace(self, console):
        status, body = _request(
            console.base,
            "PUT",
            "/api/actions",
            token=console.token,
            body={"explain": "Explain {text}"},
        )
        assert status == 200
        _, cfg = _request(console.base, "GET", "/api/config", token=console.token)
        assert cfg["actions"] == {"explain": "Explain {text}"}
