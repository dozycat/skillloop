"""Dataset IO (测试集 / 标准答案) and deterministic train/val/test splitting."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .models import Case

_INPUT_KEYS = ("input", "question", "prompt", "query", "task", "instruction")
_EXPECTED_KEYS = ("expected", "answer", "ground_truth", "gold", "output", "label", "reference")
_KNOWN = {"id", "rubric", "tags", "split", "files", "meta", "weight", "judge", *_INPUT_KEYS, *_EXPECTED_KEYS}


def case_from_record(rec: dict[str, Any], index: int = 0) -> Case:
    """Build a Case from a loosely-shaped record (common field aliases are accepted)."""
    inp = next((rec[k] for k in _INPUT_KEYS if rec.get(k) not in (None, "")), None)
    if inp is None:
        raise ValueError(f"record {index} has no input field (one of {', '.join(_INPUT_KEYS)})")
    expected = next((rec[k] for k in _EXPECTED_KEYS if k in rec and rec[k] not in (None, "")), None)
    tags = rec.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.replace(";", ",").split(",") if t.strip()]
    meta = dict(rec.get("meta") or {})
    meta.update({k: v for k, v in rec.items() if k not in _KNOWN})
    cid = str(rec.get("id") or "") or "case_" + hashlib.sha1(str(inp).encode()).hexdigest()[:8]
    return Case(
        id=cid,
        input=str(inp),
        expected=expected,
        rubric=str(rec.get("rubric") or ""),
        tags=list(tags),
        split=str(rec.get("split") or ""),
        files=dict(rec.get("files") or {}),
        meta=meta,
        weight=float(rec.get("weight") or 1.0),
        judge=str(rec.get("judge") or ""),
    )


def load_cases(path: str | Path) -> list[Case]:
    """Load cases from .jsonl, .json (list or {"cases": [...]}) or .csv."""
    p = Path(path)
    if not p.exists():
        return []
    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        records = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif suffix == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        records = data.get("cases", []) if isinstance(data, dict) else data
    elif suffix in (".csv", ".tsv"):
        with p.open(newline="", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f, delimiter="\t" if suffix == ".tsv" else ","))
    else:
        raise ValueError(f"unsupported dataset format: {p.name} (use .jsonl, .json, .csv)")
    cases = [case_from_record(r, i) for i, r in enumerate(records)]
    seen: dict[str, int] = {}
    for c in cases:  # make ids unique
        n = seen.get(c.id, 0)
        seen[c.id] = n + 1
        if n:
            c.id = f"{c.id}_{n}"
    return cases


def save_cases(cases: Iterable[Case], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for c in cases:
            d = {k: v for k, v in c.to_dict().items() if v not in ("", None, [], {}) or k in ("id", "input")}
            if d.get("weight") == 1.0:
                d.pop("weight")
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return p


def split_cases(
    cases: list[Case],
    train_ratio: float = 0.5,
    val_ratio: float = 0.3,
    seed: int = 7,
) -> dict[str, list[Case]]:
    """Deterministic, tag-stratified split. Cases with a preset ``split`` keep it; assignment
    is by a stable hash of the case id, so adding cases never reshuffles existing ones'
    relative order. Small datasets still get at least one train and one val case."""
    out: dict[str, list[Case]] = {"train": [], "val": [], "test": []}
    groups: dict[str, list[Case]] = {}
    for c in cases:
        if c.split in out:
            out[c.split].append(c)
        else:
            groups.setdefault(c.tags[0] if c.tags else "", []).append(c)
    for _, group in sorted(groups.items()):
        group = sorted(group, key=lambda c: hashlib.sha1(f"{seed}:{c.id}".encode()).hexdigest())
        n = len(group)
        n_train = max(1, round(n * train_ratio)) if n else 0
        n_val = round(n * val_ratio)
        if n >= 2 and n_val == 0 and val_ratio > 0:
            n_val = 1
        n_train = min(n_train, n - n_val) if n > 1 else n_train
        out["train"] += group[:n_train]
        out["val"] += group[n_train : n_train + n_val]
        out["test"] += group[n_train + n_val :]
    if not out["val"] and len(out["train"]) > 1:  # stratification starved val: borrow from train
        out["val"].append(out["train"].pop())
    return out
