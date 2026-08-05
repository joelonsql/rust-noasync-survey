"""Container lifecycle: start the daemon, create/recycle/destroy worker
containers, probe the net-off method, read toolchain identities."""
from __future__ import annotations

import json
import subprocess

import config


def _run(argv, timeout=120, check=False):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=check)


def system_start():
    _run(["container", "system", "start", "--enable-kernel-install"], timeout=180)


def worker_name(i: int) -> str:
    return f"noasync-w{i}"


def create_worker(i: int, big: bool = False) -> str:
    name = worker_name(i)
    _run(["container", "rm", "-f", name])  # idempotent
    vol = f"vol-work-w{i}"
    _run(["container", "volume", "create", vol])
    cpus = str(config.BIG_CPUS if big else config.WORKER_CPUS)
    mem = config.BIG_MEM if big else config.WORKER_MEM
    argv = ["container", "run", "-d", "--name", name,
            "--cpus", cpus, "--memory", mem, "--init",
            "--ulimit", "nofile=65536:65536"]
    for d in config.DNS:
        argv += ["--dns", d]
    argv += [
        "--mount", f"type=bind,source={config.PROBE_DIR},target=/probe,readonly",
        "-v", f"{config.CACHE_CRATES}:/cache/crates",   # shared .crate cache (append-only, safe)
        "-v", f"{vol}:/work",                            # per-worker: CARGO_HOME + target dirs live here
        config.IMAGE, "sleep", "infinity",
    ]
    _run(argv, timeout=180, check=True)
    return name


def recycle_worker(i: int, big: bool = False) -> str:
    name = worker_name(i)
    _run(["container", "stop", name], timeout=60)
    _run(["container", "rm", "-f", name])
    return create_worker(i, big=big)


def destroy_one(i: int):
    _run(["container", "stop", worker_name(i)], timeout=60)
    _run(["container", "rm", "-f", worker_name(i)])


def destroy_all(n: int):
    for i in range(n):
        destroy_one(i)


def netoff_ok(container: str) -> bool:
    """True if `unshare -Ur -n` blocks egress inside the container."""
    p = _run(["container", "exec", container, "/probe/probe.sh", "netoff-verify"], timeout=60)
    try:
        return bool(json.loads(p.stdout.strip().splitlines()[-1]).get("netoff"))
    except Exception:
        return False


def toolchain_identity(container: str, binpath: str) -> tuple[str, str]:
    """(rustc -vV, cargo --version) for a toolchain inside the container."""
    r1 = _run(["container", "exec", container, f"{binpath}/rustc", "-vV"], timeout=30)
    r2 = _run(["container", "exec", container, f"{binpath}/cargo", "--version"], timeout=30)
    return r1.stdout.strip(), r2.stdout.strip()
