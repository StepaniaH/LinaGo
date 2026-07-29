"""Translation providers — Ollama (local) and OpenAI-compatible BYOK.

Config lives in config/settings.toml; API keys in config/secrets.toml
(or the env var named by each provider's api_key_env).
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.toml"
SECRETS_PATH = CONFIG_DIR / "secrets.toml"

SUPPORTED_TYPES = ("ollama", "openai")


@dataclass(frozen=True)
class Provider:
    name: str
    type: str          # "ollama" | "openai"
    label: str
    base_url: str
    model: str
    api_key: str | None = None

    @property
    def display(self) -> str:
        return f"{self.label} · {self.model}"

    def require_ready(self) -> None:
        if self.type == "openai" and not self.api_key:
            raise RuntimeError(
                f"provider '{self.name}' 需要 API key："
                f"写入 {SECRETS_PATH.name} 的 [keys].{self.name}，"
                f"或设置对应的 api_key_env 环境变量"
            )


@dataclass(frozen=True)
class AppConfig:
    active: str
    providers: dict[str, Provider]

    def get(self, name: str | None = None) -> Provider:
        key = name or self.active
        if key not in self.providers:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise KeyError(f"未知 provider '{key}'；可用: {known}")
        return self.providers[key]

    def names(self) -> list[str]:
        return list(self.providers.keys())


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _resolve_api_key(name: str, entry: dict, secrets: dict) -> str | None:
    keys = secrets.get("keys") or {}
    if name in keys and keys[name]:
        return str(keys[name]).strip() or None
    env_name = entry.get("api_key_env")
    if env_name:
        val = os.environ.get(str(env_name), "").strip()
        if val:
            return val
    # Convention fallback: TRANSLATE_KEY_<NAME>
    convention = os.environ.get(f"TRANSLATE_KEY_{name.upper()}", "").strip()
    return convention or None


def _apply_env_overrides(providers: dict[str, Provider]) -> dict[str, Provider]:
    """Honor legacy / convenience env vars on the ollama provider."""
    if "ollama" not in providers:
        return providers
    p = providers["ollama"]
    url = os.environ.get("TRANSLATE_OLLAMA_URL")
    model = os.environ.get("TRANSLATE_OLLAMA_MODEL")
    if not url and not model:
        return providers
    base = p.base_url
    if url:
        # Accept either host or full /api/generate URL.
        base = url.removesuffix("/api/generate").rstrip("/")
    providers = dict(providers)
    providers["ollama"] = Provider(
        name=p.name,
        type=p.type,
        label=p.label,
        base_url=base,
        model=model or p.model,
        api_key=p.api_key,
    )
    return providers


def load_config(settings_path: Path | None = None) -> AppConfig:
    settings = _load_toml(settings_path or SETTINGS_PATH)
    secrets = _load_toml(SECRETS_PATH)

    raw_providers = settings.get("providers") or {}
    providers: dict[str, Provider] = {}
    for name, entry in raw_providers.items():
        if not isinstance(entry, dict):
            continue
        ptype = str(entry.get("type", "")).strip()
        if ptype not in SUPPORTED_TYPES:
            continue
        base_url = str(entry.get("base_url", "")).rstrip("/")
        model = str(entry.get("model", "")).strip()
        if not base_url or not model:
            continue
        providers[name] = Provider(
            name=name,
            type=ptype,
            label=str(entry.get("label") or name),
            base_url=base_url,
            model=model,
            api_key=_resolve_api_key(name, entry, secrets),
        )

    providers = _apply_env_overrides(providers)

    # Sensible built-in default if settings.toml is missing / empty.
    if not providers:
        providers["ollama"] = Provider(
            name="ollama",
            type="ollama",
            label="Ollama",
            base_url=os.environ.get(
                "TRANSLATE_OLLAMA_URL", "http://127.0.0.1:11434"
            )
            .removesuffix("/api/generate")
            .rstrip("/"),
            model=os.environ.get("TRANSLATE_OLLAMA_MODEL", "qwen2.5:3b"),
        )

    active = (
        os.environ.get("TRANSLATE_PROVIDER")
        or (settings.get("app") or {}).get("provider")
        or next(iter(providers))
    )
    if active not in providers:
        active = next(iter(providers))

    model_override = os.environ.get("TRANSLATE_MODEL", "").strip()
    if model_override and active in providers:
        p = providers[active]
        providers = dict(providers)
        providers[active] = Provider(
            name=p.name,
            type=p.type,
            label=p.label,
            base_url=p.base_url,
            model=model_override,
            api_key=p.api_key,
        )

    return AppConfig(active=active, providers=providers)


# ── streaming backends ───────────────────────────────────────────────────────
TokenCallback = Callable[[str], None]


def stream_completion(
    provider: Provider,
    prompt: str,
    on_token: TokenCallback,
    cancel,
    *,
    timeout: int = 120,
) -> None:
    """Blocking stream; call from a worker thread. Invokes on_token(full)."""
    provider.require_ready()
    if provider.type == "ollama":
        _stream_ollama(provider, prompt, on_token, cancel, timeout)
    elif provider.type == "openai":
        _stream_openai(provider, prompt, on_token, cancel, timeout)
    else:
        raise RuntimeError(f"unsupported provider type: {provider.type}")


def _emit_coalesced(on_token, full: str, state: dict, force: bool = False):
    import time

    interval = 0.04
    now = time.monotonic()
    if force or (now - state["last"]) >= interval:
        state["last"] = now
        on_token(full)


def _stream_ollama(provider, prompt, on_token, cancel, timeout):
    url = f"{provider.base_url}/api/generate"
    resp = requests.post(
        url,
        json={"model": provider.model, "prompt": prompt, "stream": True},
        stream=True,
        timeout=timeout,
    )
    resp.raise_for_status()
    full = ""
    state = {"last": 0.0}
    for line in resp.iter_lines(decode_unicode=True):
        if cancel.is_set():
            return
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        full += chunk.get("response", "")
        done = bool(chunk.get("done"))
        _emit_coalesced(on_token, full, state, force=done)
        if done:
            return
    if full and not cancel.is_set():
        on_token(full)


def _stream_openai(provider, prompt, on_token, cancel, timeout):
    url = f"{provider.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.2,
    }
    resp = requests.post(
        url, headers=headers, json=payload, stream=True, timeout=timeout
    )
    resp.raise_for_status()
    full = ""
    state = {"last": 0.0}
    for line in resp.iter_lines(decode_unicode=True):
        if cancel.is_set():
            return
        if not line:
            continue
        if line.startswith(":"):
            continue  # SSE comment / keepalive
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            if full and not cancel.is_set():
                on_token(full)
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content") or ""
        if not piece:
            # Some gateways put text in message.content on the final chunk.
            msg = choices[0].get("message") or {}
            piece = msg.get("content") or ""
        if piece:
            full += piece
            finish = choices[0].get("finish_reason")
            _emit_coalesced(on_token, full, state, force=bool(finish))
    if full and not cancel.is_set():
        on_token(full)
