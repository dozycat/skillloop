"""Shared subprocess runner for CLI-based adapters: timeout, prompt cancellation (kills the
whole process group), live line streaming and bounded output capture."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_CAPTURE = 200_000  # chars kept per stream (head + tail)


@dataclass
class ProcResult:
    returncode: int
    stdout: str
    stderr: str
    status: str  # "ok" | "error" | "timeout" | "cancelled"
    duration_s: float = 0.0
    error: str = ""

    def __iter__(self):  # allows: rc, out, err, status = run_process(...)
        return iter((self.returncode, self.stdout, self.stderr, self.status))


def truncate(text: str, limit: int = MAX_CAPTURE) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... <{len(text) - limit} chars truncated> ...\n" + text[-half:]


class _Capture:
    """Keeps the head and a rolling tail of a stream so memory stays bounded."""

    def __init__(self, limit: int):
        self.half = max(1, limit // 2)
        self.head: list[str] = []
        self.head_len = 0
        self.tail: list[str] = []
        self.tail_len = 0
        self.dropped = 0

    def add(self, line: str) -> None:
        if self.head_len < self.half:
            self.head.append(line)
            self.head_len += len(line)
            return
        self.tail.append(line)
        self.tail_len += len(line)
        while self.tail_len > self.half and len(self.tail) > 1:
            gone = self.tail.pop(0)
            self.tail_len -= len(gone)
            self.dropped += len(gone)

    def text(self) -> str:
        mid = f"\n... <{self.dropped} chars truncated> ...\n" if self.dropped else ""
        return truncate("".join(self.head) + mid + "".join(self.tail), self.half * 2 + 200)


def _kill(proc: subprocess.Popen, grace_s: float = 2.0) -> None:
    def send(sig: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            else:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    send(signal.SIGTERM)
    try:
        proc.wait(grace_s)
    except subprocess.TimeoutExpired:
        send(getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            proc.wait(grace_s)
        except subprocess.TimeoutExpired:
            pass


def run_process(
    cmd: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    timeout_s: float | None = None,
    cancel: threading.Event | None = None,
    on_line: Callable[[str], None] | None = None,
    max_capture: int = MAX_CAPTURE,
) -> ProcResult:
    """Run ``cmd`` (argv list, no shell). ``env`` is merged over ``os.environ``. Never raises
    for process-level failures; a missing binary yields ``status="error"``, returncode 127."""
    t0 = time.time()
    full_env = {**os.environ, **{k: str(v) for k, v in (env or {}).items()}}
    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=full_env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name == "posix"),
        )
    except (OSError, ValueError) as e:
        return ProcResult(127, "", str(e), "error", time.time() - t0, f"cannot start {cmd[0]!r}: {e}")

    out, err = _Capture(max_capture), _Capture(max_capture)

    def pump(stream, cap: _Capture, cb: Callable[[str], None] | None) -> None:
        try:
            for line in stream:
                cap.add(line)
                if cb is not None:
                    try:
                        cb(line.rstrip("\n"))
                    except Exception:
                        pass
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out, on_line), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err, None), daemon=True),
    ]
    for t in threads:
        t.start()
    if stdin is not None:

        def feed() -> None:
            try:
                proc.stdin.write(stdin)  # type: ignore[union-attr]
                proc.stdin.close()  # type: ignore[union-attr]
            except (BrokenPipeError, OSError, ValueError):
                pass

        threading.Thread(target=feed, daemon=True).start()

    status = "ok"
    deadline = t0 + timeout_s if timeout_s and timeout_s > 0 else None
    while True:
        try:
            proc.wait(0.1)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            status = "cancelled"
        elif deadline is not None and time.time() > deadline:
            status = "timeout"
        if status != "ok":
            _kill(proc)
            break
    for t in threads:
        t.join(2.0)
    rc = proc.returncode if proc.returncode is not None else -9
    if status == "ok" and rc != 0:
        status = "error"
    stderr = err.text()
    error = ""
    if status == "timeout":
        error = f"timed out after {timeout_s:.0f}s"
    elif status == "cancelled":
        error = "cancelled"
    elif status == "error":
        error = f"exit code {rc}: {stderr.strip()[-500:]}".rstrip(": ")
    return ProcResult(rc, out.text(), stderr, status, time.time() - t0, error)
