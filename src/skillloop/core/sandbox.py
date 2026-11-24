"""Path isolation: every file the framework touches on behalf of a project must stay inside it."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


class SandboxError(ValueError):
    """A path tried to escape its sandbox root."""


def ensure_within(root: str | Path, path: str | Path) -> Path:
    """Resolve ``path`` and require it to be ``root`` or below. Symlinks are resolved first."""
    root_r = Path(root).resolve()
    p = Path(path)
    p = (p if p.is_absolute() else root_r / p).resolve()
    if p != root_r and root_r not in p.parents:
        raise SandboxError(f"path {str(path)!r} escapes sandbox {str(root_r)!r}")
    return p


def safe_join(root: str | Path, *parts: str) -> Path:
    """Join untrusted relative ``parts`` under ``root``; absolute parts and ``..`` escapes are rejected."""
    for part in parts:
        if os.path.isabs(part):
            raise SandboxError(f"absolute path {part!r} not allowed")
    return ensure_within(root, Path(root).joinpath(*parts))


def safe_name(text: str, max_len: int = 80) -> str:
    """Turn arbitrary ids into a single safe path component."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("._") or "x"
    return s[:max_len]


class Sandbox:
    """A root directory plus helpers that refuse to leave it."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str) -> Path:
        return safe_join(self.root, *parts)

    def mkdir(self, *parts: str) -> Path:
        p = self.path(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write(self, rel: str, content: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def read(self, rel: str) -> str:
        return self.path(rel).read_text(encoding="utf-8")

    def rmtree(self, *parts: str) -> None:
        p = self.path(*parts)
        if p == self.root:
            raise SandboxError("refusing to delete the sandbox root")
        shutil.rmtree(p, ignore_errors=True)

    def contains(self, path: str | Path) -> bool:
        try:
            ensure_within(self.root, path)
            return True
        except SandboxError:
            return False
