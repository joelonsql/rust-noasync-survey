#!/usr/bin/env python3
"""Host-side orchestrator for the rust-noasync crates.io survey.

Drives long-lived Linux containers via `container exec`; the DB is the only
state. Usage:
  python3 orchestrator.py --fixtures fixtures.yaml   # validation mode (no DB queue)
  python3 orchestrator.py --limit 100                # run at most 100 probes then stop
  python3 orchestrator.py                            # run until both queues drain
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import classify
import config
import lifecycle
import runner
import db

STOP = threading.Event()


# ----------------------------------------------------------------- per-probe
def run_probe(container: str, spec: dict, spool: str,
              check_timeout: int = config.CHECK_TIMEOUT) -> dict:
    """Execute one crate probe inside `container`; return an outcome dict for
    db.record_result. Does not touch the DB itself."""
    name, version = spec["crate"], spec["version"]
    t0 = time.time()
    out: dict = {"probe_id": spec.get("probe_id"), "crate": name, "version": version,
                 "worker": container}

    # 1. fetch (network on, no foreign code) -- gated by the fetch semaphore
    with FETCH_SEM:
        fr = runner.exec_phase(container, ["/probe/probe.sh", "fetch", "--", name, version],
                               config.FETCH_TIMEOUT)
    fetch_env = _last_json(fr.stderr_text)
    if fr.timed_out:
        out.update(status="harness_error", first_error_rendered="fetch timed out"); return _t(out, t0)
    if not (fetch_env and fetch_env.get("ok")):
        transient = bool(fetch_env and fetch_env.get("transient"))
        if fetch_env and "resolve" in (fetch_env.get("err") or ""):
            out.update(status="excluded_resolve", first_error_rendered=fetch_env.get("err"))
        else:
            out.update(status="harness_error" if transient else "excluded_resolve",
                       first_error_rendered=(fetch_env or {}).get("err", "fetch failed"),
                       stderr_fork=fr.stderr_text[:4000])
        return _t(out, t0)

    # 2. check (network off, foreign code runs) -> JSON spool
    cr = runner.exec_phase(container, ["/probe/probe.sh", "check", "--", name, version],
                           check_timeout, spool_path=spool)
    scan = classify.scan_first_error(spool)
    no_tgt = _meta_no_targets(container)
    v = classify.classify_fork(exit_code=cr.exit_code, timed_out=cr.timed_out, oom=cr.oom,
                               scan=scan, stderr_text=cr.stderr_text, no_targets=no_tgt)
    out["stderr_fork"] = cr.stderr_text[:8000]
    out["error_json"] = None

    if v.status != "NEEDS_CONTROL":
        _apply(out, v)
        return _t(out, t0)

    # 3. control (stock stable, same source + lock) -- only for non-async fork failures
    kr = runner.exec_phase(container, ["/probe/probe.sh", "control", "--", name, version],
                           config.CONTROL_TIMEOUT, spool_path=spool + ".control")
    kscan = classify.scan_first_error(spool + ".control")
    final, cfirst = classify.classify_control(exit_code=kr.exit_code, timed_out=kr.timed_out,
                                              oom=kr.oom, scan=kscan, stderr_text=kr.stderr_text)
    out["status"] = final
    out["first_error_rendered"] = v.first_error_rendered
    out["error_code"] = v.error_code
    out["stderr_control"] = kr.stderr_text[:8000]
    out["control_first_error"] = cfirst
    out["notes"] = v.notes
    return _t(out, t0)


def _apply(out, v: classify.ForkVerdict):
    out["status"] = v.status
    out["async_construct"] = v.async_construct
    out["blamed_dep_name"] = v.blamed_dep_name
    out["blamed_dep_version"] = v.blamed_dep_version
    out["first_error_rendered"] = v.first_error_rendered
    out["error_code"] = v.error_code
    out["notes"] = v.notes


def _t(out, t0):
    out["wall_ms"] = int((time.time() - t0) * 1000)
    return out


def _last_json(text: str):
    import json
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


_META_CACHE: dict = {}
def _meta_no_targets(container: str) -> bool:
    import json
    rc, out = runner.exec_simple(container, ["cat", "/work/meta.json"], timeout=20)
    try:
        m = json.loads(out)
        pkgs = m.get("packages") or []
        if not pkgs:
            return False
        targets = pkgs[0].get("targets") or []
        kinds = {k for t in targets for k in (t.get("kind") or [])}
        return not ({"lib", "bin", "proc-macro", "cdylib", "staticlib", "rlib"} & kinds)
    except Exception:
        return False


def _no_targets():
    return False


# ----------------------------------------------------------------- workers
FETCH_SEM = threading.Semaphore(config.FETCH_CONCURRENCY)
INFLIGHT: dict[str, int] = {}
INFLIGHT_LOCK = threading.Lock()


def worker_loop(i: int, fork_tc: int, control_tc: int, limit_counter, force_queue=None):
    name = lifecycle.create_worker(i)
    if not lifecycle.netoff_ok(name):
        print(f"[w{i}] WARNING: net-off isolation not confirmed", file=sys.stderr)
    spool = os.path.join(config.SPOOL_DIR, f"w{i}.json")
    n = 0
    conn = db.connect()
    try:
        while not STOP.is_set():
            if limit_counter is not None and limit_counter.take() is False:
                break
            queue = force_queue or config.QUEUE_CYCLE[n % len(config.QUEUE_CYCLE)]
            spec = db.claim_probe(conn, name, queue)
            if spec is None:  # this queue drained; try the other, else idle
                other = "random" if queue == "popular" else "popular"
                spec = db.claim_probe(conn, name, other)
                if spec is None:
                    if STOP.wait(15):
                        break
                    continue
            with INFLIGHT_LOCK:
                INFLIGHT[name] = spec["probe_id"]
            try:
                outcome = run_probe(name, spec, spool)
                # resource cases get one retry on a big container
                if outcome["status"] == "excluded_resource" and "_retried" not in outcome:
                    big = lifecycle.recycle_worker(i, big=True)
                    outcome = run_probe(big, spec, spool, check_timeout=config.BIG_CHECK_TIMEOUT)
                    outcome["_retried"] = True
                    lifecycle.recycle_worker(i, big=False)
                db.record_result(conn, name, fork_tc, control_tc, outcome)
            except Exception as e:
                print(f"[w{i}] probe error: {e}", file=sys.stderr)
            finally:
                with INFLIGHT_LOCK:
                    INFLIGHT.pop(name, None)
            n += 1
            if n % config.GC_EVERY == 0:
                runner.exec_simple(name, ["/probe/probe.sh", "gc"], timeout=120)
            if n % config.RECYCLE_EVERY == 0:
                name = lifecycle.recycle_worker(i)
    finally:
        conn.close()
    print(f"[w{i}] done ({n} probes)")


# ----------------------------------------------------------------- maintenance threads
def heartbeat_loop():
    conn = db.connect()
    while not STOP.wait(config.HEARTBEAT_SEC):
        with INFLIGHT_LOCK:
            by_worker: dict[str, list[int]] = {}
            for w, pid in INFLIGHT.items():
                by_worker.setdefault(w, []).append(pid)
        for w, ids in by_worker.items():
            try:
                db.touch_probes(conn, w, ids)
            except Exception:
                pass
    conn.close()


def sweep_loop():
    conn = db.connect()
    while not STOP.wait(config.SWEEP_SEC):
        try:
            db.sweep_stale(conn, config.SWEEP_TIMEOUT)
        except Exception:
            pass
    conn.close()


class Limiter:
    def __init__(self, n): self.n, self.lock = n, threading.Lock()
    def take(self):
        with self.lock:
            if self.n <= 0: return False
            self.n -= 1; return True


# ----------------------------------------------------------------- toolchain registration
def register_toolchains(container: str):
    conn = db.connect()
    fvv, fcv = lifecycle.toolchain_identity(container, "/opt/rust-noasync/bin")
    svv, scv = lifecycle.toolchain_identity(container, "/opt/rust-stable/bin")
    fork = db.get_or_create_toolchain(conn, "fork", "noasync-" + _fork_rev(fvv), fvv, fcv,
                                      source_commit="4856e5741e4")
    ctl = db.get_or_create_toolchain(conn, "control", "stable-" + _stable_ver(svv), svv, scv)
    conn.close()
    return fork, ctl


def _fork_rev(vv):
    for ln in vv.splitlines():
        if ln.startswith("release:"):
            return ln.split(":", 1)[1].strip()
    return "unknown"


def _stable_ver(vv):
    for ln in vv.splitlines():
        if ln.startswith("release:"):
            return ln.split(":", 1)[1].strip()
    return "unknown"


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=config.WORKERS)
    ap.add_argument("--queue", choices=["popular", "random"],
                    help="force a single queue (default: 3 popular : 1 random)")
    ap.add_argument("--minutes", type=float,
                    help="stop cleanly after this many minutes (time-boxed chunk)")
    args = ap.parse_args()

    os.makedirs(config.SPOOL_DIR, exist_ok=True)
    lifecycle.system_start()

    def on_sig(*_):
        print("\nshutting down (finishing in-flight probes)…", file=sys.stderr)
        STOP.set()
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    if args.fixtures:
        import fixtures_run
        fixtures_run.run(args.fixtures)
        return

    # one bootstrap container to register toolchains, then tear it down
    boot = lifecycle.create_worker(99)
    fork_tc, control_tc = register_toolchains(boot)
    lifecycle.destroy_one(99)
    print(f"toolchains: fork={fork_tc} control={control_tc}")

    limiter = Limiter(args.limit) if args.limit else None
    if args.minutes:
        threading.Thread(target=lambda: STOP.wait(args.minutes * 60) or STOP.set(),
                         daemon=True).start()
        print(f"time-boxed: will stop after {args.minutes} min")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=sweep_loop, daemon=True).start()

    ts = []
    for i in range(args.workers):
        t = threading.Thread(target=worker_loop, args=(i, fork_tc, control_tc, limiter, args.queue))
        t.start(); ts.append(t)
    for t in ts:
        t.join()
    lifecycle.destroy_all(args.workers)
    print("orchestrator finished.")


if __name__ == "__main__":
    main()
