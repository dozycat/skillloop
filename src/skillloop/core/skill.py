"""The Skill bundle: a directory with a ``SKILL.md`` (YAML-ish frontmatter + markdown body)
and optional supporting text files (references/, scripts/, ...). This is the unit that evolves."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .sandbox import safe_join

SKILL_FILE = "SKILL.md"
_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".js", ".ts", ".csv", ".sql", ""}
MAX_FILE_BYTES = 512_000


def slugify(text: str, default: str = "skill") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64].strip("-")
    return s or default


def _parse_frontmatter(block: str) -> dict[str, str]:
    """Tiny frontmatter parser: ``key: value`` lines, quoted values, and ``>``/``|`` blocks.
    Enough for SKILL.md without requiring PyYAML."""
    out: dict[str, str] = {}
    key = None
    buf: list[str] = []
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            if key is not None:
                out[key] = " ".join(b.strip() for b in buf).strip()
            key, val = m.group(1), m.group(2).strip()
            buf = [] if val in (">", "|", ">-", "|-") else [val]
        elif key is not None:
            buf.append(line)
    if key is not None:
        out[key] = " ".join(b.strip() for b in buf).strip()
    for k, v in out.items():
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            out[k] = v[1:-1]
    return out


def _fm_value(v: str) -> str:
    v = " ".join(str(v).split())
    if re.search(r"[:#\[\]{}&*!|>'\"%@`]", v) or v != v.strip():
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


@dataclass
class Skill:
    name: str = "skill"
    description: str = ""
    body: str = ""
    files: dict[str, str] = field(default_factory=dict)  # supporting files, relpath -> text
    metadata: dict[str, str] = field(default_factory=dict)  # extra frontmatter keys

    # ---- construction ----
    @classmethod
    def from_markdown(cls, text: str, files: dict[str, str] | None = None) -> Skill:
        text = text.replace("\r\n", "\n")
        m = _FM.match(text)
        fm = _parse_frontmatter(m.group(1)) if m else {}
        body = text[m.end() :] if m else text
        name = fm.pop("name", "") or "skill"
        desc = fm.pop("description", "")
        return cls(name=name, description=desc, body=body.strip() + "\n", files=dict(files or {}), metadata=fm)

    @classmethod
    def load(cls, path: str | Path) -> Skill:
        """Load from a skill directory or a bare SKILL.md file."""
        p = Path(path)
        if p.is_file():
            return cls.from_markdown(p.read_text(encoding="utf-8"))
        md = p / SKILL_FILE
        if not md.is_file():
            raise FileNotFoundError(f"no {SKILL_FILE} in {p}")
        files: dict[str, str] = {}
        for f in sorted(p.rglob("*")):
            if not f.is_file() or f.name == SKILL_FILE or any(part.startswith(".") for part in f.relative_to(p).parts):
                continue
            if f.suffix.lower() not in _TEXT_SUFFIXES or f.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                files[f.relative_to(p).as_posix()] = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return cls.from_markdown(md.read_text(encoding="utf-8"), files)

    # ---- output ----
    def to_markdown(self) -> str:
        fm = [f"name: {_fm_value(self.name)}", f"description: {_fm_value(self.description)}"]
        fm += [f"{k}: {_fm_value(v)}" for k, v in self.metadata.items()]
        return "---\n" + "\n".join(fm) + "\n---\n\n" + self.body.strip() + "\n"

    def save(self, directory: str | Path) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        (d / SKILL_FILE).write_text(self.to_markdown(), encoding="utf-8")
        for rel, content in self.files.items():
            target = safe_join(d, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return d

    def render_for_prompt(self) -> str:
        """Whole skill as one text blob, for agents with no native skill mechanism."""
        parts = [f"# Skill: {self.name}", self.description, "", self.body.strip()]
        for rel, content in sorted(self.files.items()):
            parts += ["", f"## File: {rel}", "```", content.rstrip(), "```"]
        return "\n".join(parts).strip() + "\n"

    def all_files(self) -> dict[str, str]:
        return {SKILL_FILE: self.to_markdown(), **self.files}

    def copy(self) -> Skill:
        return Skill(self.name, self.description, self.body, dict(self.files), dict(self.metadata))

    # ---- identity / checks ----
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for rel, content in sorted(self.all_files().items()):
            h.update(rel.encode() + b"\0" + content.encode() + b"\0")
        return h.hexdigest()[:16]

    def size(self) -> int:
        return sum(len(c) for c in self.all_files().values())

    def validate(self, max_chars: int = 0) -> list[str]:
        """Return a list of problems (empty == valid)."""
        problems = []
        if not _NAME.match(self.name):
            problems.append(f"name {self.name!r} must be lowercase letters/digits/hyphens (max 64 chars)")
        if not self.description.strip():
            problems.append("description is empty (it tells the agent when to use the skill)")
        elif len(self.description) > 1024:
            problems.append("description longer than 1024 chars")
        if len(self.body.strip()) < 20:
            problems.append("body is empty or trivially short")
        if max_chars and self.size() > max_chars:
            problems.append(f"skill is {self.size()} chars, over the {max_chars} limit")
        for rel in self.files:
            if rel.startswith("/") or ".." in Path(rel).parts:
                problems.append(f"unsafe file path {rel!r}")
        return problems

    def diff(self, other: Skill, a_label: str = "a", b_label: str = "b") -> str:
        """Unified diff from self -> other across all files."""
        mine, theirs = self.all_files(), other.all_files()
        chunks = []
        for rel in sorted(set(mine) | set(theirs)):
            a, b = mine.get(rel, ""), theirs.get(rel, "")
            if a == b:
                continue
            chunks.append(
                "".join(
                    difflib.unified_diff(
                        a.splitlines(keepends=True),
                        b.splitlines(keepends=True),
                        f"{a_label}/{rel}",
                        f"{b_label}/{rel}",
                    )
                )
            )
        return "\n".join(chunks)
