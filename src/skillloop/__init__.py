"""skillloop - a Skill 进化闭环 framework that plugs into any agent system.

    from skillloop import SkillLoop
    es = SkillLoop("my_project")        # a directory with skillloop.toml, requirement.md, dataset.jsonl
    result = es.evolve()               # evaluate → judge → error book → iterate → compare → promote
    print(result.best_version, es.version()["skill_md"])

Extension points (see docs/): ``AgentAdapter`` (plug in an agent system), ``Judge`` (scoring),
``LLMClient`` (the model used for judging/analysis), each with a registry decorator."""

__version__ = "0.1.0"

from .adapters.base import AgentAdapter, AgentRequest  # noqa: E402
from .core.config import Config  # noqa: E402
from .core.models import Case, EvalReport, Judgment, SkillVersion, Step, Trajectory  # noqa: E402
from .core.project import Project  # noqa: E402
from .core.registry import ADAPTERS, JUDGES, LLMS  # noqa: E402
from .core.skill import Skill  # noqa: E402
from .eval.base import Judge, JudgeContext  # noqa: E402
from .eval.runner import Evaluator  # noqa: E402
from .llm.base import LLMClient, LLMResponse  # noqa: E402
from .service import SkillLoop, Workspace  # noqa: E402

__all__ = [
    "ADAPTERS",
    "JUDGES",
    "LLMS",
    "AgentAdapter",
    "AgentRequest",
    "Case",
    "Config",
    "EvalReport",
    "Evaluator",
    "SkillLoop",
    "Judge",
    "JudgeContext",
    "Judgment",
    "LLMClient",
    "LLMResponse",
    "Project",
    "Skill",
    "SkillVersion",
    "Step",
    "Trajectory",
    "Workspace",
    "__version__",
]
