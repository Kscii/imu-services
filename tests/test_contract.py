import sys
import pytest
from pydantic import ValidationError
from imu_services.models import CreateRun, BatchRequest
from imu_services.analysis import recording_summary


def test_timestamp_and_sample_contract():
    with pytest.raises(ValidationError):
        CreateRun(request_key="r", recording_id="r", device_id="d", start_time="2026-10-20T00:00:00")
    with pytest.raises(ValidationError):
        BatchRequest(batch_id="b", start_sample=True, acceleration_mps2=[[0, 0, 0]])
    with pytest.raises(ValidationError):
        BatchRequest(batch_id="b", start_sample=0, acceleration_mps2=[[0, 0, float("nan")]])
    with pytest.raises(ValidationError):
        BatchRequest(batch_id="b", start_sample=0, acceleration_mps2=[[0, 0, 0]], valid=[1])


def test_analysis_boundary_needs_no_models():
    class Reader:
        def run(self, run_id):
            return {"provenance": {"version": "fixed"}, "status": "completed", "summary": {"observed_s": 60}}
    result = recording_summary(Reader(), "run")
    assert result["summary"]["observed_s"] == 60
    assert "torch" not in sys.modules
