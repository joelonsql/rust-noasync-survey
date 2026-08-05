#!/usr/bin/env bash
# Resume the survey. Idempotent: brings the cluster and the container daemon
# back up (neither auto-starts after a reboot), prints progress, then runs the
# orchestrator. All extra args pass through, e.g.:
#   ./resume.sh                 # run until both queues drain
#   ./resume.sh --minutes 120   # process for 2 hours then stop cleanly
#   ./resume.sh --limit 5000    # process 5000 more probes then stop
set -euo pipefail
SURVEY="$(cd "$(dirname "$0")" && pwd)"
PGBIN=/Applications/Postgres.app/Contents/Versions/18/bin
PGDATA="$SURVEY/pgdata"

# 1. cluster up?
if ! "$PGBIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; then
  echo "starting PostgreSQL cluster (port 5433)…"
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$SURVEY/logs/pg.log" -w start
fi

# 2. container daemon up?
if ! container system status 2>/dev/null | grep -q running; then
  echo "starting container system…"
  container system start --enable-kernel-install
fi

# 3. requeue anything a previous crash left 'running' past its lease
"$PGBIN/psql" -h 127.0.0.1 -p 5433 -d rust_crates -tAc \
  "SELECT 'reclaimed '||noasync.sweep_stale('0 seconds')||' stale probes';"

# 4. progress snapshot
"$PGBIN/psql" -h 127.0.0.1 -p 5433 -d rust_crates -tAc "
  SELECT 'progress: '||count(*) FILTER (WHERE state='done')||' done, '
    ||count(*) FILTER (WHERE state='pending')||' pending, '
    ||count(*) FILTER (WHERE state='exhausted')||' exhausted ('
    ||round(100.0*count(*) FILTER (WHERE state IN ('done','exhausted'))/count(*),1)||'%)'
  FROM noasync.probe;"

echo "resuming orchestrator ($*)…"
cd "$SURVEY"
exec python3 orchestrator/orchestrator.py "$@"
