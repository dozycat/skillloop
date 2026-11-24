"""Skill patches: the unit analysts propose, the merger consolidates and the builder applies.

A patch is a list of small, declarative edits. Applying is deterministic and guarded:
edits that reference missing text/files are *rejected individually* (and reported), never guessed.

Edit ops (``file`` defaults to SKILL.md; for SKILL.md, edits act on the markdown body):
  {"op": "append",  "section": "## Rules", "content": "..."}   # under heading (created if absent); no section -> end
  {"op": "replace", "old": "exact text", "new": "..."}
  {"op": "delete",  "old": "exact text"}
  {"op": "write_file", "file": "references/x.md", "content": "..."}
  {"op": "delete_file", "file": "references/x.md"}
  {"op": "set_description", "content": "..."}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..core.models import Serializable
from ..core.skill import SKILL_FILE, Skill

OPS = ("append", "replace", "delete", "write_file", "delete_file", "set_description")

PATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "rationale": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPS)},
                    "file": {"type": "string"},
                    "section": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["op"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "edits"],
    "additionalProperties": False,
}


@dataclass
class Patch(Serializable):
    summary: str = ""
    rationale: str = ""
    edits: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # case ids supporting the patch
    source: str = ""  # "error" | "success" | "merge" | "manual"
    support: int = 1  # how many independent analyses proposed (roughly) this

    @classmethod
    def coerce(cls, data: Any, source: str = "") -> Patch:
        """Build from untrusted model output, dropping malformed edits."""
        if not isinstance(data, dict):
            return cls(source=source)
        edits = [e for e in data.get("edits") or [] if isinstance(e, dict) and e.get("op") in OPS]
        return cls(
            summary=str(data.get("summary") or ""),
            rationale=str(data.get("rationale") or ""),
            edits=edits,
            evidence=[str(x) for x in data.get("evidence") or []],
            source=source or str(data.get("source") or ""),
            support=int(data.get("support") or 1),
        )

    def is_empty(self) -> bool:
        return not self.edits


@dataclass
class ApplyResult:
    skill: Skill
    applied: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)  # each: {"edit": ..., "reason": ...}

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def _safe_rel(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and not path.startswith(".")


def _append_under(body: str, section: str, content: str) -> str:
    content = content.strip("\n")
    if not section.strip():
        return body.rstrip("\n") + "\n\n" + content + "\n"
    title = section.strip().lstrip("#").strip()
    lines = body.split("\n")
    heading = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
    start = level = None
    for i, line in enumerate(lines):
        m = heading.match(line)
        if m and m.group(2).strip().lower() == title.lower():
            start, level = i, len(m.group(1))
            break
    if start is None:
        hashes = re.match(r"^#+", section.strip())
        head = f"{hashes.group(0) if hashes else '##'} {title}"
        return body.rstrip("\n") + f"\n\n{head}\n\n{content}\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = heading.match(lines[j])
        if m and len(m.group(1)) <= level:  # type: ignore[operator]
            end = j
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    new = lines[:end] + content.split("\n") + ([""] if end < len(lines) else []) + lines[end:]
    return "\n".join(new).rstrip("\n") + "\n"


def apply_patch(skill: Skill, patch: Patch) -> ApplyResult:
    """Apply ``patch`` to a copy of ``skill``. Never raises on bad edits; see ``ApplyResult.rejected``."""
    out = skill.copy()
    res = ApplyResult(out)

    def reject(edit: dict[str, Any], reason: str) -> None:
        res.rejected.append({"edit": edit, "reason": reason})

    for edit in patch.edits:
        op = edit.get("op")
        file = str(edit.get("file") or SKILL_FILE)
        is_main = file == SKILL_FILE
        if not is_main and not _safe_rel(file):
            reject(edit, f"unsafe file path {file!r}")
            continue
        text = out.body if is_main else out.files.get(file)

        if op == "set_description":
            content = " ".join(str(edit.get("content") or "").split())
            if not content:
                reject(edit, "empty description")
                continue
            out.description = content[:1024]
        elif op == "write_file":
            if is_main:
                reject(edit, "write_file cannot target SKILL.md (use append/replace)")
                continue
            out.files[file] = str(edit.get("content") or "")
        elif op == "delete_file":
            if is_main or file not in out.files:
                reject(edit, f"file {file!r} does not exist")
                continue
            del out.files[file]
        elif op == "append":
            content = str(edit.get("content") or "")
            if not content.strip():
                reject(edit, "empty content")
                continue
            if text is None:
                reject(edit, f"file {file!r} does not exist")
                continue
            if content.strip() in text:
                reject(edit, "content already present")
                continue
            new = _append_under(text, str(edit.get("section") or ""), content)
            if is_main:
                out.body = new
            else:
                out.files[file] = new
        elif op in ("replace", "delete"):
            old = str(edit.get("old") or "")
            if text is None:
                reject(edit, f"file {file!r} does not exist")
                continue
            if not old.strip():
                reject(edit, "empty 'old' text")
                continue
            count = text.count(old)
            if count == 0:
                reject(edit, "'old' text not found")
                continue
            if count > 1:
                reject(edit, f"'old' text is ambiguous ({count} matches)")
                continue
            new = text.replace(old, str(edit.get("new") or "") if op == "replace" else "", 1)
            new = re.sub(r"\n{3,}", "\n\n", new)
            if is_main:
                out.body = new
            else:
                out.files[file] = new
        else:
            reject(edit, f"unknown op {op!r}")
            continue
        res.applied.append(edit)
    return res
