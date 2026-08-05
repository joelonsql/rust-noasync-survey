# rust-noasync crates.io survival survey

Measures **how much of crates.io survives [rust-noasync](https://github.com/joelonsql/rust-noasync)** —
the Rust fork where async/await is a hard compile error. Each crate is
build-probed with the fork's toolchain (default features, the version cargo
would pick); it either passes or the *first* failure is recorded and
classified: async syntax in the crate's own code, async syntax in a dependency
(with the culprit named), or unrelated breakage (excluded from the denominator
after confirming it also fails on stock stable Rust).

Results live in a dedicated PostgreSQL cluster; a live web dashboard updates
after every crate.

## Results so far

_Snapshot at 3,867 / 303,131 crates probed (1.3%). Numbers move as the run
progresses; the dashboard is live._

![dashboard](docs/dashboard.png)

- **Projected whole-registry survival: ~62.5%** (95% CI 59.2–65.7%), from the
  uniform random sample (n=846). This is the unbiased estimate for *all* of
  crates.io.
- **Survival climbs steeply with popularity**: top 100 → **96.0%**, top 1k →
  **89.2%**, top 10k → **86.8%**. The most-used crates are far more likely to
  survive than the long tail.
- **Outcome mix** of the 3,867 probed: 75.9% pass, 16.0% fail on an async
  dependency, 1.5% fail on their own async, 5.4% excluded (also broken on stock
  stable), 1.0% unresolvable. The `fail_other` canary (fork fails where stock
  passes) sits at **2** — essentially zero, so the fork isn't breaking sync code.
- **Blame concentrates on two crates**: **`tokio`** (290 crates, almost all
  transitively) and — notably — **`cc`** (144 crates, *all* transitively).
  Recent `cc` versions use an `async` block in their parallel command runner,
  so many `-sys` crates that compile C code fail through it. Then `futures-lite`
  (35), `glib` (26), and a long tail.

## Architecture

- **Host**: a dedicated, disposable PostgreSQL 18 cluster (port 5433) holding
  the crates.io dump + our `noasync` results schema, plus a stdlib Python
  dashboard. The orchestrator runs here and talks to the DB over loopback.
- **Sandbox**: Apple `container` (one lightweight Linux VM per worker). The
  orchestrator drives long-lived containers via `container exec`. Untrusted
  `build.rs`/proc-macro code only ever runs inside a VM, and during `cargo
  check` the network is removed (`unshare -Ur -n`) so foreign code can't phone
  home. DB credentials never enter the sandbox.
- **Toolchains** (baked into `noasync-probe:v1`): the fork built for
  aarch64-linux (`channel=stable`), and stock stable Rust as the control.

```
db.py                 DSN + claim_probe()/record_result() the orchestrator uses
dashboard.py          single-file http.server + SSE + LISTEN/NOTIFY
import_dump.sh        (documented below) fetch+load the nightly dump
sql/01_noasync_schema.sql   results schema + claim/complete/sweep/touch functions
sql/02_populate_probes.sql  build the work queue from the dump
verify_claims.py      concurrency test for the claim/sweep logic
smoke_seed.py         synthetic results to exercise the dashboard
container/            Containerfile, apt list, Linux bootstrap config, build+verify scripts
probe/probe.sh        in-container: fetch | check | control | clean | netoff-verify | gc
orchestrator/         config, runner (container exec), lifecycle, classify, orchestrator, fixtures
```

## One-time setup

```sh
PGBIN=/Applications/Postgres.app/Contents/Versions/18/bin
SURVEY="$HOME/rust-noasync-survey"   # or wherever you cloned this repo

# 1. dedicated cluster (port 5433, own data dir, tuned for bulk load)
"$PGBIN/initdb" -D "$SURVEY/pgdata" --encoding=UTF8 --locale=C -U "$(whoami)"
#   append port=5433, listen_addresses='127.0.0.1', shared_buffers=8GB,
#   maintenance_work_mem=8GB, work_mem=256MB to pgdata/postgresql.conf
#   (fsync=off during import; ALTER SYSTEM SET fsync=on afterwards)
"$PGBIN/pg_ctl" -D "$SURVEY/pgdata" -l "$SURVEY/logs/pg.log" -w start

# 2. dump: download, extract, drop version_downloads, load schema+data
cd "$SURVEY/dump"
curl -fL -o db-dump.tar.gz https://static.crates.io/db-dump.tar.gz
tar -xzf db-dump.tar.gz --strip-components 1 -C current
sed -i '' '/version_downloads/d' current/import.sql   # exclude the 90-day history table
#   comment out CREATE/COMMENT EXTENSION crunchy_pooler,pgaudit in current/schema.sql
"$PGBIN/createdb" -h 127.0.0.1 -p 5433 rust_crates
"$PGBIN/psql" -h 127.0.0.1 -p 5433 -d rust_crates -f current/schema.sql
cd current && "$PGBIN/psql" -h 127.0.0.1 -p 5433 -d rust_crates -f import.sql

# 3. results schema + work queue
"$PGBIN/psql" -h 127.0.0.1 -p 5433 -d rust_crates -f "$SURVEY/sql/01_noasync_schema.sql"
"$PGBIN/psql" -h 127.0.0.1 -p 5433 -d rust_crates -f "$SURVEY/sql/02_populate_probes.sql"

# 4. containers: build the base + probe images (fork toolchain baked in)
container system start --enable-kernel-install
cd "$SURVEY/container"
container build -t noasync-base --target base -f Containerfile ctx
#   build the Linux fork toolchain once (see container/builder-inner.sh), then:
container build -t noasync-probe:v1 --target probe -f Containerfile ctx
```

## Running

```sh
cd "$HOME/rust-noasync-survey"                    # or your clone path
ulimit -n 4096 && python3 dashboard.py &          # http://127.0.0.1:8787

# full run (both queues, 6 workers, until drained):
python3 orchestrator/orchestrator.py

# useful variants:
python3 orchestrator/orchestrator.py --limit 100 --queue popular   # top-N milestone
python3 orchestrator/orchestrator.py --fixtures orchestrator/fixtures_run.py  # validation set
```

The DB is the only state: kill the orchestrator any time (SIGINT finishes
in-flight probes), rerun to resume. `sweep_stale` reclaims a dead worker's
probes after 15 min; the orchestrator heartbeats live claims every 60s.
Estimated full run: ~2 weeks (~900 probes/hr steady) for 303,131 probes.

## Notes / caveats

- **cargo is beta-built**: the fork can't compile cargo's own async source, so
  the survey's fork cargo is the stock-beta-built binary (behaviorally
  identical — it just invokes the fork rustc). Resolution outcomes are
  therefore not fork-influenced.
- Probing is **default features only** (v1). The schema's `feature_config`
  column is ready for per-feature probes later.
- **Excluded** = not counted against the fork: fails the fork with a non-async
  error *and* fails stock stable too (missing system lib, nightly-only, MSRV).
  `fail_other` (fork fails where stock passes) is the canary for fork bugs and
  should stay ≈ 0.
- Container DNS: the vmnet gateway resolver is flaky for some hosts, so workers
  use `--dns 8.8.8.8`.
- The cluster is disposable — everything is re-creatable from the nightly dump.
