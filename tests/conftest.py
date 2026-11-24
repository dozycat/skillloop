import pytest

from skillloop.core.project import Project
from skillloop.demo import REQUIREMENT, SEED_SKILL, demo_cases, demo_config


@pytest.fixture
def demo_project(tmp_path):
    """The offline demo project (simulated agent + LLM)."""
    p = Project.init(
        tmp_path / "demo", config=demo_config(), requirement=REQUIREMENT, seed_skill=SEED_SKILL, cases=demo_cases()
    )
    yield p
    p.close()
