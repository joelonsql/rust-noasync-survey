"""Database access for the rust-noasync crates.io survey.

The orchestrator codes against exactly two names here: `claim_probe()` and
`record_result(outcome)`. Everything maps onto the server-side functions in
sql/01_noasync_schema.sql. Connections are loopback-only to the dedicated
5433 cluster; credentials never enter the sandbox.
"""
from __future__ import annotations

import json
from typing import Any

import psycopg

DSN = "host=127.0.0.1 port=5433 dbname=rust_crates application_name=noasync"

# The unified status set (must match the CHECK in probe_result.status).
STATUSES = {
    "pass", "pass_trivial", "fail_async_direct", "fail_async_dep",
    "fail_other", "excluded_broken", "excluded_resolve",
    "excluded_resource", "harness_error",
}
# control ran iff status in this set (must match control_iff_nonasync_failure).
CONTROL_STATUSES = {"fail_other", "excluded_broken"}
ASYNC_STATUSES = {"fail_async_direct", "fail_async_dep"}


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=autocommit)


def claim_probe(conn: psycopg.Connection, worker: str, queue: str) -> dict[str, Any] | None:
    """Claim the next pending probe from `queue` ('popular'|'random'). None if drained."""
    row = conn.execute(
        "SELECT o_probe_id, o_crate_name, o_version_num, o_feature_config"
        " FROM noasync.claim_probe(%s, %s)", (worker, queue)
    ).fetchone()
    if row is None:
        return None
    return {"probe_id": row[0], "crate": row[1], "version": row[2], "feature_config": row[3]}


def touch_probes(conn: psycopg.Connection, worker: str, probe_ids: list[int]) -> int:
    if not probe_ids:
        return 0
    return conn.execute(
        "SELECT noasync.touch_probes(%s, %s)", (worker, probe_ids)
    ).fetchone()[0]


def sweep_stale(conn: psycopg.Connection, timeout: str = "15 minutes") -> int:
    return conn.execute("SELECT noasync.sweep_stale(%s::interval)", (timeout,)).fetchone()[0]


def record_result(conn: psycopg.Connection, worker: str, fork_tc: int,
                  control_tc: int | None, outcome: dict[str, Any]) -> int:
    """Map a classifier outcome dict onto noasync.complete_probe (one atomic txn).

    Required keys: probe_id, status. Optional: async_construct, blamed_dep_name/
    blamed_crate (+_version), error_code, wall_ms, first_error_rendered,
    error_json, stderr_fork, stderr_control.
    """
    status = outcome["status"]
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")

    # Blame column: the probed crate itself for direct, the dep for dep-failures.
    if status == "fail_async_direct":
        blamed = outcome.get("crate")
        blamed_ver = outcome.get("version")
    elif status == "fail_async_dep":
        blamed = outcome.get("blamed_dep_name")
        blamed_ver = outcome.get("blamed_dep_version")
    else:
        blamed = blamed_ver = None

    ctl = control_tc if status in CONTROL_STATUSES else None
    ej = outcome.get("error_json")
    row = conn.execute(
        "SELECT noasync.complete_probe(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (outcome["probe_id"], worker, fork_tc, ctl, status,
         outcome.get("async_construct"), blamed, blamed_ver,
         outcome.get("error_code"), outcome.get("wall_ms"),
         outcome.get("first_error_rendered"),
         json.dumps(ej) if ej is not None else None,
         outcome.get("stderr_fork"), outcome.get("stderr_control")),
    ).fetchone()
    return row[0]


def get_or_create_toolchain(conn: psycopg.Connection, kind: str, label: str,
                            rustc_vv: str, cargo_version: str | None = None,
                            source_commit: str | None = None) -> int:
    row = conn.execute("SELECT id FROM noasync.toolchain WHERE label=%s", (label,)).fetchone()
    if row:
        return row[0]
    return conn.execute(
        "INSERT INTO noasync.toolchain (kind,label,rustc_vv,cargo_version,source_commit)"
        " VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (kind, label, rustc_vv, cargo_version, source_commit),
    ).fetchone()[0]
