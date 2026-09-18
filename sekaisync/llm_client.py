"""Optional LLM client (zero third-party dependencies).

The LLM path is strictly optional: SekaiSync works fully offline, and the
LLM is only an assistant for terminology extraction / review.  P15 therefore
makes the failure semantics explicit — a missing key is a controlled local
error with **no network traffic at all**, rather than an unauthenticated
request that leaks the prompt to whichever endpoint happens to answer.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# P15 provisional budgets (calibrate against real fixtures later).
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_BODY_CHARS = 200

# Anything that looks like a credential is scrubbed before it can reach a
# log line or an exception message.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|authorization|bearer|token|password|secret)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"),
)


class LLMConfigError(RuntimeError):
    """The LLM is not usable as configured (missing key, missing file, ...)."""


class LLMResponseError(RuntimeError):
    """The provider answered, but not in the shape the protocol requires."""


def redact_secrets(text: str) -> str:
    """Remove anything credential-shaped from a message before it is logged."""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[redacted]", out)
    return out


@dataclass
class LLMConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    api_key: str = ""
    model: str = "gpt-4.1-mini"
    temperature: float = 0.2
    timeout: int = 90
    extra_headers: dict[str, str] = field(default_factory=dict)


def load_llm_config(path: Optional[Path] = None) -> LLMConfig:
    """Load the optional LLM config.

    An explicitly given path (argument or ``SEKAISYNC_LLM_CONFIG``) that does
    not exist is an error: silently continuing with defaults would hide a
    typo behind a network call.  A genuinely unconfigured state keeps the
    documented default behaviour.
    """
    config_path = path
    explicit = config_path is not None
    if config_path is None:
        env_path = os.environ.get("SEKAISYNC_LLM_CONFIG")
        if env_path:
            config_path = Path(env_path)
            explicit = True
    data: dict[str, Any] = {}
    if config_path is not None:
        resolved = Path(config_path)
        if not resolved.exists():
            if explicit:
                raise LLMConfigError(f"LLM config file not found: {resolved}")
        else:
            data = json.loads(resolved.read_text(encoding="utf-8"))
    return LLMConfig(
        base_url=str(data.get("base_url", "https://api.openai.com/v1")),
        api_key_env=str(data.get("api_key_env", "OPENAI_API_KEY")),
        api_key=str(data.get("api_key", "")),
        model=str(data.get("model", "gpt-4.1-mini")),
        temperature=float(data.get("temperature", 0.2)),
        timeout=int(data.get("timeout", 90)),
        extra_headers={str(k): str(v) for k, v in (data.get("extra_headers") or {}).items()},
    )


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    def resolve_api_key(self) -> str:
        if self.config.api_key:
            return self.config.api_key
        return os.environ.get(self.config.api_key_env, "")

    def chat_json(self, system: str, user: str) -> Any:
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        api_key = self.resolve_api_key()
        if not api_key:
            # Controlled failure, zero network traffic (P15).  The message
            # names the env var so the operator can fix it, and contains no
            # prompt text.
            raise LLMConfigError(
                "No LLM API key configured (set "
                f"{self.config.api_key_env} or 'api_key' in the LLM config); "
                "refusing to send an unauthenticated request."
            )
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            **self.config.extra_headers,
            "Authorization": f"Bearer {api_key}",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise LLMResponseError(
                f"LLM request body exceeds {MAX_REQUEST_BYTES} bytes"
            )
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # The response body could echo the request (and therefore the
            # credential); only the status is surfaced.
            raise LLMResponseError(f"LLM HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LLMResponseError(
                f"LLM request failed: {redact_secrets(str(exc.reason))}"
            ) from exc
        except (OSError, TimeoutError) as exc:
            # Connection refused, DNS failure, socket timeout: all local
            # transport faults.  No retry against a different provider.
            raise LLMResponseError(
                f"LLM request failed: {redact_secrets(type(exc).__name__)}"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LLMResponseError(f"LLM response exceeds {MAX_RESPONSE_BYTES} bytes")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMResponseError("LLM response is not valid JSON") from exc
        content = _extract_content(data)
        return _parse_json_content(content)


def _extract_content(data: Any) -> str:
    """Pull ``message.content`` out of a chat completion, or fail cleanly."""
    if not isinstance(data, dict):
        raise LLMResponseError("LLM response is not a JSON object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("LLM response contains no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMResponseError("LLM response choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMResponseError("LLM response choice has no message object")
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMResponseError("LLM response message.content is not a string")
    return content


def _parse_json_content(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # The raw content is NOT echoed: it is model output derived from the
        # prompt and may contain material that must not reach a log.
        raise ValueError(
            f"LLM returned invalid JSON ({len(content)} chars)"
        ) from exc
