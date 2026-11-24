"""Claude via the official ``anthropic`` SDK (optional dependency: ``pip install 'skillloop[anthropic]'``).

    [llm]
    provider = "anthropic"
    model = "claude-opus-5-5"      # default
    [llm.options]
    effort = "high"                # low | medium | high | xhigh | max  (omit for the model default)

Credentials are resolved by the SDK (``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` / an
``ant auth login`` profile); nothing is read or stored here. Requests are streamed (no HTTP
timeouts on long outputs), use adaptive thinking, enforce ``json_schema`` through structured
outputs when given, and opt into server-side refusal fallbacks."""

from __future__ import annotations

import threading
from typing import Any

from ..core.registry import LLMS
from .base import LLMClient, LLMError, LLMResponse

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# models that take a fixed thinking budget instead of adaptive thinking: leave `thinking` unset there
_NO_ADAPTIVE = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5", "claude-3")


def _sdk():
    try:
        import anthropic  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError as e:
        raise LLMError("the Anthropic provider needs the official SDK: pip install 'skillloop[anthropic]'") from e
    return anthropic


@LLMS.register("anthropic")
class AnthropicLLM(LLMClient):
    name = "anthropic"

    def __init__(
        self,
        model: str = "",
        effort: str = "",
        max_retries: int = 2,
        timeout: float = 600.0,
        thinking: str = "adaptive",
        fallbacks: Any = "default",
        base_url: str = "",
        **options: Any,
    ):
        super().__init__(model or DEFAULT_MODEL, **options)
        self.effort, self.max_retries, self.timeout = effort, max_retries, timeout
        self.thinking = thinking  # "adaptive" | "" (omit the parameter)
        self.fallbacks = fallbacks  # "default" | [{"model": ...}] | "" / None to disable
        self.base_url = base_url
        self._client: Any = None
        self._lock = threading.Lock()
        self._no_fallbacks = False  # set once the API rejects the fallback beta for this deployment
        self._no_schema = False

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                anthropic = _sdk()
                kwargs: dict[str, Any] = {"max_retries": self.max_retries, "timeout": self.timeout}
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = anthropic.Anthropic(**kwargs)  # credentials come from the environment
            return self._client

    def _params(self, prompt: str, system: str, max_tokens: int, schema: dict[str, Any] | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            params["system"] = system
        if self.thinking == "adaptive" and not self.model.startswith(_NO_ADAPTIVE):
            params["thinking"] = {"type": "adaptive"}
        output_config: dict[str, Any] = {}
        if self.effort:
            output_config["effort"] = self.effort
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            params["output_config"] = output_config
        return params

    def _call(self, client: Any, params: dict[str, Any], use_fallbacks: bool) -> Any:
        if use_fallbacks:
            with client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks=self.fallbacks, **params) as stream:
                return stream.get_final_message()
        with client.messages.stream(**params) as stream:
            return stream.get_final_message()

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 16000,
        json_schema: dict[str, Any] | None = None,
        role: str = "",
    ) -> LLMResponse:
        anthropic = _sdk()
        client = self._get_client()
        use_fallbacks = bool(self.fallbacks) and not self._no_fallbacks
        schema = None if self._no_schema else json_schema
        # Degrade on 400s in order: drop the fallback beta, then drop the schema (plain prompting;
        # the caller's prompt already asks for JSON and complete_json() extracts it).
        while True:
            try:
                message = self._call(client, self._params(prompt, system, max_tokens, schema), use_fallbacks)
                break
            except anthropic.BadRequestError as e:
                if use_fallbacks:
                    use_fallbacks = False
                    self._no_fallbacks = True
                    continue
                if schema is not None:
                    schema = None
                    continue
                raise LLMError(f"anthropic bad request: {e.message}") from e
            except anthropic.AuthenticationError as e:
                raise LLMError("anthropic authentication failed: set ANTHROPIC_API_KEY or run `ant auth login`") from e
            except anthropic.PermissionDeniedError as e:
                raise LLMError(f"anthropic permission denied: {e.message}") from e
            except anthropic.NotFoundError as e:
                raise LLMError(f"anthropic model or endpoint not found ({self.model}): {e.message}") from e
            except anthropic.RateLimitError as e:
                raise LLMError(f"anthropic rate limit exceeded after {self.max_retries} retries: {e.message}") from e
            except anthropic.APIStatusError as e:
                raise LLMError(f"anthropic API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise LLMError(f"anthropic connection error: {e}") from e

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise LLMError(f"model refused the request (category: {category})")
        text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
        if message.stop_reason == "max_tokens" and not text.strip():
            raise LLMError(f"model hit max_tokens ({max_tokens}) before producing any text")
        u = message.usage
        usage = {
            k: float(getattr(u, k))
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            if isinstance(getattr(u, k, None), (int, float))
        }
        return LLMResponse(text=text, model=getattr(message, "model", self.model), usage=usage, raw=message)

    def healthcheck(self) -> tuple[bool, str]:
        try:
            _sdk()
            self._get_client()
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        return True, f"anthropic SDK ready (model {self.model})"
