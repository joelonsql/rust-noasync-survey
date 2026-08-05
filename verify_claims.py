#!/usr/bin/env python3
"""Concurrency test for the claim/sweep logic. Run after population, before real
workers. Claims only (no completes); resets the queue at the end."""
import sys
import threading

import psycopg

DSN = "host=127.0.0.1 port=5433 dbname=rust_crates"
THREADS, PER = 8, 250
QCYCLE = ["popular", "popular", "popular", "random"]


def worker(name, out, lock):
    got = []
    with psycopg.connect(DSN, autocommit=True) as conn:
        for i in range(PER):
            q = QCYCLE[i % 4]
            row = conn.execute(
                "SELECT o_probe_id FROM noasync.claim_probe(%s,%s)", (name, q)
            ).fetchone()
            if row:
                got.append(row[0])
    with lock:
        out.extend(got)


def main():
    fail = 0
    with psycopg.connect(DSN, autocommit=True) as c:
        pending0 = c.execute("SELECT count(*) FROM noasync.probe WHERE state='pending'").fetchone()[0]

    out, lock, ts = [], threading.Lock(), []
    for t in range(THREADS):
        th = threading.Thread(target=worker, args=(f"vt{t}", out, lock))
        th.start(); ts.append(th)
    for th in ts:
        th.join()

    # (1) no duplicate ids across all threads
    if len(out) != len(set(out)):
        print(f"FAIL: {len(out)-len(set(out))} duplicate claims"); fail += 1
    else:
        print(f"OK: {len(out)} claims, all distinct")

    with psycopg.connect(DSN, autocommit=True) as c:
        running = c.execute("SELECT count(*) FROM noasync.probe WHERE state='running'").fetchone()[0]
        # (2) running == total claims
        if running != len(out):
            print(f"FAIL: running={running} != claims={len(out)}"); fail += 1
        else:
            print(f"OK: running={running} matches claims")
        # (3) every claim has attempts==1
        bad = c.execute("SELECT count(*) FROM noasync.probe WHERE state='running' AND attempts<>1").fetchone()[0]
        print(("OK" if bad == 0 else "FAIL") + f": attempts<>1 rows = {bad}"); fail += (bad != 0)
        # (4) popular-claimed set is a prefix of pop_rank order (lowest ranks taken)
        maxpop = c.execute("SELECT max(pop_rank) FROM noasync.probe WHERE state='running' AND claimed_via='popular'").fetchone()[0]
        npop = c.execute("SELECT count(*) FROM noasync.probe WHERE state='running' AND claimed_via='popular'").fetchone()[0]
        gaps = c.execute("SELECT count(*) FROM noasync.probe WHERE pop_rank<=%s AND state='pending'", (maxpop,)).fetchone()[0]
        print(("OK" if gaps == 0 else f"FAIL: {gaps} lower-rank probes left pending") + f" (popular claimed {npop}, max_rank {maxpop})")
        fail += (gaps != 0)

        # sweeper: age the claims, sweep, expect all back to pending with attempts preserved
        c.execute("UPDATE noasync.probe SET claimed_at = now() - interval '2 hours' WHERE state='running'")
        swept = c.execute("SELECT noasync.sweep_stale('15 minutes')").fetchone()[0]
        still = c.execute("SELECT count(*) FROM noasync.probe WHERE state='running'").fetchone()[0]
        att = c.execute("SELECT count(*) FROM noasync.probe WHERE attempts=1 AND state='pending'").fetchone()[0]
        print(("OK" if still == 0 else "FAIL") + f": swept={swept}, still running={still}, attempts-preserved={att}")
        fail += (still != 0)

        # reset the queue to pristine
        c.execute("UPDATE noasync.probe SET state='pending', claimed_by=NULL, claimed_at=NULL, claimed_via=NULL, attempts=0")
        pending1 = c.execute("SELECT count(*) FROM noasync.probe WHERE state='pending'").fetchone()[0]
        print(("OK" if pending1 == pending0 else "FAIL") + f": reset pending {pending1} (was {pending0})")
        fail += (pending1 != pending0)

    print("\nRESULT:", "ALL PASS" if fail == 0 else f"{fail} FAILURES")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
