"""Use a command-line model as the framework LLM - reuses an existing CLI login, no API key.

Default: Claude Code in print mode with tools disabled (a pure completion)::

    [llm]
    provider = "cli"
    model = "sonnet"               # optional, passed as --model

Any other CLI via a template (placeholders {prompt} {system} {model}; without {prompt} the
prompt goes to stdin, without {system} the system text is prepended to the prompt)::

    [llm.options]
    command = ["llm", "-m", "{model}", "-s", "{system}"]
"""

from __future__ import annotations

import re
import shlex
import shutil
from typing import Any

from ..adapters._proc import run_process
from ..core.registry import LLMS
from .base import LLMClient, LLMError, LLMResponse

CLAUDE_COMMAND = [
    "claude",
    "-p",
    "--output-format",
    "text",
    "--tools",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--system-prompt",
    "{system}",
]
DEFAULT_SYSTEM = "You are a precise assistant. Follow the instructions exactly."


@LLMS.register("cli")
class CLILLM(LLMClient):
    name = "cli"

    def __init__(
        self,
        model: str = "",
        command: list[str] | str | None = None,
        timeout: float = 600.0,
        env: dict[str, str] | None = None,
        cwd: str = "",
        **options: Any,
    ):
        super().__init__(model, **options)
        if command is None:
            self.command = list(CLAUDE_COMMAND) + (["--model", "{model}"] if model else [])
        else:
            self.command = shlex.split(command) if isinstance(command, str) else [str(c) for c in command]
        self.timeout, self.env, self.cwd = timeout, dict(env or {}), cwd

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 16000,
        json_schema: dict[str, Any] | None = None,
        role: str = "",
    ) -> LLMResponse:
        if not self.command:
            raise LLMError("cli provider: empty command")
        has_prompt = any("{prompt}" in p for p in self.command)
        has_system = any("{system}" in p for p in self.command)
        if json_schema is not None:
            prompt += "\n\nReply with ONLY a JSON value (no prose, no code fences)."
        full_prompt = prompt if has_system or not system else f"{system}\n\n{prompt}"
        values = {"prompt": full_prompt, "system": system or DEFAULT_SYSTEM, "model": self.model}
        cmd = [re.sub(r"\{(prompt|system|model)\}", lambda m: values[m.group(1)], p) for p in self.command]
        res = run_process(
            cmd, cwd=self.cwd or None, env=self.env, stdin=None if has_prompt else full_prompt, timeout_s=self.timeout
        )
        if res.status != "ok":
            raise LLMError(f"cli provider ({cmd[0]}) failed: {res.error or res.status}")
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", res.stdout).strip()
        if not text:
            raise LLMError(f"cli provider ({cmd[0]}) produced no output: {res.stderr.strip()[-300:]}")
        return LLMResponse(
            text=text,
            model=self.model or cmd[0],
            usage={"input_tokens": len(full_prompt) // 4, "output_tokens": len(text) // 4},
        )

    def healthcheck(self) -> tuple[bool, str]:
        path = shutil.which(self.command[0]) if self.command else None
        return (
            (True, f"found {path}")
            if path
            else (False, f"binary {self.command[0] if self.command else ''!r} not found on PATH")
        )
