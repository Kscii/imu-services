import os
from uuid import uuid4
import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from imu_services.settings import Settings
from imu_services.store import Store, migrate


class FakeBackend:
    def whole_record(self, x, valid, baseline, sign_adaptation):
        return baseline.argmax(1).astype(np.int8), {"enabled": sign_adaptation, "applied": False}

    def activity_probabilities(self, x, valid):
        p = np.tile([.1, .2, .3, .4], (len(x), 1))
        p[x[:, 0] > 1] = [.7, .1, .1, .1]
        return p.astype(np.float32)

    def step_gate(self, x, center):
        return .8

    def step_contacts(self, x):
        return np.flatnonzero(x[:, 0].astype(int) % 17 == 0), "OK"


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def store():
    url = os.environ.get("IMU_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set IMU_TEST_DATABASE_URL to an isolated PostgreSQL database")
    schema = "test_"+uuid4().hex
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(url, options=f"-csearch_path={schema}")
    migrate(scoped)
    settings = Settings(scoped, "integration-write-token-not-for-production")
    obj = Store(settings)
    try:
        yield obj
    finally:
        obj.close()
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def run_request():
    return dict(request_key=uuid4().hex, recording_id="recording-1", device_id="device-1",
                start_time="2026-10-20T00:00:00+00:00", steps_enabled=True)
