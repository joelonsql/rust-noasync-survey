-- Populate the work queue: one probe per crate = its default (cargo-picked) version.
--   psql -h 127.0.0.1 -p 5433 -d rust_crates -v ON_ERROR_STOP=1 -f 02_populate_probes.sql
BEGIN;
SELECT setseed(0.20260805);   -- fixed seed => reproducible random-queue order / estimator sample

INSERT INTO noasync.probe
    (crate_id, version_id, crate_name, version_num, feature_config,
     downloads, pop_rank, rand_key)
SELECT c.id, dv.version_id, c.name, v.num, 'default',
       COALESCE(cd.downloads, 0),
       row_number() OVER (ORDER BY COALESCE(cd.downloads, 0) DESC, c.id),
       random()
FROM public.crates c
JOIN public.default_versions dv     ON dv.crate_id = c.id       -- the version cargo would pick
JOIN public.versions v              ON v.id = dv.version_id
LEFT JOIN public.crate_downloads cd ON cd.crate_id = c.id
WHERE NOT v.yanked                                              -- yanked default => uninstallable
  AND (COALESCE(v.has_lib, true) OR COALESCE(cardinality(v.bin_names), 0) > 0)
ON CONFLICT ON CONSTRAINT probe_unique_target DO NOTHING;

COMMIT;
