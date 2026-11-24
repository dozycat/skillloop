"""The simplest "agent": one model call with the skill as the system prompt.

[agent]
adapter = "llm"
[agent.options]
provider = "anthropic"     # any registered LLM provider (anthropic | openai | cli | mock | pkg:Class)
model = "claude-opus-5-5"
max_tokens = 16000
[agent.options.llm_options]  # passed to the provider constructor
"""

from __future__ import annotations

import time
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS, LLMS
from ..llm.base import LLMClient
from .base import AgentAdapter, AgentRequest

DEFAULT_SYSTEM = (
    "You are an assistant equipped with the skill below. Follow it whenever it applies, "
    "and answer the user's task directly.\n\n{skill}"
)


@ADAPTERS.register("llm")
class LLMAdapter(AgentAdapter):
    name = "llm"

    def __init__(
        self,
        provider: str = "mock",
        model: str = "",
        llm_options: dict[str, Any] | None = None,
        max_tokens: int = 16000,
        system_template: str = DEFAULT_SYSTEM,
        client: LLMClient | None = None,
        **options: Any,
    ):
        super().__init__(**options)
        self.provider, self.model = provider, model
        self.max_tokens, self.system_template = max_tokens, system_template
        self.client = client or LLMS.create(provider, model=model, **(llm_options or {}))

    def run(self, request: AgentRequest) -> Trajectory:
        t0 = time.time()
        cid = request.case.id
        if request.cancel.is_set():
            return Trajectory(case_id=cid, status="cancelled", error="cancelled")
        try:
            system = self.system_template.replace("{skill}", request.skill.render_for_prompt())
            prompt = request.case.input
            if request.case.files:
                prompt += "\n\n" + "\n\n".join(
                    f'<file path="{p}">\n{c}\n</file>' for p, c in request.case.files.items()
                )
            resp = self.client.complete(prompt, system=system, max_tokens=self.max_tokens, role="agent")
            usage = {k: v for k, v in (resp.usage or {}).items() if isinstance(v, (int, float))}
            usage["turns"] = 1
            return Trajectory(
                case_id=cid,
                output=resp.text.strip(),
                steps=[Step("message", resp.text.strip())],
                usage=usage,
                duration_s=time.time() - t0,
                raw=resp.text[:200_000],
            )
        except Exception as e:  # noqa: BLE001 - adapters never raise
            return Trajectory(
                case_id=cid, status="error", error=f"{type(e).__name__}: {e}", duration_s=time.time() - t0
            )

    def healthcheck(self) -> tuple[bool, str]:
        try:
            return self.client.healthcheck()
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
