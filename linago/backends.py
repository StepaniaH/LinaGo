"""Streaming translation backends — Ollama and OpenAI-compatible."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import requests

from linago.lang import normalize_text

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], None]

# Instruction used when a multimodal model does the OCR step.
TRANSCRIBE_PROMPT = (
    "Transcribe ALL text visible in this image exactly as written, "
    "preserving the original reading order and line breaks. "
    "Output only the transcription, no commentary."
)


def stream_completion(
    provider,
    prompt: str,
    on_token: TokenCallback,
    cancel,
    *,
    timeout: int | None = None,
) -> None:
    """Blocking stream; call from a worker thread. Invokes on_token(full)."""
    effective_timeout = provider.timeout_s or timeout or 120
    if provider.type == "ollama":
        _stream_ollama(provider, prompt, on_token, cancel, effective_timeout)
    elif provider.type == "openai":
        _stream_openai(provider, prompt, on_token, cancel, effective_timeout)
    else:
        raise RuntimeError(f"unsupported provider type: {provider.type}")


def _auth_headers(provider) -> dict:
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


def _emit_coalesced(on_token, full: str, state: dict, force: bool = False):
    now = time.monotonic()
    if force or (now - state["last"]) >= 0.04:
        state["last"] = now
        on_token(full)


def _post_with_retry(
    url: str,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    **kwargs,
):
    """POST with bounded retries on connection-level failures.

    Timeouts and dropped connections are usually transient; HTTP status
    errors are raised immediately because repeating a rejected request
    does not improve it.
    """
    for attempt in range(attempts):
        try:
            return requests.post(url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt + 1 >= attempts:
                raise
            delay = base_delay * (2**attempt)
            logger.warning(
                "request to %s failed (%s), retrying in %.1fs",
                url,
                exc,
                delay,
            )
            time.sleep(delay)


def _stream_ollama(provider, prompt, on_token, cancel, timeout):
    url = f"{provider.base_url}/api/generate"
    logger.debug("ollama request: model=%s", provider.model)
    body: dict = {"model": provider.model, "prompt": prompt, "stream": True}
    options: dict = {}
    if provider.temperature is not None:
        options["temperature"] = provider.temperature
    if provider.max_tokens is not None:
        options["num_predict"] = provider.max_tokens
    if options:
        body["options"] = options
    resp = _post_with_retry(
        url,
        json=body,
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
    headers = _auth_headers(provider)
    payload = {
        "model": provider.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "temperature": 0.2 if provider.temperature is None else provider.temperature,
    }
    if provider.max_tokens is not None:
        payload["max_tokens"] = provider.max_tokens
    logger.debug("openai-compatible request: model=%s", provider.model)
    resp = _post_with_retry(
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


# ── vision OCR ───────────────────────────────────────────────────────────────
def _vision_request(
    provider,
    image_path: str | Path,
    *,
    stream: bool,
    timeout: int,
):
    """Build and send the multimodal request for either backend type."""
    encoded = base64.b64encode(Path(image_path).read_bytes()).decode()
    if provider.type == "openai":
        url = f"{provider.base_url}/chat/completions"
        headers = _auth_headers(provider)
        body = {
            "model": provider.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": TRANSCRIBE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                }
            ],
            "stream": stream,
        }
        if stream:
            body["temperature"] = 0.0
        return url, headers, body
    if provider.type == "ollama":
        url = f"{provider.base_url}/api/generate"
        body = {
            "model": provider.model,
            "prompt": TRANSCRIBE_PROMPT,
            "images": [encoded],
            "stream": stream,
        }
        return url, None, body
    raise RuntimeError(f"unsupported provider type: {provider.type}")


def _cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def stream_vision_ocr(
    provider,
    image_path: str | Path,
    on_token,
    cancel,
    *,
    timeout: int = 180,
) -> str | None:
    """Transcribe a screenshot, emitting accumulated text while streaming.

    Contract mirrors vision_ocr: the final transcription, "" when the
    model produced nothing, or None on transport/response failure.
    on_token(full_so_far) fires at most every 40 ms plus a forced final
    emit; it never receives error text.
    """
    try:
        url, headers, body = _vision_request(
            provider, image_path, stream=True, timeout=timeout
        )
        logger.debug("vision request via %s: model=%s", provider.type, provider.model)
        resp = _post_with_retry(
            url, headers=headers, json=body, stream=True, timeout=timeout
        )
        resp.raise_for_status()
        full = ""
        state = {"last": 0.0}
        # Wire formats are told apart per line, not by content type:
        # SSE frames start with "data:", Ollama streams bare JSON.
        for line in resp.iter_lines(decode_unicode=True):
            if _cancelled(cancel):
                return normalize_vision_text(full)
            if not line:
                continue
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                piece = (
                    (choices[0].get("delta") or {}).get("content") if choices else ""
                )
            else:
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = chunk.get("response", "")
            if not piece:
                continue
            full += piece
            done = bool(chunk.get("done"))
            if on_token is not None:
                _emit_coalesced(on_token, full, state, force=done)
            if done:
                break
        result = normalize_vision_text(full)
        if not _cancelled(cancel) and on_token is not None and result:
            on_token(result)
        return result
    except (
        requests.RequestException,
        OSError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        logger.warning("vision OCR failed", exc_info=True)
        return None


def normalize_vision_text(text: str) -> str:
    return normalize_text(text)


def vision_ocr(provider, image_path: str | Path, *, timeout: int = 180):
    """Blocking wrapper over :func:`stream_vision_ocr` without output."""
    return stream_vision_ocr(provider, image_path, None, None, timeout=timeout)


def tts_speech(
    provider,
    text: str,
    *,
    voice: str = "alloy",
    timeout: int | None = None,
):
    """Synthesize speech for *text* via an OpenAI-compatible endpoint.

    Returns raw audio bytes. Only ``openai``-type providers implement
    TTS; anything else raises RuntimeError so callers can disable the
    control up front.
    """
    if provider.type != "openai":
        raise RuntimeError(f"provider type '{provider.type}' does not support TTS")
    url = f"{provider.base_url}/audio/speech"
    resp = _post_with_retry(
        url,
        headers=_auth_headers(provider),
        json={
            "model": provider.model,
            "voice": voice,
            "input": text,
        },
        timeout=timeout or provider.timeout_s or 120,
    )
    resp.raise_for_status()
    return resp.content


def probe_ollama(provider, *, timeout: int = 3) -> tuple[bool, str]:
    """Reachability check against Ollama's /api/tags."""
    try:
        resp = requests.get(f"{provider.base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
        names = [m.get("name") for m in resp.json().get("models", [])]
        return True, f"reachable, {len(names)} model(s)"
    except requests.RequestException as exc:
        return False, str(exc)[:120]


def probe_openai(provider, *, timeout: int = 3) -> tuple[bool, str]:
    """Reachability check against an OpenAI-compatible /models list."""
    try:
        resp = requests.get(
            f"{provider.base_url}/models",
            headers=_auth_headers(provider),
            timeout=timeout,
        )
        if resp.status_code == 401:
            return False, "unauthorized: check the API key"
        resp.raise_for_status()
        data = resp.json().get("data") or []
        return True, f"reachable, {len(data)} model(s)"
    except requests.RequestException as exc:
        return False, str(exc)[:120]
