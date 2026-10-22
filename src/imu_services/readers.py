"""Analysis input boundary: immutable results, with database and HTTP adapters."""
from typing import Protocol

from .errors import NotFound


class ResultReader(Protocol):
    def run(self, run_id): ...
    def results(self, run_id, after=0, limit=100): ...


class PostgresReader:
    def __init__(self, store):
        self.store = store

    def run(self, run_id):
        with self.store.pool.connection() as c:
            row = c.execute("SELECT * FROM query_v1_runs WHERE id=%s", (run_id,)).fetchone()
        if row is None:
            raise NotFound("run not found")
        return row

    def results(self, run_id, after=0, limit=100):
        self.run(run_id)
        with self.store.pool.connection() as c:
            return c.execute("""SELECT * FROM query_v1_results
                WHERE run_id=%s AND id>%s ORDER BY id LIMIT %s""", (run_id, after, limit)).fetchall()

    def versions(self, recording_id, limit=100):
        with self.store.pool.connection() as c:
            return c.execute("""SELECT id,created_at,status,provenance FROM query_v1_runs
                WHERE recording_id=%s ORDER BY created_at DESC,id DESC LIMIT %s""",
                             (recording_id, limit)).fetchall()


class HttpResultReader:
    """No database dependency; the same analysis can later run in another process."""
    def __init__(self, base_url, token, *, transport=None):
        import httpx
        self.client = httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"},
                                   timeout=30, transport=transport)

    def _get(self, path, **kwargs):
        response = self.client.get(path, **kwargs)
        if response.status_code == 404:
            raise NotFound("run not found")
        response.raise_for_status()
        return response.json()

    def run(self, run_id):
        return self._get(f"/v2/runs/{run_id}")

    def results(self, run_id, after=0, limit=100):
        return self._get(f"/v2/runs/{run_id}/results",
                         params={"after": after, "limit": limit})["items"]

    def close(self):
        self.client.close()
