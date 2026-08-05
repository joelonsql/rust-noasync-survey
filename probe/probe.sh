#!/usr/bin/env bash
# In-container probe driver. One invocation per phase. Stdout of `check`/`control`
# is pure cargo JSON (streamed to a host spool); human output goes to stderr.
#
#   probe.sh fetch   -- <name> <version>
#   probe.sh check   -- <name> <version>
#   probe.sh control -- <name> <version>
#   probe.sh clean   -- <name> <version>
#   probe.sh netoff-verify
#   probe.sh gc
set -uo pipefail

CACHE=/cache/crates
# Per-worker CARGO_HOME on the /work volume: flock over virtiofs does NOT
# serialize across VMs, so a shared CARGO_HOME would race. /work is a
# per-worker volume (fast virtio-blk) that persists across container recycles,
# so each worker's registry cache still warms over its lifetime.
CARGO_HOME_SHARED="${CARGO_HOME_OVERRIDE:-/work/cargo-home}"
FORK_BIN=/opt/rust-noasync/bin
STOCK_BIN=/opt/rust-stable/bin
FETCH_TIMEOUT=300
CHECK_TIMEOUT=900
GC_LIMIT_KB=$((40*1024*1024))   # 40 GiB
UA='rust-noasync-survey/0.1 (mailto:reg@compiler.org)'

phase="${1:-}"; shift || true
[ "${1:-}" = "--" ] && shift || true
name="${1:-}"; version="${2:-}"

# crate names: [A-Za-z0-9_-]; versions may carry semver build metadata (`+`).
valid() { [[ "$1" =~ ^[A-Za-z0-9_.+-]{1,80}$ ]]; }

srcdir() { echo "/work/src/${name}-${version}"; }

do_fetch() {
  valid "$name" && valid "$version" || { echo '{"phase":"fetch","ok":false,"err":"bad name/version"}'; exit 2; }
  local shard="${name:0:2}"; local dir="$CACHE/$shard/$name"; local f="$dir/${name}-${version}.crate"
  mkdir -p "$dir" "$CARGO_HOME_SHARED"
  if [ ! -s "$f" ]; then
    local tmp; tmp="$(mktemp "$dir/.dl.XXXXXX")"
    if ! curl -sSfL --retry 3 --max-time "$FETCH_TIMEOUT" -A "$UA" \
         "https://static.crates.io/crates/${name}/${name}-${version}.crate" -o "$tmp"; then
      rm -f "$tmp"; echo '{"phase":"fetch","ok":false,"err":"download failed","transient":true}'; exit 3
    fi
    gzip -t "$tmp" 2>/dev/null || { rm -f "$tmp"; echo '{"phase":"fetch","ok":false,"err":"corrupt crate"}'; exit 3; }
    mv -f "$tmp" "$f"
  fi
  rm -rf /work/src && mkdir -p /work/src
  tar -xzf "$f" -C /work/src || { echo '{"phase":"fetch","ok":false,"err":"untar failed"}'; exit 3; }
  local sd; sd="$(srcdir)"
  [ -d "$sd" ] || sd="$(find /work/src -maxdepth 1 -mindepth 1 -type d | head -1)"
  local had_lock=false
  [ -f "$sd/Cargo.lock" ] && { had_lock=true; rm -f "$sd/Cargo.lock"; }
  # resolve + download deps (no foreign code executes during fetch)
  if ! ( cd "$sd" && timeout -k 15 "$FETCH_TIMEOUT" env CARGO_HOME="$CARGO_HOME_SHARED" \
         CARGO_NET_RETRY=3 "$FORK_BIN/cargo" fetch 1>&2 ); then
    echo "{\"phase\":\"fetch\",\"ok\":false,\"err\":\"resolve/fetch failed\",\"dir\":\"$sd\",\"had_lockfile\":$had_lock}"; exit 4
  fi
  ( cd "$sd" && env CARGO_HOME="$CARGO_HOME_SHARED" "$FORK_BIN/cargo" metadata --no-deps \
    --offline --format-version 1 > /work/meta.json 2>/dev/null ) || echo '{}' > /work/meta.json
  echo "{\"phase\":\"fetch\",\"ok\":true,\"dir\":\"$sd\",\"had_lockfile\":$had_lock}"
}

# run cargo check with networking removed; $1=bin dir, $2=target dir
run_check() {
  local bindir="$1" target="$2" sd; sd="$(srcdir)"
  [ -d "$sd" ] || sd="$(find /work/src -maxdepth 1 -mindepth 1 -type d | head -1)"
  [ -d "$sd" ] || { echo "no source dir" 1>&2; return 90; }
  cd "$sd" || return 90
  timeout -k 15 "$CHECK_TIMEOUT" unshare -Ur -n -- /bin/sh -c '
    ip link set lo up 2>/dev/null || true
    exec env -i PATH="'"$bindir"':/usr/local/bin:/usr/bin:/bin" HOME=/root \
      CARGO_HOME="'"$CARGO_HOME_SHARED"'" CARGO_TARGET_DIR="'"$target"'" \
      CARGO_INCREMENTAL=0 CARGO_NET_OFFLINE=true CARGO_TERM_COLOR=never \
      "'"$bindir"'/cargo" check --message-format=json --offline --locked
  '
}

do_check()   { run_check "$FORK_BIN"  /work/target;         exit $?; }
do_control() { run_check "$STOCK_BIN" /work/target-control; exit $?; }

do_clean() { rm -rf /work/src /work/target /work/target-control /work/meta.json 2>/dev/null; echo cleaned; }

do_netoff_verify() {
  if unshare -Ur -n -- /bin/sh -c 'curl -sf --max-time 4 https://static.crates.io/ >/dev/null 2>&1'; then
    echo '{"netoff":false,"method":"unshare -Ur -n","note":"network REACHABLE inside netns - isolation FAILED"}'; exit 1
  fi
  echo '{"netoff":true,"method":"unshare -Ur -n"}'
}

do_gc() {
  local kb; kb="$(du -sk /work/target /work/target-control 2>/dev/null | awk '{s+=$1} END{print s+0}')"
  if [ "${kb:-0}" -gt "$GC_LIMIT_KB" ]; then rm -rf /work/target /work/target-control; echo "gc: wiped ($kb KB)"; else echo "gc: ok ($kb KB)"; fi
}

case "$phase" in
  fetch)          do_fetch ;;
  check)          do_check ;;
  control)        do_control ;;
  clean)          do_clean ;;
  netoff-verify)  do_netoff_verify ;;
  gc)             do_gc ;;
  *) echo "usage: probe.sh {fetch|check|control|clean|netoff-verify|gc} -- <name> <version>" 1>&2; exit 64 ;;
esac
