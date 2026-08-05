#!/usr/bin/env python3
"""Fixture validation: run a hand-picked set of crates with known expected
outcome classes through the real probe pipeline (no DB writes). Mismatches are
flagged for inspection, not treated as hard failures (some are genuinely
'investigate and reclassify')."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config
import db
import orchestrator

# crate -> expected outcome class
FIXTURES = {
    # sync crates -> pass
    "itoa": "pass", "serde": "pass", "serde_json": "pass", "log": "pass",
    "anyhow": "pass", "libc": "pass", "bytes": "pass", "once_cell": "pass",
    # proc-macro that only *generates* async -> its own source is sync -> pass
    "async-trait": "pass",
    # direct async syntax in the crate's own code -> fail_async_direct
    "tokio": "fail_async_direct",
    # futures-util's default-feature code is poll-based combinators; its async
    # syntax lives only in macro_rules! bodies never expanded when compiling
    # the library itself (same as std's join!). So the library survives.
    "futures-util": "pass",
    # a dependency's async fails first (deps build before dependents):
    "hyper": "fail_async_dep",        # tokio errors before hyper's own code
    "async-std": "fail_async_dep",    # futures-lite errors first
    "postgres": "fail_async_dep",     # tokio-postgres/tokio
    # system lib not installed -> fails both toolchains -> excluded_broken
    "gpgme": "excluded_broken",
}


def main():
    container = sys.argv[1] if len(sys.argv) > 1 else "noasync-smoke"
    spool = "/tmp/fixture.json"
    conn = db.connect()
    rows = []
    for crate, expect in FIXTURES.items():
        ver = conn.execute(
            "SELECT version_num FROM noasync.probe WHERE crate_name=%s", (crate,)
        ).fetchone()
        if not ver:
            rows.append((crate, "?", expect, "NO_VERSION", "not in probe table")); continue
        spec = {"probe_id": None, "crate": crate, "version": ver[0]}
        try:
            oc = orchestrator.run_probe(container, spec, spool)
        except Exception as e:
            rows.append((crate, ver[0], expect, "ERROR", str(e)[:60])); continue
        got = oc["status"]
        detail = oc.get("blamed_dep_name") or oc.get("async_construct") or \
            (oc.get("first_error_rendered") or "")[:50] or oc.get("notes") or ""
        mark = "OK " if got == expect else "!! "
        rows.append((crate, ver[0], expect, got, detail, mark))
        print(f"{mark}{crate:16} {ver[0]:12} expect={expect:18} got={got:18} {detail}")
        sys.stdout.flush()
    conn.close()

    ok = sum(1 for r in rows if len(r) > 5 and r[5] == "OK ")
    print(f"\n{ok}/{len(FIXTURES)} matched expected class")
    mism = [r for r in rows if len(r) <= 5 or r[5] != "OK "]
    if mism:
        print("to inspect:")
        for r in mism:
            print("  ", r[0], "->", r[3], "|", r[4] if len(r) > 4 else "")


if __name__ == "__main__":
    main()
