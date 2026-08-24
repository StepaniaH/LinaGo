"""Self-checks for the local environment, configuration, and backends.

Shared by the ``linago --doctor`` command and the web console's
diagnostics tab. Connectivity probes are warnings by nature — an
offline provider does not make the installation wrong.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

import linago
from linago.backends import probe_ollama, probe_openai
from linago.config import load_config, load_ocr_settings, load_settings
from linago.daemon import daemon_alive, default_socket_path

_BINARIES = [
    ("slurp", True),
    ("grim", True),
    ("tesseract", True),
    ("wl-copy", False),
    ("wl-paste", False),
    ("hyprctl", False),
    ("pw-play", False),
    ("paplay", False),
]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning_only: bool = False

    @property
    def fatal(self) -> bool:
        return not self.ok and not self.warning_only


def _check_binaries(which: Callable[[str], str | None]) -> list[Check]:
    checks = []
    missing_optional = []
    for name, hard in _BINARIES:
        found = which(name)
        if found:
            continue
        if hard:
            checks.append(Check(f"binary:{name}", False, "not found"))
        else:
            missing_optional.append(name)
    if missing_optional:
        checks.append(
            Check(
                "binary:optional",
                True,
                "not found (some features disabled): " + ", ".join(missing_optional),
                warning_only=True,
            )
        )
    return checks


def _check_tesseract_langs(
    runner: Callable[..., subprocess.CompletedProcess],
) -> Check:
    try:
        proc = runner(["tesseract", "--list-langs"], capture_output=True, text=True)
    except OSError:
        return Check("tesseract-langs", False, "tesseract not runnable")
    out = (proc.stdout or "") + (proc.stderr or "")
    installed = [lang for lang in ("chi_sim", "eng") if lang in out]
    if len(installed) == 2:
        return Check("tesseract-langs", True, "chi_sim + eng")
    return Check(
        "tesseract-langs",
        False,
        f"missing traineddata: {sorted(set(['chi_sim', 'eng']) - set(installed))}",
    )


def run_checks(
    config=None,
    *,
    probe: bool = True,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    socket_path: str | None = None,
) -> list[Check]:
    checks: list[Check] = []
    checks += _check_binaries(which)

    settings = load_settings()
    try:
        cfg = config or load_config(settings)
        ocr_cfg = load_ocr_settings(settings)
        checks.append(Check("config", True, f"{len(cfg.providers)} provider(s)"))
        active = cfg.get()
        ready_error = None
        try:
            active.require_ready()
        except RuntimeError as exc:
            ready_error = str(exc)
        if ready_error:
            checks.append(
                Check("active-provider-key", False, ready_error, warning_only=True)
            )
        else:
            checks.append(Check("active-provider-key", True, active.display))
        if probe:
            for provider in cfg.providers.values():
                if provider.type == "ollama":
                    ok, detail = probe_ollama(provider)
                else:
                    ok, detail = (
                        probe_openai(provider)
                        if provider.api_key
                        else (False, "no API key configured")
                    )
                checks.append(
                    Check(
                        f"reachability:{provider.name}",
                        ok,
                        detail,
                        warning_only=True,
                    )
                )
        engine_checks = (
            [_check_tesseract_langs(runner)] if ocr_cfg.engine == "tesseract" else []
        )
        checks += engine_checks
    except Exception as exc:  # config errors must not crash the report
        checks.append(Check("config", False, f"{type(exc).__name__}: {exc}"))
        return checks

    path = socket_path or default_socket_path()
    state = "running" if daemon_alive(path) else "not running"
    checks.append(Check("daemon-socket", True, f"{state} ({path})", warning_only=True))
    checks.append(Check("version", True, f"linago {linago.__version__}"))
    return checks


def has_fatal(checks: list[Check]) -> bool:
    return any(c.fatal for c in checks)
