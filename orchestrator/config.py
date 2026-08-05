"""Orchestrator configuration for the rust-noasync crates.io survey."""
SURVEY = "/Users/joel/rust-noasync-survey"
IMAGE = "noasync-probe:v1"

# worker pool
WORKERS = 6
WORKER_CPUS = 4
WORKER_MEM = "12g"
BIG_CPUS = 8            # lazy retry slot for timeout/OOM cases
BIG_MEM = "32g"

# crates.io politeness: cap concurrent fetch phases
FETCH_CONCURRENCY = 3

# phase timeouts (seconds); client waits phase_timeout + GRACE before force-kill
FETCH_TIMEOUT = 300
CHECK_TIMEOUT = 900
CONTROL_TIMEOUT = 900
BIG_CHECK_TIMEOUT = 1800
CLIENT_GRACE = 90

# lifecycle
RECYCLE_EVERY = 250        # probes per container before recycle (ballooning caveat)
RECYCLE_HOURS = 6
GC_EVERY = 25              # probes between target-dir GC checks
HEARTBEAT_SEC = 60         # lease renewal for in-flight claims
SWEEP_SEC = 60
SWEEP_TIMEOUT = "15 minutes"

# queue interleave: 3 popular : 1 random
QUEUE_CYCLE = ["popular", "popular", "popular", "random"]

# container DNS (gateway resolver is flaky for some hosts; use public DNS)
DNS = ["8.8.8.8", "1.1.1.1"]

# host paths bind-mounted into every worker
CACHE_CRATES = f"{SURVEY}/cache/crates"
CACHE_CARGO = f"{SURVEY}/cache/cargo-home"
PROBE_DIR = f"{SURVEY}/probe"
SPOOL_DIR = f"{SURVEY}/logs/spool"
ARCHIVE_DIR = f"{SURVEY}/logs/archive"

# capped stderr capture per phase
STDERR_CAP = 65536
