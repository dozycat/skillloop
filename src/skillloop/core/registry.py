"""Plugin registries. Third parties extend the framework three ways:

1. ``@ADAPTERS.register("my_agent")`` in their own code (import it before use);
2. a ``"package.module:ClassName"`` spec written directly in config;
3. a Python entry point in the group ``skillloop.adapters`` / ``skillloop.judges`` / ``skillloop.llm``.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str, builtin_modules: tuple[str, ...] = ()):
        self.kind = kind
        self._factories: dict[str, Callable[..., T]] = {}
        self._builtin_modules = builtin_modules
        self._loaded = False

    def register(self, name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
        def deco(factory: Callable[..., T]) -> Callable[..., T]:
            self._factories[name] = factory
            return factory

        return deco

    def add(self, name: str, factory: Callable[..., T]) -> None:
        self._factories[name] = factory

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        for mod in self._builtin_modules:
            try:
                importlib.import_module(mod)
            except ImportError as e:  # an optional built-in missing its extra dependency
                if e.name and (e.name == mod or not e.name.startswith("skillloop")):
                    continue
                raise
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group=f"skillloop.{self.kind}"):
                self._factories.setdefault(ep.name, ep.load())
        except Exception:
            pass

    def names(self) -> list[str]:
        self._load()
        return sorted(self._factories)

    def get(self, name: str) -> Callable[..., T]:
        self._load()
        if name in self._factories:
            return self._factories[name]
        if ":" in name:  # "pkg.module:attr"
            mod, _, attr = name.partition(":")
            return getattr(importlib.import_module(mod), attr)
        raise KeyError(f"unknown {self.kind} {name!r}; available: {', '.join(self.names())}")

    def create(self, name: str, **options: Any) -> T:
        return self.get(name)(**options)


ADAPTERS: Registry = Registry(
    "adapters",
    (
        "skillloop.adapters.mock",
        "skillloop.adapters.command",
        "skillloop.adapters.claude_code",
        "skillloop.adapters.codex",
        "skillloop.adapters.nanobot",
        "skillloop.adapters.http",
        "skillloop.adapters.llm",
        "skillloop.adapters.callable",
    ),
)
JUDGES: Registry = Registry("judges", ("skillloop.eval.judges", "skillloop.eval.answer"))
LLMS: Registry = Registry(
    "llm",
    ("skillloop.llm.mock", "skillloop.llm.anthropic", "skillloop.llm.openai_compat", "skillloop.llm.cli"),
)
