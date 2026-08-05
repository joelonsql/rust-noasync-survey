"""Classify a probe from `cargo check --message-format=json` output.

The first `level=="error"` compiler-message in stream order is the verdict.
Cargo builds dependencies before dependents, so a dependency's error naturally
precedes the crate's own -> blame ordering is correct by construction.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

BAN_MESSAGE = "async/await syntax is not supported by this toolchain"
LABEL_SUFFIX = " is not supported"
CONSTRUCTS = {
    "async function", "async closure", "async block",
    "`async gen` function", "`async gen` closure", "`async gen` block",
    "`.await` expression", "`for await` loop", "async trait bound",
}

# cargo/rustc non-JSON stderr signatures
_RESOLVE_SIGS = [
    "failed to select a version", "no matching package", "failed to parse manifest",
    "cyclic package dependency", "the lock file", "unable to get packages",
    "required by package", "which is neither", "failed to load source",
    "error: package `", "rust-version", "no matching version",
]
_BUILDSCRIPT_RE = re.compile(r"failed to run custom build command for `([^`]+?) v([^`]+?)`")


def parse_package_id(pid: str) -> tuple[str, str, str]:
    """(name, version, source) with source in {registry, path, git, other}."""
    if not pid:
        return ("", "", "other")
    src = "other"
    if pid.startswith("registry+"):
        src = "registry"
    elif pid.startswith("path+"):
        src = "path"
    elif pid.startswith("git+"):
        src = "git"
    # modern: "<src>+<url>#name@version" or "...#version"
    if "#" in pid and (pid.startswith(("registry+", "path+", "git+"))):
        url, frag = pid.rsplit("#", 1)
        if "@" in frag:
            name, ver = frag.rsplit("@", 1)
        else:
            ver = frag
            base = url.rstrip("/").rsplit("/", 1)[-1]  # e.g. foo-1.2.3
            name = base[: -(len(ver) + 1)] if base.endswith("-" + ver) else base
        return (name, ver, src)
    # legacy: "name version (registry+...)"
    m = re.match(r"^(\S+)\s+(\S+)\s+\((\w+)\+", pid)
    if m:
        return (m.group(1), m.group(2), m.group(3))
    return (pid, "", src)


@dataclass
class ScanResult:
    err_pkg: str | None = None
    err_msg: str | None = None
    err_code: str | None = None
    err_rendered: str | None = None
    err_label: str | None = None
    error_count: int = 0
    ban_error_count: int = 0
    build_success: bool | None = None


def scan_first_error(spool_path: str, max_rendered: int = 16384) -> ScanResult:
    """Streaming scan of a cargo JSON spool file (never loads it whole)."""
    r = ScanResult()
    try:
        fh = open(spool_path, "r", errors="replace")
    except OSError:
        return r
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            reason = msg.get("reason")
            if reason == "build-finished":
                r.build_success = bool(msg.get("success"))
                continue
            if reason != "compiler-message":
                continue
            m = msg.get("message") or {}
            level = m.get("level") or ""
            if not level.startswith("error"):
                continue
            r.error_count += 1
            text = m.get("message") or ""
            if text == BAN_MESSAGE:
                r.ban_error_count += 1
            if r.err_pkg is None:  # first error wins
                r.err_pkg = msg.get("package_id")
                r.err_msg = text
                code = m.get("code")
                r.err_code = code.get("code") if isinstance(code, dict) else None
                rendered = m.get("rendered") or ""
                r.err_rendered = rendered[:max_rendered]
                for sp in (m.get("spans") or []):
                    if sp.get("is_primary") and sp.get("label"):
                        r.err_label = sp["label"]
                        break
    return r


@dataclass
class ForkVerdict:
    status: str                 # final, or "NEEDS_CONTROL"
    async_construct: str | None = None
    blamed_dep_name: str | None = None
    blamed_dep_version: str | None = None
    first_error_rendered: str | None = None
    error_code: str | None = None
    error_count: int = 0
    ban_error_count: int = 0
    control_needed: bool = False
    notes: str | None = None


def _construct_from_label(label: str | None) -> str:
    if label and label.endswith(LABEL_SUFFIX):
        c = label[: -len(LABEL_SUFFIX)]
        return c if c in CONSTRUCTS else "unknown_async"
    return "unknown_async"


def classify_fork(*, exit_code: int, timed_out: bool, oom: bool,
                  scan: ScanResult, stderr_text: str, no_targets: bool) -> ForkVerdict:
    if timed_out or oom:
        return ForkVerdict(status="excluded_resource",
                           notes="timeout" if timed_out else "oom")
    if exit_code == 0 and scan.build_success is not False:
        return ForkVerdict(status="pass_trivial" if no_targets else "pass",
                           error_count=scan.error_count)

    if scan.err_pkg is not None and scan.err_msg == BAN_MESSAGE:
        name, ver, src = parse_package_id(scan.err_pkg)
        construct = _construct_from_label(scan.err_label)
        if src == "path":
            return ForkVerdict(status="fail_async_direct", async_construct=construct,
                               first_error_rendered=scan.err_rendered,
                               error_count=scan.error_count, ban_error_count=scan.ban_error_count)
        return ForkVerdict(status="fail_async_dep", async_construct=construct,
                           blamed_dep_name=name, blamed_dep_version=ver,
                           first_error_rendered=scan.err_rendered,
                           error_count=scan.error_count, ban_error_count=scan.ban_error_count)

    if scan.err_pkg is not None:
        # a real compiler error that is NOT ours -> disambiguate against stock
        return ForkVerdict(status="NEEDS_CONTROL", control_needed=True,
                           first_error_rendered=scan.err_rendered, error_code=scan.err_code,
                           error_count=scan.error_count, notes="non_ban_compiler_error")

    # no JSON compiler error but nonzero exit: inspect stderr
    bs = _BUILDSCRIPT_RE.search(stderr_text or "")
    if bs:
        return ForkVerdict(status="NEEDS_CONTROL", control_needed=True,
                           first_error_rendered=f"build-script failure: {bs.group(1)} v{bs.group(2)}",
                           notes="build_script_failure")
    low = (stderr_text or "").lower()
    if any(sig in low for sig in _RESOLVE_SIGS):
        return ForkVerdict(status="excluded_resolve",
                           first_error_rendered=(stderr_text or "")[:2000], notes="resolution")
    # unknown nonzero exit -> retryable harness error
    return ForkVerdict(status="harness_error",
                       first_error_rendered=(stderr_text or "")[:2000], notes="unknown_nonzero")


def classify_control(*, exit_code: int, timed_out: bool, oom: bool,
                     scan: ScanResult, stderr_text: str) -> tuple[str, str | None]:
    """Given the fork failed with a non-async error, judge the control run.
    Returns (final_status, control_first_error)."""
    if exit_code == 0 and scan.build_success is not False:
        return ("fail_other", None)   # fork-caused non-ban failure (canary)
    # control also failed -> exclude the crate from the survey
    ce = scan.err_rendered or (stderr_text or "")[:2000]
    return ("excluded_broken", ce)
