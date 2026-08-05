"""Safe `container exec` wrapper: argv lists only (no shell), stdout streamed to
a spool file (never through Python memory), stderr captured and capped, hard
timeout with force-kill."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass

import config


@dataclass
class ExecResult:
    exit_code: int
    timed_out: bool
    oom: bool
    stderr_text: str
    spool_path: str | None


def _drain(stream, cap, sink):
    total = 0
    for chunk in iter(lambda: stream.read(8192), b""):
        if total < cap:
            sink.append(chunk[: cap - total])
            total += len(chunk)
    stream.close()


def exec_phase(container: str, argv: list[str], timeout: int,
               spool_path: str | None = None) -> ExecResult:
    """Run `container exec <container> <argv...>`. If spool_path is given, stdout
    is written there (truncated first); otherwise stdout joins stderr capture."""
    cmd = ["container", "exec", container, *argv]
    sink: list[bytes] = []
    out_fh = open(spool_path, "wb") if spool_path else None
    try:
        p = subprocess.Popen(
            cmd,
            stdout=(out_fh if out_fh else subprocess.PIPE),
            stderr=subprocess.PIPE,
        )
        threads = [threading.Thread(target=_drain, args=(p.stderr, config.STDERR_CAP, sink), daemon=True)]
        if out_fh is None:
            threads.append(threading.Thread(target=_drain, args=(p.stdout, config.STDERR_CAP, sink), daemon=True))
        for t in threads:
            t.start()
        timed_out = False
        try:
            p.wait(timeout=timeout + config.CLIENT_GRACE)
        except subprocess.TimeoutExpired:
            timed_out = True
            # kill in-VM process group best-effort, then the exec client
            subprocess.run(["container", "exec", container, "pkill", "-9", "-f", "cargo|rustc|probe.sh"],
                           timeout=10, capture_output=True)
            p.kill()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for t in threads:
            t.join(timeout=5)
        rc = p.returncode if p.returncode is not None else -9
        stderr_text = b"".join(sink).decode("utf-8", "replace")
        oom = rc == 137 or "out of memory" in stderr_text.lower() or "oom" in stderr_text.lower()
        return ExecResult(rc, timed_out, oom, stderr_text, spool_path)
    finally:
        if out_fh:
            out_fh.close()


def exec_simple(container: str, argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """Small helper for control/status commands; returns (rc, combined_output)."""
    p = subprocess.run(["container", "exec", container, *argv],
                       capture_output=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
