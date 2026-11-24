"""In-process adapters: plug a Python function in as the agent.

Config form (function imported from a dotted path)::

    [agent]
    adapter = "callable"
    [agent.options]
    target = "my_pkg.my_agent:run_case"   # (AgentRequest) -> Trajectory | str | dict

SDK form::

    from skillloop.adapters.callable import FunctionAdapter
    adapter = FunctionAdapter(lambda req: my_agent(req.case.input, skill=req.skill.render_for_prompt()))
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Callable
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS
from .base import AgentAdapter, AgentRequest

AgentFn = Callable[[AgentRequest], Any]


def coerce_trajectory(result: Any) -> Trajectory:
    """Accept a Trajectory, a plain answer string, or a dict shaped like Trajectory (or {"output": ...})."""
    if isinstance(result, Trajectory):
        return result
    if isinstance(result, dict):
        if "output" in result or "steps" in result or "status" in result:
            d = dict(result)
            if not isinstance(d.get("output", ""), str):
                d["output"] = json.dumps(d["output"], ensure_ascii=False, default=str)
            traj = Trajectory.from_dict(d)
        else:
            traj = Trajectory(output=json.dumps(result, ensure_ascii=False, default=str))
    elif result is None:
        traj = Trajectory(output="")
    else:
        traj = Trajectory(output=str(result))
    if not traj.steps and traj.output:
        traj.steps.append(Step("message", traj.output))
    return traj


class FunctionAdapter(AgentAdapter):
    """Wrap ``fn(request)``. Exceptions become ``status="error"``; the function should poll
    ``request.cancel`` itself if it runs long (in-process code cannot be killed)."""

    name = "function"

    def __init__(self, fn: AgentFn, **options: Any):
        super().__init__(**options)
        self.fn = fn

    def run(self, request: AgentRequest) -> Trajectory:
        t0 = time.time()
        cid = request.case.id
        if request.cancel.is_set():
            return Trajectory(case_id=cid, status="cancelled", error="cancelled")
        try:
            traj = coerce_trajectory(self.fn(request))
        except Exception as e:  # noqa: BLE001 - adapters never raise
            return Trajectory(
                case_id=cid, status="error", error=f"{type(e).__name__}: {e}", duration_s=time.time() - t0
            )
        traj.case_id = cid
        if not traj.duration_s:
            traj.duration_s = time.time() - t0
        if request.cancel.is_set() and traj.status == "ok" and not traj.output:
            traj.status, traj.error = "cancelled", "cancelled"
        return traj


def load_target(target: str) -> AgentFn:
    mod, sep, attr = target.partition(":")
    if not sep or not mod or not attr:
        raise ValueError(f"callable target must look like 'pkg.module:function', got {target!r}")
    obj: Any = importlib.import_module(mod)
    for part in attr.split("."):
        obj = getattr(obj, part)
    if not callable(obj):
        raise TypeError(f"{target!r} is not callable")
    return obj


@ADAPTERS.register("callable")
class CallableAdapter(FunctionAdapter):
    name = "callable"

    def __init__(self, target: str = "", **options: Any):
        self.target = target
        self._load_error = ""
        fn: AgentFn
        try:
            fn = load_target(target)
        except Exception as e:  # noqa: BLE001 - surface at run()/healthcheck() instead of at construction
            self._load_error = f"cannot load {target!r}: {type(e).__name__}: {e}"

            def fn(_request: AgentRequest) -> Any:
                raise RuntimeError(self._load_error)

        super().__init__(fn, **options)

    def healthcheck(self) -> tuple[bool, str]:
        return (False, self._load_error) if self._load_error else (True, f"loaded {self.target}")
