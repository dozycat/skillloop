"""版本管理: immutable skill snapshots with manifests, lineage, and a local git history.

Layout (``.skillloop/versions`` is its own git repo, so the user's repo is never touched)::

    versions/
      v001/manifest.json  v001/skill/SKILL.md ...
      v002/...
      best/SKILL.md ...          # copy of the promoted version; `git log -- best` is the promotion history
      BEST_VERSION               # id of the promoted version
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from .models import SkillVersion
from .sandbox import safe_join
from .skill import Skill

_VID = re.compile(r"^v\d{3,}$")


class VersionStore:
    def __init__(self, root: str | Path, use_git: bool = True):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.git_enabled = use_git and shutil.which("git") is not None
        if self.git_enabled and not (self.root / ".git").exists():
            if self._git("init", "-q").returncode != 0:
                self.git_enabled = False

    # ---------------------------------------------------------------- git
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=skillloop",
                "-c",
                "user.email=skillloop@localhost",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _commit(self, message: str, tag: str = "") -> None:
        if not self.git_enabled:
            return
        try:
            self._git("add", "-A")
            self._git("commit", "-q", "--allow-empty", "-m", message)
            if tag:
                self._git("tag", "-f", tag)
        except (OSError, subprocess.SubprocessError):
            pass  # history is best-effort; snapshots on disk remain the source of truth

    def git_log(self, limit: int = 50) -> list[dict[str, str]]:
        if not self.git_enabled:
            return []
        try:
            r = self._git("log", f"-{limit}", "--pretty=format:%h%x1f%ad%x1f%s", "--date=iso")
        except (OSError, subprocess.SubprocessError):
            return []
        out = []
        for line in r.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                out.append({"commit": parts[0], "date": parts[1], "message": parts[2]})
        return out

    # ---------------------------------------------------------------- paths
    def _dir(self, vid: str) -> Path:
        if not _VID.match(vid):
            raise KeyError(f"invalid version id {vid!r}")
        return safe_join(self.root, vid)

    def skill_dir(self, vid: str) -> Path:
        return self._dir(vid) / "skill"

    def ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and _VID.match(p.name))

    def _next_id(self) -> str:
        ids = self.ids()
        return f"v{(int(ids[-1][1:]) + 1 if ids else 1):03d}"

    # ---------------------------------------------------------------- CRUD
    def create(
        self,
        skill: Skill,
        *,
        parent: str = "",
        origin: str = "manual",
        summary: str = "",
        iteration: int = 0,
        status: str = "candidate",
        patches: list[dict[str, Any]] | None = None,
        tags: list[str] | None = None,
    ) -> SkillVersion:
        with self._lock:
            vid = self._next_id()
            d = self._dir(vid)
            skill.save(d / "skill")
            version = SkillVersion(
                id=vid,
                parent=parent,
                origin=origin,
                status=status,
                summary=summary,
                iteration=iteration,
                content_hash=skill.content_hash(),
                patches=patches or [],
                tags=tags or [],
            )
            self._write_manifest(version)
            self._commit(
                f"{vid}: {origin}" + (f" from {parent}" if parent else "") + (f" - {summary[:80]}" if summary else ""),
                tag=vid,
            )
            return version

    def _write_manifest(self, version: SkillVersion) -> None:
        (self._dir(version.id) / "manifest.json").write_text(
            json.dumps(version.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, vid: str) -> SkillVersion:
        p = self._dir(vid) / "manifest.json"
        if not p.exists():
            raise KeyError(f"unknown version {vid!r}")
        return SkillVersion.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def exists(self, vid: str) -> bool:
        try:
            return (self._dir(vid) / "manifest.json").exists()
        except KeyError:
            return False

    def load_skill(self, vid: str) -> Skill:
        return Skill.load(self.skill_dir(vid))

    def list(self) -> list[SkillVersion]:
        return [self.get(v) for v in self.ids()]

    def update(self, version: SkillVersion, commit_message: str = "") -> SkillVersion:
        """Persist manifest changes (scores, status, gate decision)."""
        with self._lock:
            self._write_manifest(version)
            if commit_message:
                self._commit(f"{version.id}: {commit_message}")
            return version

    def record_score(self, vid: str, split: str, summary: dict[str, Any]) -> SkillVersion:
        with self._lock:
            v = self.get(vid)
            v.scores[split] = summary
            return self.update(v)

    def find_by_hash(self, content_hash: str) -> SkillVersion | None:
        for v in self.list():
            if v.content_hash == content_hash:
                return v
        return None

    # ---------------------------------------------------------------- best / lineage
    def best_id(self) -> str:
        p = self.root / "BEST_VERSION"
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""

    def best(self) -> SkillVersion | None:
        vid = self.best_id()
        return self.get(vid) if vid and self.exists(vid) else None

    def set_best(self, vid: str, reason: str = "") -> SkillVersion:
        """Promote ``vid`` (提升最佳版本). The previous best is demoted to 'accepted'."""
        with self._lock:
            version = self.get(vid)
            prev = self.best()
            if prev and prev.id != vid:
                prev.status = "accepted"
                self._write_manifest(prev)
            version.status = "best"
            self._write_manifest(version)
            best_dir = self.root / "best"
            shutil.rmtree(best_dir, ignore_errors=True)
            self.load_skill(vid).save(best_dir)
            (self.root / "BEST_VERSION").write_text(vid + "\n", encoding="utf-8")
            self._commit(f"promote {vid} to best" + (f": {reason[:120]}" if reason else ""), tag="best")
            return version

    def lineage(self, vid: str) -> list[SkillVersion]:
        """Ancestors from the root down to ``vid``."""
        chain, seen = [], set()
        while vid and vid not in seen and self.exists(vid):
            seen.add(vid)
            v = self.get(vid)
            chain.append(v)
            vid = v.parent
        return chain[::-1]

    def children(self, vid: str) -> list[SkillVersion]:
        return [v for v in self.list() if v.parent == vid]

    def diff(self, a: str, b: str) -> str:
        return self.load_skill(a).diff(self.load_skill(b), a, b)

    def export(self, vid: str, dest: str | Path) -> Path:
        return self.load_skill(vid).save(dest)
