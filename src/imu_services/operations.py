"""Administrator-only database setup. Runtime services cannot grant permissions."""
import psycopg
from psycopg import sql


def grant_roles(connection, schema="public", runtime="imu_runtime", query="imu_query"):
    """Grant access to an existing dedicated schema, without ownership or DDL."""
    c = connection
    s, r, q = map(sql.Identifier, (schema, runtime, query))
    c.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(s, r, q))
    c.execute(sql.SQL("GRANT SELECT ON {}.schema_migrations TO {}, {}").format(s, r, q))
    for table in ("runs", "input_batches", "admission_budget", "worker_heartbeats"):
        c.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON {}.{} TO {}")
                  .format(s, sql.Identifier(table), r))
    for table in ("results", "attempt_events"):
        c.execute(sql.SQL("GRANT SELECT, INSERT ON {}.{} TO {}").format(s, sql.Identifier(table), r))
    for table in ("query_v1_runs", "query_v1_results", "query_v1_queue",
                  "query_v1_budget", "query_v1_workers", "query_v2_modes"):
        c.execute(sql.SQL("GRANT SELECT ON {}.{} TO {}, {}").format(s, sql.Identifier(table), r, q))
    c.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(s, r))


def install_roles(url, runtime_password, query_password):
    if min(len(runtime_password), len(query_password)) < 32:
        raise ValueError("database role passwords require at least 32 characters")
    with psycopg.connect(url) as c:
        for role, password in (("imu_runtime", runtime_password), ("imu_query", query_password)):
            if not c.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                c.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
            c.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}")
                      .format(sql.Identifier(role), sql.Literal(password)))
        grant_roles(c)
