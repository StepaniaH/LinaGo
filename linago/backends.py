"""Streaming translation backends — Ollama and OpenAI-compatible."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path

import requests

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
    now = time.monotonic()
    if force or (now - state["last"]) >= 0.04:
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


# ── vision OCR ───────────────────────────────────────────────────────────────
def vision_ocr(provider, image_path: str | Path, *, timeout: int = 180) -> str | None:
    """Transcribe a screenshot with a multimodal model.

    Returns the transcription, "" when the model returned nothing, or
    None on any transport/response failure. Callers treat None as OCR
    failure and empty as "nothing recognized".
    """
    provider.require_ready()
    try:
        encoded = base64.b64encode(Path(image_path).read_bytes()).decode()
        if provider.type == "ollama":
            return _vision_ollama(provider, encoded, timeout)
        if provider.type == "openai":
            return _vision_openai(provider, encoded, timeout)
        raise RuntimeError(f"unsupported provider type: {provider.type}")
    except (
        requests.RequestException,
        OSError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        return None


def _vision_openai(provider, image_b64: str, timeout: int) -> str:
    url = f"{provider.base_url}/chat/completions"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": provider.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": TRANSCRIBE_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                }
            ],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]["content"]
    text = (message or "").strip()
    return text or ""


def _vision_ollama(provider, image_b64: str, timeout: int) -> str:
    url = f"{provider.base_url}/api/generate"
    resp = requests.post(
        url,
        json={
            "model": provider.model,
            "prompt": TRANSCRIBE_PROMPT,
            "images": [image_b64],
            "stream": False,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    text = (resp.json().get("response") or "").strip()
    return text or ""
