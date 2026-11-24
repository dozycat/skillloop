"""生成初版 Skill: draft a first SKILL.md from the requirement document when no seed skill is given."""

from __future__ import annotations

from ..core.models import Case
from ..core.skill import Skill, slugify
from ..llm.base import LLMClient, tagged

SYSTEM = """You write agent skills. A skill is a SKILL.md file: YAML frontmatter with `name` and
`description`, then a markdown body of instructions that a capable agent loads when the description
matches its task. Write for an intelligent reader who lacks only the context: explain what to do and
why, cover the judgement calls and edge cases the requirement implies, and keep it tight - every line
should change what the agent does. The description says what the skill does and when to use it."""

PROMPT = """Write the first version of a skill from the requirement document below.

{requirement}

{skill_name}

{examples}

The examples are sample task inputs the skill will face (answers withheld on purpose): use them to
understand the shape of the work, and do not write guidance that only fits these exact inputs.

Reply with the complete SKILL.md content only - frontmatter first, no code fence, no commentary.
Use exactly the name given in <skill_name>."""


def generate_initial_skill(
    llm: LLMClient, requirement: str, name: str, examples: list[Case] | None = None, max_chars: int = 24000
) -> Skill:
    name = slugify(name)
    sample = [{"input": c.input[:1500], "tags": c.tags} for c in (examples or [])[:6]]
    prompt = PROMPT.format(
        requirement=tagged("requirement", requirement.strip() or "(no requirement document provided)"),
        skill_name=tagged("skill_name", name),
        examples=tagged("examples", sample),
    )
    text = llm.complete(prompt, system=SYSTEM, role="builder").text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    skill = Skill.from_markdown(text)
    skill.name = name
    if not skill.description.strip():
        first = next((ln.strip("# ").strip() for ln in requirement.splitlines() if ln.strip()), name)
        skill.description = f"Use when working on: {first[:200]}"
    if len(skill.body.strip()) < 20:
        skill.body = f"# {name}\n\n{requirement.strip()}\n"
    if skill.size() > max_chars:
        skill.body = skill.body[: max(200, max_chars - 400)].rstrip() + "\n"
    return skill
