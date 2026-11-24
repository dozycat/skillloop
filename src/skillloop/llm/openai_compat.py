"""Any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI, Azure-style gateways, vLLM,
Ollama, DeepSeek, Qwen/DashScope, OpenRouter ...). Standard library only.

    [llm]
    provider = "openai"
    model = "gpt-5"
    [llm.options]
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"
    structured = "none"            # "none" | "json_object" | "json_schema"
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from typing import Any

from ..core.registry import LLMS
from .base import LLMClient, LLMError, LLMResponse

_RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


@LLMS.register("openai")
class OpenAICompatLLM(LLMClient):
    name = "openai"

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        api_key_env: str = "OPENAI_API_KEY",
        api_key: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 300.0,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        structured: str = "none",
        max_tokens_param: str = "max_tokens",
        extra_body: dict[str, Any] | None = None,
        **options: Any,
    ):
        super().__init__(model, **options)
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key_env, self._api_key = api_key_env, api_key
        self.headers = dict(headers or {})
        self.timeout, self.max_retries, self.backoff_s = timeout, max_retries, backoff_s
        if structured not in ("none", "json_object", "json_schema"):
            raise ValueError("structured must be 'none', 'json_object' or 'json_schema'")
        self.structured, self.max_tokens_param = structured, max_tokens_param
        self.extra_body = dict(extra_body or {})

    def _key(self) -> str:
        return self._api_key or os.environ.get(self.api_key_env, "")

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", **self.headers}
        if self._key():
            headers.setdefault("Authorization", f"Bearer {self._key()}")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = ""
        for attempt in range(self.max_retries + 1):
            delay = min(self.backoff_s * (2**attempt), 30.0) + random.uniform(0, self.backoff_s / 4)
            req = urllib.request.Request(f"{self.base_url}/chat/completions", data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:500]
                last = f"HTTP {e.code}: {detail}"
                if e.code not in _RETRYABLE:
                    raise LLMError(f"openai-compatible endpoint error ({self.base_url}): {last}") from e
                retry_after = e.headers.get("retry-after") if e.headers else None
                try:
                    delay = min(float(retry_after), 60.0) if retry_after else delay
                except ValueError:
                    pass
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = f"{type(e).__name__}: {e}"
            except json.JSONDecodeError as e:
                raise LLMError(f"openai-compatible endpoint returned non-JSON ({self.base_url})") from e
            if attempt < self.max_retries:
                time.sleep(delay)
        raise LLMError(
            f"openai-compatible endpoint failed after {self.max_retries + 1} attempts ({self.base_url}): {last}"
        )

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 16000,
        json_schema: dict[str, Any] | None = None,
        role: str = "",
    ) -> LLMResponse:
        if not self.model:
            raise LLMError("openai provider: set [llm] model")
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        body: dict[str, Any] = {"model": self.model, "messages": messages, **self.extra_body}
        if self.max_tokens_param:
            body[self.max_tokens_param] = max_tokens
        if json_schema is not None and self.structured == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": role or "result", "schema": json_schema},
            }
        elif json_schema is not None and self.structured == "json_object":
            body["response_format"] = {"type": "json_object"}
        data = self._request(body)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise LLMError(f"unexpected response shape from {self.base_url}: {str(data)[:300]}") from e
        if isinstance(content, list):  # some gateways return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content is None:
            refusal = choice["message"].get("refusal")
            raise LLMError("model returned no content" + (f" (refusal: {refusal})" if refusal else ""))
        u = data.get("usage") or {}
        usage = {}
        if isinstance(u.get("prompt_tokens"), (int, float)):
            usage["input_tokens"] = float(u["prompt_tokens"])
        if isinstance(u.get("completion_tokens"), (int, float)):
            usage["output_tokens"] = float(u["completion_tokens"])
        return LLMResponse(text=str(content), model=str(data.get("model") or self.model), usage=usage, raw=data)

    def healthcheck(self) -> tuple[bool, str]:
        if not self.model:
            return False, "no model configured"
        local = any(h in self.base_url for h in ("localhost", "127.0.0.1", "0.0.0.0"))
        if not self._key() and not local and "Authorization" not in self.headers:
            return False, f"environment variable {self.api_key_env} is not set"
        return True, f"{self.base_url} (model {self.model})"
