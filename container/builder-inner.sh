#!/usr/bin/env bash
# Runs INSIDE the builder container. Builds the rust-noasync toolchain for
# aarch64-unknown-linux-gnu and emits a zstd tarball to /out.
#   /hostsrc  : the fork repo, read-only bind mount
#   /cfg      : the container/ dir, read-only bind mount
#   /out      : toolchain/dist, writable bind mount
#   /build    : named volume (virtio-blk, fast) for the build tree
set -euxo pipefail
REV="${1:?usage: builder-inner.sh <git-rev>}"

git config --global --add safe.directory '*'   # virtiofs uid mismatch on /hostsrc

if [ ! -d /build/rust-noasync/.git ]; then
  git clone /hostsrc /build/rust-noasync        # clone the LOCAL repo (rev present, no network)
fi
cd /build/rust-noasync
git fetch origin --tags || true
git checkout -f "$REV"
git submodule update --init --recursive --depth=1 src/tools/cargo library/backtrace || true

cp /cfg/bootstrap-linux.toml bootstrap.toml

# stage2 rustc + std + rustdoc under the ban -> /opt/rust-noasync
./x install

# cargo can't be compiled by the fork (its source is async); build it at stage 1
# with the stock beta bootstrap compiler and copy it in.
./x build --stage 1 cargo
cp build/host/stage1-tools-bin/cargo /opt/rust-noasync/bin/cargo

{ git rev-parse HEAD; date -u; /opt/rust-noasync/bin/rustc -vV; } > /opt/rust-noasync/MANIFEST

bash /cfg/verify-toolchain.sh fork-only         # gate BEFORE tarballing (no stock stable here)

tar -C /opt -c rust-noasync | zstd -T0 -19 -o "/out/rust-noasync-$(git rev-parse --short HEAD)-aarch64-unknown-linux-gnu.tar.zst"
echo "builder-inner: done"
