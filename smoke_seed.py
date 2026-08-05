#!/usr/bin/env python3
"""Drive synthetic results through the real claim_probe/complete_probe functions
to exercise the dashboard (NOTIFY->SSE, every panel, Wilson CI). Resets after.

  python3 smoke_seed.py 300      # seed 300 probes then STOP (leaves data for viewing)
  python3 smoke_seed.py reset    # wipe results, reset probes to pending
"""
import random
import sys
import time

import psycopg

import db

# deterministic-ish weighted outcomes (not cryptographic; fixed seed)
random.seed(42)
CULPRITS = ["tokio", "hyper", "reqwest", "async-std", "futures-util", "axum"]
WEIGHTS = [
    ("pass", 0.62), ("pass_trivial", 0.05), ("fail_async_dep", 0.15),
    ("fail_async_direct", 0.05), ("fail_other", 0.01), ("excluded_broken", 0.04),
    ("excluded_resolve", 0.03), ("excluded_resource", 0.02), ("harness_error", 0.03),
]
STATUSES = [s for s, _ in WEIGHTS]
CUM = []
acc = 0.0
for _, w in WEIGHTS:
    acc += w
    CUM.append(acc)


def pick():
    r = random.random() * CUM[-1]
    for s, c in zip(STATUSES, CUM):
        if r <= c:
            return s
    return "pass"


def reset(conn):
    conn.execute("DELETE FROM noasync.probe_result")  # cascades diagnostics
    conn.execute("UPDATE noasync.probe SET state='pending', claimed_by=NULL,"
                 " claimed_at=NULL, claimed_via=NULL, attempts=0, finished_at=NULL")
    print("reset: results wiped, probes pending")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "300"
    with db.connect() as conn:
        if arg == "reset":
            reset(conn)
            return
        n = int(arg)
        fork = db.get_or_create_toolchain(conn, "fork", "noasync-smoke", "rustc 1.99.0 (smoke)")
        ctl = db.get_or_create_toolchain(conn, "control", "stable-smoke", "rustc 1.97.0 (smoke)")
        cycle = ["popular", "popular", "popular", "random"]
        for i in range(n):
            claim = db.claim_probe(conn, f"smoke{i % 6}", cycle[i % 4])
            if claim is None:
                print("queue drained"); break
            st = pick()
            oc = {"probe_id": claim["probe_id"], "crate": claim["crate"],
                  "version": claim["version"], "status": st, "wall_ms": random.randint(200, 90000)}
            if st == "fail_async_dep":
                oc["blamed_dep_name"] = random.choice(CULPRITS)
                oc["blamed_dep_version"] = "1.0.0"
                oc["async_construct"] = "async function"
                oc["first_error_rendered"] = f"error: async/await syntax is not supported (in {oc['blamed_dep_name']})"
            elif st == "fail_async_direct":
                oc["async_construct"] = random.choice(["async function", "`.await` expression", "async block"])
                oc["first_error_rendered"] = "error: async/await syntax is not supported by this toolchain"
            elif st in ("fail_other", "excluded_broken"):
                oc["first_error_rendered"] = "error[E0433]: failed to resolve: use of undeclared crate"
                oc["control_outcome"] = "fail" if st == "excluded_broken" else "pass"
            db.record_result(conn, f"smoke{i % 6}", fork, ctl, oc)
            if i % 20 == 0:
                time.sleep(0.05)  # let the dashboard breathe; also exercises the 4/s cap when faster
        print(f"seeded {i + 1} results. View http://127.0.0.1:8787 then `python3 smoke_seed.py reset`.")


if __name__ == "__main__":
    main()
