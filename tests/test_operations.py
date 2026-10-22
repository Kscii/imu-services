from uuid import uuid4
import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient
from imu_services.api import create_app
from imu_services.query import create_app as query_app
from imu_services.operations import grant_roles
from imu_services.worker import Worker
from imu_services.release import check_upgrade

pytestmark = pytest.mark.postgres


def test_query_role_cannot_read_inputs_or_write_results(store):
    role = "readonly_" + uuid4().hex
    runtime = "runtime_" + uuid4().hex
    with psycopg.connect(store.settings.database_url, autocommit=True) as c:
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        c.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(runtime)))
        try:
            grant_roles(c, schema, runtime, role)
            c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            for view in (
                "query_v1_runs",
                "query_v1_results",
                "query_v1_queue",
                "query_v1_budget",
                "query_v1_workers",
                "schema_migrations",
            ):
                c.execute(sql.SQL("SELECT * FROM {} LIMIT 1").format(sql.Identifier(view)))
            for statement in (
                "SELECT payload FROM input_batches",
                "SELECT checkpoint FROM runs",
                "DELETE FROM query_v1_results",
                "UPDATE query_v1_runs SET status='failed'",
            ):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    c.execute(statement)
            c.execute("RESET ROLE")
            c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(runtime)))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("DELETE FROM results")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("UPDATE results SET events='[]'")
        finally:
            c.execute("RESET ROLE")
            for name in (role, runtime):
                c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
                c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def test_metrics_auth_and_worker_heartbeat(store, backend):
    worker = Worker(store.settings, store, backend)
    worker.once()
    headers = {"Authorization": "Bearer " + store.settings.api_token}
    for factory in (create_app, query_app):
        with TestClient(factory(store.settings, store)) as client:
            assert client.get("/metrics").status_code == 401
            assert client.get("/health/ready").status_code == 200
            metrics = client.get("/metrics", headers=headers)
            assert metrics.status_code == 200 and "imu_http_requests_total" in metrics.text
            if factory is query_app:
                operations = client.get("/v2/operations", headers=headers).json()
                assert operations["budget"]["pending_bytes"] == 0
                assert operations["workers"][0]["id"] == str(worker.worker_id)


def test_upgrade_refuses_unsupported_active_pipeline(store, run_request):
    run_id, _ = store.create(run_request)
    assert check_upgrade(store.settings.database_url)["compatible"]
    with pytest.raises(RuntimeError, match="unfinished runs"):
        check_upgrade(store.settings.database_url, steps_enabled=False)
    with store.pool.connection() as c:
        c.execute("UPDATE runs SET fingerprint='previous-incompatible-pipeline' WHERE id=%s", (run_id,))
    with pytest.raises(RuntimeError, match="unfinished runs"):
        check_upgrade(store.settings.database_url)


def test_v1_migration_requires_drain_and_keeps_old_results(store, run_request):
    from imu_services.store import migrate

    run_id, _ = store.create(run_request)
    with store.pool.connection() as c:
        c.execute("DROP VIEW query_v1_runs, query_v1_budget, query_v2_modes")
        c.execute(
            "ALTER TABLE runs DROP COLUMN mode, DROP COLUMN input_sha256, DROP COLUMN input_cleared, DROP COLUMN diagnostics"
        )
        c.execute("DELETE FROM admission_budget WHERE id=2")
        c.execute("ALTER TABLE admission_budget DROP CONSTRAINT admission_budget_id_check")
        c.execute("ALTER TABLE admission_budget ADD CONSTRAINT admission_budget_id_check CHECK(id=1)")
        c.execute("DELETE FROM schema_migrations WHERE version=2")
    with pytest.raises(RuntimeError, match="drain all v1 tasks"):
        migrate(store.settings.database_url)
    with store.pool.connection() as c:
        c.execute("UPDATE runs SET status='completed' WHERE id=%s", (run_id,))
    migrate(store.settings.database_url)
    migrate(store.settings.database_url)
    assert store.ready()
    with store.pool.connection() as c:
        row = c.execute("SELECT * FROM query_v1_runs WHERE id=%s", (run_id,)).fetchone()
        assert row["status"] == "completed" and row["mode"] == "incremental"
        assert c.execute("SELECT count(*) AS n FROM admission_budget").fetchone()["n"] == 2
