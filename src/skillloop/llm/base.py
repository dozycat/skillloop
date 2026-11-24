"""LLM abstraction used by the framework itself (judge / analyst / merger / builder / devset).

Deliberately tiny: one ``complete`` call. Every prompt passes a ``role`` so that (a) config can
route roles to different models and (b) the mock client can answer deterministically.
Prompts carry their data in ``<tag>...</tag>`` blocks (see :func:`tagged`)."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..core.config import LLMSection
from ..core.registry import LLMS


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str = ""
    usage: dict[str, float] = field(default_factory=dict)  # input_tokens, output_tokens
    raw: Any = None


def tagged(name: str, content: Any) -> str:
    """Wrap content in an XML-ish block; non-strings are JSON-encoded."""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=2, default=str)
    return f"<{name}>\n{content}\n</{name}>"


def extract_tag(text: str, name: str, default: str = "") -> str:
    m = re.search(rf"<{re.escape(name)}>\n?(.*?)\n?</{re.escape(name)}>", text, re.S)
    return m.group(1) if m else default


def extract_tag_json(text: str, name: str, default: Any = None) -> Any:
    raw = extract_tag(text, name, "")
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply (handles code fences and chatter)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise LLMError(f"no JSON found in model reply: {text[:200]!r}")


class LLMClient(ABC):
    """Implement ``complete``; everything else is provided."""

    name = "llm"

    def __init__(self, model: str = "", **options: Any):
        self.model = model
        self.options = options

    @abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 16000,
        json_schema: dict[str, Any] | None = None,
        role: str = "",
    ) -> LLMResponse:
        """Return the model's reply. ``json_schema`` is a hint: providers with structured
        output should enforce it; others must at least be asked (by the caller's prompt) for JSON."""

    def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema: dict[str, Any] | None = None,
        role: str = "",
        max_tokens: int = 16000,
        retries: int = 1,
    ) -> Any:
        """``complete`` + tolerant JSON extraction, re-asking once on malformed output."""
        ask = prompt
        last: Exception | None = None
        for _ in range(retries + 1):
            resp = self.complete(ask, system=system, max_tokens=max_tokens, json_schema=schema, role=role)
            try:
                return extract_json(resp.text)
            except LLMError as e:
                last = e
                ask = prompt + "\n\nYour previous reply was not valid JSON. Reply with ONLY the JSON value, no prose."
        raise LLMError(str(last))

    def healthcheck(self) -> tuple[bool, str]:
        return True, "ok"


class LLMRouter(LLMClient):
    """Dispatch by role: ``roles[role]`` if configured, else the default client."""

    name = "router"

    def __init__(self, default: LLMClient, roles: dict[str, LLMClient] | None = None):
        super().__init__(default.model)
        self.default = default
        self.roles = roles or {}

    def for_role(self, role: str) -> LLMClient:
        return self.roles.get(role, self.default)

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 16000,
        json_schema: dict[str, Any] | None = None,
        role: str = "",
    ) -> LLMResponse:
        return self.for_role(role).complete(
            prompt, system=system, max_tokens=max_tokens, json_schema=json_schema, role=role
        )

    def healthcheck(self) -> tuple[bool, str]:
        return self.default.healthcheck()


def build_llm(section: LLMSection) -> LLMClient:
    """Instantiate the configured LLM (with per-role overrides) from the ``[llm]`` config section."""
    default = LLMS.create(section.provider, model=section.model, **section.options)
    roles = {}
    for role, spec in section.roles.items():
        roles[role] = LLMS.create(
            spec.get("provider", section.provider),
            model=spec.get("model", section.model),
            **{**section.options, **spec.get("options", {})},
        )
    return LLMRouter(default, roles) if roles else default
