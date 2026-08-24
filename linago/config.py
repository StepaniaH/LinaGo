"""Provider configuration, API key resolution, and app settings.

Providers are declared in ``settings.toml``; keys live in
``secrets.toml`` beside it or in the env var named by each provider's
``api_key_env``.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from linago.paths import find_config_dir

SUPPORTED_TYPES = ("ollama", "openai")


@dataclass(frozen=True)
class Provider:
    name: str
    type: str  # "ollama" | "openai"
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
                f"写入 secrets.toml 的 [keys].{self.name}，"
                "或设置对应的 api_key_env 环境变量"
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


TESSERACT_DEFAULT_LANGS = "chi_sim+eng"


@dataclass(frozen=True)
class OcrSettings:
    engine: str = "tesseract"  # "tesseract" | "vision"
    tesseract_langs: str = TESSERACT_DEFAULT_LANGS
    provider: str | None = None  # used when engine == "vision"


def _read_toml(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def load_settings(settings_path: Path | None = None) -> dict:
    """Parsed settings.toml; empty dict when no configuration exists."""
    if settings_path is not None:
        return _read_toml(settings_path)
    config_dir = find_config_dir()
    if config_dir is None:
        return {}
    return _read_toml(config_dir / "settings.toml")


def secrets_path() -> Path | None:
    """secrets.toml beside the resolved settings.toml, if any."""
    config_dir = find_config_dir()
    if config_dir is None:
        return None
    return config_dir / "secrets.toml"


def warn_secret_permissions() -> None:
    """Warn when secrets.toml is readable by group/others (POSIX)."""
    path = secrets_path()
    if path is None or not path.exists() or os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        print(
            f"警告: {path} 权限过宽，建议执行 chmod 600 {path}",
            file=sys.stderr,
        )


def _resolve_api_key(name: str, entry: dict, secrets: dict) -> str | None:
    keys = secrets.get("keys") or {}
    if name in keys and keys[name]:
        return str(keys[name]).strip() or None
    env_name = entry.get("api_key_env")
    if env_name:
        val = os.environ.get(str(env_name), "").strip()
        if val:
            return val
    convention = os.environ.get(f"TRANSLATE_KEY_{name.upper()}", "").strip()
    return convention or None


def load_config(
    settings: dict,
    secrets: dict | None = None,
) -> AppConfig:
    """Build the provider table from parsed settings (+ optional secrets).

    Invalid provider entries (unknown type, missing base_url/model) are
    skipped. When nothing valid remains a local Ollama default is used.
    """
    if secrets is None:
        secrets = _read_toml(secrets_path())

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

    if not providers:
        providers["ollama"] = Provider(
            name="ollama",
            type="ollama",
            label="Ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen2.5:3b",
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


def load_ocr_settings(settings: dict) -> OcrSettings:
    table = settings.get("ocr") or {}
    engine = str(table.get("engine", "tesseract")).strip().lower()
    if engine not in ("tesseract", "vision"):
        engine = "tesseract"
    langs = str(table.get("tesseract_langs", "")).strip() or TESSERACT_DEFAULT_LANGS
    provider = str(table.get("provider", "")).strip() or None
    return OcrSettings(engine=engine, tesseract_langs=langs, provider=provider)


def load_actions(settings: dict) -> dict[str, str]:
    """Named prompt templates from the ``[actions]`` table."""
    table = settings.get("actions") or {}
    actions: dict[str, str] = {}
    for name, template in table.items():
        text = str(template).strip()
        if text:
            actions[str(name)] = text
    return actions
