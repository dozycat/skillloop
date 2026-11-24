"""A complete custom adapter in one file - the template for plugging your own agent system in.

`skillloop.toml` lists this module under `[project] plugins`, so it is imported at startup and the
`@ADAPTERS.register` decorator below makes `adapter = "glossary_agent"` available. Nothing needs to
be installed or packaged: the project directory is put on `sys.path`.

The "agent" here is a toy that needs no model, so the example runs offline: it can only expand an
abbreviation if the skill's glossary defines it. Replace the body of `run` with a call into your
real agent - the contract stays the same:

* read the task from `request.case.input` and the skill from `request.skill`
* work inside `request.workdir`; respect `request.timeout_s` and `request.cancel`
* return a `Trajectory`: the final answer in `output`, whatever trace you have in `steps`
* never raise for agent-side failures - return `Trajectory(status="error", error=...)` instead
"""

import re

from skillloop import ADAPTERS, AgentAdapter, AgentRequest, Step, Trajectory

GLOSSARY_LINE = re.compile(r"^- \[([^\]]+)\]\s*(.+)$", re.M)


@ADAPTERS.register("glossary_agent")
class GlossaryAgent(AgentAdapter):
    name = "glossary_agent"

    def __init__(self, unknown_reply: str = "I do not know this abbreviation.", **options):
        super().__init__(**options)  # keys of [agent.options] arrive here as keyword arguments
        self.unknown_reply = unknown_reply

    def run(self, request: AgentRequest) -> Trajectory:
        # 1. Hand the skill to the agent. A real adapter would install it where the agent looks for
        #    skills, e.g.  self.install_skill(request.skill, request.workdir / ".my-agent" / "skills")
        glossary = {k.lower(): v.strip() for k, v in GLOSSARY_LINE.findall(request.skill.body)}

        # 2. Run the task.
        term = request.case.input.strip().rstrip("?").split()[-1].lower()
        steps = [Step("thought", f"Looking up '{term}' in the skill glossary ({len(glossary)} entries).")]
        if term in glossary:
            answer = glossary[term]
        else:
            # The simulated analyst (the mock LLM used by this example) learns from lines of this
            # shape; with a real LLM as analyst, any honest trace of what went wrong works.
            hint = request.case.meta.get("hints", {}).get(term, "")
            steps.append(Step("thought", f"MISSING_GUIDANCE: {term} :: {hint}".rstrip(" :")))
            answer = self.unknown_reply

        # 3. Report what happened.
        steps.append(Step("message", answer))
        return Trajectory(output=answer, steps=steps, usage={"turns": 1})

    def healthcheck(self) -> tuple[bool, str]:
        return True, "toy agent, always available"  # a real adapter checks its binary / endpoint here
