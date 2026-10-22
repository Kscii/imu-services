CREATE TABLE IF NOT EXISTS schema_migrations (
    version integer PRIMARY KEY, installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admission_budget (
    id integer PRIMARY KEY CHECK (id IN (1,2)),
    pending_bytes bigint NOT NULL DEFAULT 0 CHECK (pending_bytes >= 0)
);
INSERT INTO admission_budget(id) VALUES(1) ON CONFLICT DO NOTHING;
INSERT INTO admission_budget(id) VALUES(2) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY,
    request_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    recording_id text NOT NULL,
    device_id text NOT NULL,
    start_time timestamptz NOT NULL,
    config jsonb NOT NULL,
    provenance jsonb NOT NULL,
    fingerprint text NOT NULL,
    mode text NOT NULL DEFAULT 'incremental' CHECK(mode IN ('incremental','whole_record')),
    input_sha256 text,
    input_cleared boolean NOT NULL DEFAULT false,
    diagnostics jsonb,
    status text NOT NULL DEFAULT 'waiting'
        CHECK(status IN ('waiting','queued','running','completed','failed')),
    available_end bigint NOT NULL DEFAULT 0 CHECK(available_end>=0),
    processed_samples bigint NOT NULL DEFAULT 0 CHECK(processed_samples>=0),
    sealed_end bigint CHECK(sealed_end>=0),
    checkpoint jsonb,
    pending_events jsonb NOT NULL DEFAULT '[]',
    published_samples bigint NOT NULL DEFAULT 0,
    summary jsonb,
    lease_token bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    failures integer NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK(processed_samples<=available_end),
    CHECK(sealed_end IS NULL OR available_end<=sealed_end)
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, next_attempt, updated_at);
CREATE INDEX IF NOT EXISTS runs_recording ON runs(recording_id, created_at);

CREATE TABLE IF NOT EXISTS input_batches (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    batch_id text NOT NULL,
    start_sample bigint NOT NULL CHECK(start_sample>=0),
    end_sample bigint NOT NULL CHECK(end_sample>start_sample),
    digest text NOT NULL,
    payload bytea,
    payload_bytes bigint NOT NULL CHECK(payload_bytes>=0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,batch_id),
    UNIQUE(run_id,start_sample)
);
CREATE INDEX IF NOT EXISTS input_pending ON input_batches(run_id,start_sample) WHERE payload IS NOT NULL;

CREATE TABLE IF NOT EXISTS results (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    start_sample bigint NOT NULL,
    end_sample bigint NOT NULL,
    final boolean NOT NULL,
    provenance jsonb NOT NULL,
    events jsonb NOT NULL,
    summary jsonb NOT NULL,
    content_sha256 text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,start_sample,end_sample,final)
);
CREATE INDEX IF NOT EXISTS results_paging ON results(run_id,id);

CREATE TABLE IF NOT EXISTS attempt_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    lease_token bigint NOT NULL,
    event text NOT NULL,
    detail text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    id uuid PRIMARY KEY,
    fingerprints text[] NOT NULL,
    last_seen timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_result_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'published results are immutable';
END
$$;
DROP TRIGGER IF EXISTS immutable_result ON results;
CREATE TRIGGER immutable_result BEFORE UPDATE ON results
FOR EACH ROW EXECUTE FUNCTION reject_result_update();

CREATE OR REPLACE VIEW query_v1_runs AS
SELECT id,recording_id,device_id,start_time,config,provenance,fingerprint,status,
       available_end,processed_samples,sealed_end,summary,last_error,created_at,updated_at,
       mode,input_sha256,input_cleared,diagnostics
FROM runs;
CREATE OR REPLACE VIEW query_v1_results AS SELECT * FROM results;
CREATE OR REPLACE VIEW query_v1_queue AS
SELECT status,count(*) AS runs FROM runs GROUP BY status;
CREATE OR REPLACE VIEW query_v1_budget AS
SELECT coalesce(sum(pending_bytes),0)::bigint AS pending_bytes,
       (SELECT min(created_at) FROM input_batches WHERE payload IS NOT NULL) AS oldest_pending_input
FROM admission_budget;
CREATE OR REPLACE VIEW query_v1_workers AS
SELECT id,fingerprints,last_seen FROM worker_heartbeats
WHERE last_seen>now()-interval '90 seconds';
CREATE OR REPLACE VIEW query_v2_modes AS
SELECT CASE b.id WHEN 1 THEN 'incremental' ELSE 'whole_record' END AS mode, b.pending_bytes,
       count(r.id) FILTER (WHERE r.status='queued') AS queued,
       count(r.id) FILTER (WHERE r.status='running') AS running,
       count(r.id) FILTER (WHERE r.status='failed') AS failed
FROM admission_budget b LEFT JOIN runs r
ON r.mode=CASE b.id WHEN 1 THEN 'incremental' ELSE 'whole_record' END
GROUP BY b.id,b.pending_bytes;
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
INSERT INTO schema_migrations(version) VALUES(2) ON CONFLICT DO NOTHING;
