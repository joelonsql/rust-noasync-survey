#!/usr/bin/env bash
# Hard gate: proves the built fork toolchain bans async and keeps the sync
# Future surface. `fork-only` skips stock-stable checks (used in the builder
# container, which has no /opt/rust-stable). Full mode is the probe-image gate.
# NOTE: no `pipefail` + no `| grep` on chatty commands (avoids SIGPIPE/141);
# greps use here-strings instead.
set -eu
MODE="${1:-full}"
F=/opt/rust-noasync/bin/rustc
S=/opt/rust-stable/bin/rustc
d=$(mktemp -d)
trap 'rm -rf "$d"' EXIT

# 1) async fn -> exact ban error, nonzero exit
printf 'async fn f() {}\n' > "$d/a.rs"
if $F --edition 2021 --crate-type lib "$d/a.rs" -o "$d/a.rlib" 2>"$d/err"; then
  echo "FAIL: fork accepted async fn"; cat "$d/err"; exit 1
fi
grep -qF 'error: async/await syntax is not supported by this toolchain' "$d/err"
grep -qF 'async function is not supported' "$d/err"

# 2) async block + closure labels
printf 'fn g() { let _ = async {}; let _ = async || {}; }\n' > "$d/b.rs"
$F --edition 2021 --crate-type lib "$d/b.rs" 2>"$d/err2" || true
grep -qF 'async block is not supported' "$d/err2"

# 3) sync code + manual impl Future (std's sync Future/task API intact) -> PASS
cat > "$d/c.rs" <<'EOF'
use std::{future::Future, pin::Pin, task::{Context, Poll}};
pub struct S;
impl Future for S { type Output = u8;
  fn poll(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<u8> { Poll::Ready(7) } }
EOF
$F --edition 2021 --crate-type lib "$d/c.rs" -o "$d/c.rlib"

# 4) stable channel: #![feature] must be rejected
printf '#![feature(never_type)]\n' > "$d/f.rs"
if $F --crate-type lib "$d/f.rs" -o "$d/f.rlib" 2>/dev/null; then
  echo "FAIL: #![feature] not locked (channel != stable)"; exit 1
fi

# 5) identities (here-strings, no pipe -> no SIGPIPE)
vv="$($F -vV)"
grep -qF 'host: aarch64-unknown-linux-gnu' <<<"$vv"
ver="$($F --version)"
grep -qF '1.99.0' <<<"$ver"
/opt/rust-noasync/bin/cargo --version >/dev/null

if [ "$MODE" != "fork-only" ]; then
  # stock control accepts async, and both cargos work
  $S --edition 2021 --crate-type lib "$d/a.rs" -o "$d/a_stock.rlib"
  /opt/rust-stable/bin/cargo --version >/dev/null
  ( cd "$d" && /opt/rust-noasync/bin/cargo new hello >/dev/null 2>&1 \
      && cd hello && /opt/rust-noasync/bin/cargo check -q )
fi
echo "verify-toolchain ($MODE): OK"
