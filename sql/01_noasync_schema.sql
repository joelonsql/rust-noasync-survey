-- rust-noasync crates.io survey: results schema.
--   psql -h 127.0.0.1 -p 5433 -d rust_crates -v ON_ERROR_STOP=1 -f 01_noasync_schema.sql
-- Lives alongside the crates.io dump in `public`; references it by FK.
BEGIN;

CREATE SCHEMA IF NOT EXISTS noasync;

------------------------------------------------------------------ provenance
CREATE TABLE noasync.import_meta (
    id               smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dump_timestamp   timestamptz NOT NULL UNIQUE,   -- metadata.json .timestamp
    crates_io_commit text        NOT NULL,          -- metadata.json .crates_io_commit
    tarball_sha256   text,
    dump_dir         text,                           -- 'YYYY-MM-DD-HHMMSS'
    imported_at      timestamptz NOT NULL DEFAULT now(),
    notes            text
);

------------------------------------------------------------------ toolchains
CREATE TABLE noasync.toolchain (
    id            smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          text NOT NULL CHECK (kind IN ('fork', 'control')),
    label         text NOT NULL UNIQUE,     -- 'noasync-4856e5741e4', 'stable-1.97.0'
    rustc_vv      text NOT NULL,            -- full `rustc -vV`
    cargo_version text,
    source_commit text,                     -- fork: SHA in /Users/joel/src/rust-noasync
    created_at    timestamptz NOT NULL DEFAULT now()
);

------------------------------------------------------------------ work queue
CREATE TABLE noasync.probe (
    id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    crate_id       integer NOT NULL,
    version_id     integer NOT NULL,
    crate_name     text    NOT NULL,        -- denormalized: survives a dump refresh
    version_num    text    NOT NULL,
    feature_config text    NOT NULL DEFAULT 'default',   -- v1 always 'default'
    downloads      bigint  NOT NULL DEFAULT 0,
    pop_rank       integer NOT NULL,                     -- 1 = most downloaded
    rand_key       double precision NOT NULL,            -- uniform draw; random-queue order + estimator
    state          text    NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','running','done','exhausted')),
    claimed_via    text CHECK (claimed_via IN ('popular','random')),
    claimed_by     text,
    claimed_at     timestamptz,
    attempts       integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts   integer NOT NULL DEFAULT 3,
    finished_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT probe_unique_target UNIQUE (crate_id, version_id, feature_config),
    CONSTRAINT probe_running_has_claim CHECK (state <> 'running' OR claimed_at IS NOT NULL),
    CONSTRAINT fk_probe_crate   FOREIGN KEY (crate_id)   REFERENCES public.crates(id),
    CONSTRAINT fk_probe_version FOREIGN KEY (version_id) REFERENCES public.versions(id)
);
CREATE INDEX probe_pending_pop_idx  ON noasync.probe (pop_rank)   WHERE state = 'pending';
CREATE INDEX probe_pending_rand_idx ON noasync.probe (rand_key)   WHERE state = 'pending';
CREATE INDEX probe_running_idx      ON noasync.probe (claimed_at) WHERE state = 'running';
CREATE INDEX probe_pop_rank_idx     ON noasync.probe (pop_rank);
CREATE INDEX probe_finished_idx     ON noasync.probe (finished_at) WHERE finished_at IS NOT NULL;
CREATE INDEX probe_crate_name_idx   ON noasync.probe (crate_name);

------------------------------------------------------------------ results (append-only history)
CREATE TABLE noasync.probe_result (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    probe_id             integer  NOT NULL REFERENCES noasync.probe(id),
    fork_toolchain_id    smallint NOT NULL REFERENCES noasync.toolchain(id),
    control_toolchain_id smallint          REFERENCES noasync.toolchain(id),
    -- unified status set (reconciled across both design agents):
    status               text NOT NULL CHECK (status IN (
                           'pass','pass_trivial','fail_async_direct','fail_async_dep',
                           'fail_other','excluded_broken','excluded_resolve',
                           'excluded_resource','harness_error')),
    async_construct      text,             -- e.g. 'async function', '`.await` expression'
    blamed_crate_name    text,             -- direct: the probed crate; dep: the async dependency
    blamed_version_num   text,
    error_code           text,
    wall_ms              integer,
    is_current           boolean NOT NULL DEFAULT true,
    created_at           timestamptz NOT NULL DEFAULT now(),
    -- blame present iff the failure was async-classified
    CONSTRAINT blame_iff_async CHECK (
        (status IN ('fail_async_direct','fail_async_dep')) = (blamed_crate_name IS NOT NULL)),
    -- the control toolchain ran iff we needed to disambiguate a non-async fork failure
    CONSTRAINT control_iff_nonasync_failure CHECK (
        (control_toolchain_id IS NOT NULL) = (status IN ('fail_other','excluded_broken')))
);
CREATE UNIQUE INDEX probe_result_current_uq ON noasync.probe_result (probe_id) WHERE is_current;
CREATE INDEX probe_result_status_idx ON noasync.probe_result (status) WHERE is_current;
CREATE INDEX probe_result_blame_idx  ON noasync.probe_result (blamed_crate_name)
    WHERE is_current AND blamed_crate_name IS NOT NULL;
CREATE INDEX probe_result_recent_idx ON noasync.probe_result (created_at DESC);

------------------------------------------------------------------ heavy text, off the hot path
CREATE TABLE noasync.probe_diagnostics (
    result_id      bigint PRIMARY KEY REFERENCES noasync.probe_result(id) ON DELETE CASCADE,
    first_error    text,     -- rendered first error (dashboard ticker)
    error_json     jsonb,    -- structured diagnostic
    stderr_fork    text,
    stderr_control text
);

------------------------------------------------------------------ the one definition of denominator + survived
CREATE VIEW noasync.current_results AS
SELECT p.id AS probe_id, p.crate_id, p.crate_name, p.version_num, p.feature_config,
       p.downloads, p.pop_rank, p.rand_key, p.claimed_via, p.claimed_by, p.finished_at,
       r.id AS result_id, r.status, r.async_construct, r.blamed_crate_name, r.blamed_version_num,
       r.error_code, r.fork_toolchain_id, r.wall_ms,
       (r.status IN ('pass','pass_trivial','fail_async_direct','fail_async_dep','fail_other'))
         AS in_denominator,
       (r.status IN ('pass','pass_trivial')) AS survived
FROM noasync.probe p
JOIN noasync.probe_result r ON r.probe_id = p.id AND r.is_current;

------------------------------------------------------------------ worker-facing functions
-- Claim the next pending probe from one queue; zero rows when that queue is drained.
CREATE FUNCTION noasync.claim_probe(p_worker text, p_queue text)
RETURNS TABLE (o_probe_id integer, o_crate_name text, o_version_num text, o_feature_config text)
LANGUAGE plpgsql AS $$
DECLARE v_id integer;
BEGIN
    IF p_queue = 'popular' THEN
        SELECT id INTO v_id FROM noasync.probe
        WHERE state = 'pending' ORDER BY pop_rank LIMIT 1 FOR UPDATE SKIP LOCKED;
    ELSIF p_queue = 'random' THEN
        SELECT id INTO v_id FROM noasync.probe
        WHERE state = 'pending' ORDER BY rand_key LIMIT 1 FOR UPDATE SKIP LOCKED;
    ELSE
        RAISE EXCEPTION 'unknown queue: %', p_queue;
    END IF;
    IF v_id IS NULL THEN RETURN; END IF;

    UPDATE noasync.probe p
       SET state = 'running', claimed_via = p_queue, claimed_by = p_worker,
           claimed_at = now(), attempts = attempts + 1
     WHERE p.id = v_id
    RETURNING p.id, p.crate_name, p.version_num, p.feature_config
         INTO o_probe_id, o_crate_name, o_version_num, o_feature_config;
    RETURN NEXT;
END $$;

-- Heartbeat: renew the lease on in-flight claims so a slow (but alive) worker
-- is not swept. Orchestrator calls this every ~60s for its running probes.
CREATE FUNCTION noasync.touch_probes(p_worker text, p_ids integer[])
RETURNS integer LANGUAGE sql AS $$
    WITH t AS (
        UPDATE noasync.probe SET claimed_at = now()
        WHERE state = 'running' AND claimed_by = p_worker AND id = ANY(p_ids)
        RETURNING 1)
    SELECT count(*)::integer FROM t;
$$;

-- One call = one atomic transaction: result + diagnostics + probe state + NOTIFY.
CREATE FUNCTION noasync.complete_probe(
    p_probe_id integer, p_worker text,
    p_fork_tc smallint, p_control_tc smallint,
    p_status text, p_async_construct text,
    p_blamed_crate text, p_blamed_version text,
    p_error_code text, p_wall_ms integer,
    p_first_error text, p_error_json jsonb, p_stderr_fork text, p_stderr_control text
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE v_result_id bigint; v_probe noasync.probe; v_state text;
BEGIN
    SELECT * INTO v_probe FROM noasync.probe WHERE id = p_probe_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'no probe %', p_probe_id; END IF;
    IF v_probe.state <> 'running' OR v_probe.claimed_by IS DISTINCT FROM p_worker THEN
        RAISE EXCEPTION 'probe % not running for % (state=%, claimed_by=%)',
                        p_probe_id, p_worker, v_probe.state, v_probe.claimed_by;
    END IF;

    UPDATE noasync.probe_result SET is_current = false
     WHERE probe_id = p_probe_id AND is_current;

    INSERT INTO noasync.probe_result
        (probe_id, fork_toolchain_id, control_toolchain_id, status, async_construct,
         blamed_crate_name, blamed_version_num, error_code, wall_ms)
    VALUES (p_probe_id, p_fork_tc, p_control_tc, p_status, p_async_construct,
            p_blamed_crate, p_blamed_version, p_error_code, p_wall_ms)
    RETURNING id INTO v_result_id;

    INSERT INTO noasync.probe_diagnostics
        (result_id, first_error, error_json, stderr_fork, stderr_control)
    VALUES (v_result_id, p_first_error, p_error_json, p_stderr_fork, p_stderr_control);

    v_state := CASE
        WHEN p_status = 'harness_error' AND v_probe.attempts < v_probe.max_attempts THEN 'pending'
        WHEN p_status = 'harness_error'                                             THEN 'exhausted'
        ELSE 'done' END;
    UPDATE noasync.probe
       SET state = v_state, claimed_by = NULL, claimed_at = NULL,
           finished_at = CASE WHEN v_state IN ('done','exhausted') THEN now() END
     WHERE id = p_probe_id;

    PERFORM pg_notify('noasync_progress', json_build_object(
        'probe_id', p_probe_id, 'crate', v_probe.crate_name,
        'status', p_status, 'queue', v_probe.claimed_via)::text);
    RETURN v_result_id;
END $$;

-- Reclaim probes whose lease has expired (dead worker). Threshold must exceed
-- the orchestrator's heartbeat interval (~60s) with margin; 15 min is used.
CREATE FUNCTION noasync.sweep_stale(p_timeout interval DEFAULT '15 minutes')
RETURNS integer LANGUAGE plpgsql AS $$
DECLARE n integer;
BEGIN
    WITH stale AS (
        SELECT id FROM noasync.probe
        WHERE state = 'running' AND claimed_at < now() - p_timeout
        FOR UPDATE SKIP LOCKED
    )
    UPDATE noasync.probe p
       SET state = CASE WHEN p.attempts >= p.max_attempts THEN 'exhausted' ELSE 'pending' END,
           claimed_by = NULL, claimed_at = NULL
      FROM stale WHERE p.id = stale.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n > 0 THEN
        PERFORM pg_notify('noasync_progress', json_build_object('swept', n)::text);
    END IF;
    RETURN n;
END $$;

COMMIT;
