from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


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
    config_path = path
    if config_path is None:
        env_path = os.environ.get("SEKAISYNC_LLM_CONFIG")
        if env_path:
            config_path = Path(env_path)
    data: dict[str, Any] = {}
    if config_path is not None and Path(config_path).exists():
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
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
        }
        api_key = self.resolve_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc.reason}") from exc
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        return _parse_json_content(content)


def _parse_json_content(content: str) -> Any:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned invalid JSON: {content[:300]}") from exc
