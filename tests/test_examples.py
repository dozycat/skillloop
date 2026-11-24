"""The shipped examples must keep working: they are the first thing a new user runs."""

import shutil
import sys
from pathlib import Path

import pytest

from skillloop import ADAPTERS, SkillLoop
from skillloop.core.config import Config
from skillloop.core.dataset import load_cases

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _copy(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / name
    shutil.copytree(EXAMPLES / name, dest, ignore=shutil.ignore_patterns(".skillloop", "outputs", "__pycache__"))
    return dest


def test_custom_adapter_example_runs_offline(tmp_path):
    root = _copy("custom-adapter", tmp_path)
    es = SkillLoop(root)
    try:
        assert "glossary_agent" in ADAPTERS.names()  # registered by the project-local plugin
        assert es.health()["agent"]["ok"]
        result = es.evolve()
        assert result.best_scores["test"]["mean_score"] == 1.0 > result.baseline_scores["test"]["mean_score"]
    finally:
        es.close()
        sys.path.remove(str(root))
        sys.modules.pop("my_agent", None)


def test_missing_plugin_is_a_clear_error(tmp_path):
    root = _copy("custom-adapter", tmp_path)
    cfg = Config.load(root)
    cfg.project.plugins = ["no_such_plugin_module"]
    cfg.save(root)
    with pytest.raises(ImportError, match="no_such_plugin_module"):
        SkillLoop(root)


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.iterdir() if (p / "skillloop.toml").exists()))
def test_example_inputs_are_well_formed(name):
    root = EXAMPLES / name
    cfg = Config.load(root)
    assert cfg.validate() == []
    assert (root / cfg.project.requirement).read_text(encoding="utf-8").strip()
    cases = load_cases(root / cfg.project.dataset)
    assert len(cases) >= 12 and len({c.id for c in cases}) == len(cases)
    assert (root / cfg.project.seed_skill / "SKILL.md").exists()
