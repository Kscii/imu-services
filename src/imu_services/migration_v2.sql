ALTER TABLE admission_budget DROP CONSTRAINT admission_budget_id_check;
ALTER TABLE admission_budget ADD CONSTRAINT admission_budget_id_check CHECK(id IN (1,2));
ALTER TABLE runs ADD COLUMN mode text NOT NULL DEFAULT 'incremental'
    CHECK(mode IN ('incremental','whole_record'));
ALTER TABLE runs ADD COLUMN input_sha256 text;
ALTER TABLE runs ADD COLUMN input_cleared boolean NOT NULL DEFAULT false;
ALTER TABLE runs ADD COLUMN diagnostics jsonb;
