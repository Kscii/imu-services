from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from imu_services.analysis import activity_analysis, calendar_summary
from test_analysis import MemoryReader, request, span


def with_steps(reader, run_id, events):
    reader.rows[run_id][0]["events"].extend(events)
    analyzed = sum(e["end"] - e["start"] for e in events if e["analyzed"])
    unresolved = sum(e["end"] - e["start"] for e in events if not e["analyzed"])
    reader.runs[run_id]["summary"]["steps"] = {
        "enabled": True,
        "estimated_step_count": sum(len(e["event_samples"]) for e in events) if analyzed else None,
        "analyzed_s": analyzed / 25, "unresolved_valid_s": unresolved / 25,
    }


def steps(a, b, points=(), analyzed=True):
    return dict(kind="step_segment", start=a, end=b, event_samples=list(points), analyzed=analyzed)


def test_steps_midnight_clip_and_hour_day_week_additivity():
    reader = MemoryReader()
    run = reader.add(1, "2026-09-20T23:59:59+08:00", [span(0, 100)])
    with_steps(reader, run, [steps(0, 100, [0, 24, 25, 26, 49, 50, 99])])
    result = activity_analysis(reader, request([run, run], timezone="Asia/Shanghai",
                              date_from="2026-09-21", date_to="2026-09-21"))
    calendar = result["calendar"]
    assert result["analysis_version"] == "activity-calendar-v2"
    assert result["recording_steps"][0]["statistics"]["estimated_step_count"] == 7
    assert calendar["steps"]["estimated_step_count"] == 5
    assert calendar["steps"]["analyzed_s"] == 3
    assert calendar["hourly"][0]["steps"]["estimated_step_count"] == 5
    assert calendar["hourly"][1]["steps"]["estimated_step_count"] is None
    assert calendar["daily"][0]["steps"] == calendar["weekly"][0]["steps"] == calendar["steps"]
    both = activity_analysis(reader, request([run], timezone="Asia/Shanghai",
                             date_from="2026-09-20", date_to="2026-09-21"))["calendar"]
    assert [d["steps"]["estimated_step_count"] for d in both["daily"]] == [2, 5]
    assert sum(w["steps"]["estimated_step_count"] or 0 for w in both["weekly"]) == 7
    assert both["steps"]["estimated_step_count"] == 7


@pytest.mark.parametrize("start,date,hours,local_labels", [
    ("2026-10-04T01:59:59+10:00", "2026-10-04", 23, ["01:00:00+10:00", "03:00:00+11:00"]),
    ("2026-04-05T02:59:59+11:00", "2026-04-05", 25, ["02:00:00+11:00", "02:00:00+10:00"]),
])
def test_step_dst_gap_and_repeated_hour(start, date, hours, local_labels):
    reader = MemoryReader()
    run = reader.add(1, start, [span(0, 100)])
    with_steps(reader, run, [steps(0, 100, [0, 25, 49, 50])])
    calendar = activity_analysis(reader, request([run], date_from=date, date_to=date))["calendar"]
    assert len(calendar["hourly"]) == hours
    observed = [h for h in calendar["hourly"] if h["steps"]["analyzed_s"]]
    assert [h["local_start"][11:] for h in observed] == local_labels
    assert [h["steps"]["estimated_step_count"] for h in observed] == [1, 3]
    assert calendar["steps"]["estimated_step_count"] == 4


def test_step_zero_unavailable_disabled_and_partial_are_distinct():
    reader = MemoryReader()
    run = reader.add(1, "2026-10-04T03:00:00+11:00", [span(0, 100)])
    disabled = activity_analysis(reader, request([run]))["calendar"]["steps"]
    assert disabled["estimated_step_count"] is None
    with_steps(reader, run, [steps(0, 100, analyzed=False)])
    unavailable = activity_analysis(reader, request([run]))["calendar"]["steps"]
    assert unavailable["estimated_step_count"] is None and unavailable["unresolved_valid_s"] == 4
    reader.rows[run][0]["events"] = reader.rows[run][0]["events"][:1]
    with_steps(reader, run, [steps(0, 25), steps(25, 100, analyzed=False)])
    partial = activity_analysis(reader, request([run]))["calendar"]["steps"]
    assert partial["estimated_step_count"] == 0 and partial["coverage_of_processed_valid"] == .25
    assert partial["analyzed_s"] == 1 and partial["unresolved_valid_s"] == 3


@pytest.mark.parametrize("bad", ["duplicate", "outside", "overlap", "count", "coverage"])
def test_reject_invalid_step_events(bad):
    reader = MemoryReader()
    run = reader.add(1, "2026-10-04T03:00:00+11:00", [span(0, 100)])
    with_steps(reader, run, [steps(0, 100, [25, 50])])
    event = reader.rows[run][0]["events"][1]
    if bad == "duplicate":
        event["event_samples"] = [25, 25]
    elif bad == "outside":
        event["event_samples"] = [25, 100]
    elif bad == "overlap":
        reader.rows[run][0]["events"].append(deepcopy(event))
    elif bad == "count":
        reader.runs[run]["summary"]["steps"]["estimated_step_count"] = 3
    else:
        reader.runs[run]["summary"]["steps"]["analyzed_s"] = 5
    with pytest.raises(ValueError):
        activity_analysis(reader, request([run]))


def test_single_record_calendar_matches_multi_record_api():
    reader = MemoryReader()
    run = reader.add(1, "2026-10-03T23:59:59+10:00", [span(0, 25), span(25, 50, status="invalid"),
                    span(50, 125)])
    with_steps(reader, run, [steps(0, 25, [0, 24]), steps(50, 100, [50, 99]), steps(100, 125, analyzed=False)])
    got = calendar_summary(reader, run, "Australia/Sydney", date(2026, 10, 4), date(2026, 10, 4))
    expected = activity_analysis(reader, request([run]))["calendar"]
    assert got["analysis_version"] == "activity-calendar-v2"
    assert got["steps"] == expected["steps"]
    assert got["steps"]["estimated_step_count"] == 2
    assert got["steps"]["analyzed_s"] == 2
    assert got["steps"]["unresolved_valid_s"] == 1
    for kind in ["hourly", "daily", "weekly"]:
        assert [row["steps"] for row in got[kind]] == [row["steps"] for row in expected[kind]]


def test_step_budget_is_shared_across_records(monkeypatch):
    import imu_services.analysis as analysis
    monkeypatch.setattr(analysis, "MAX_SEGMENTS", 3)
    reader = MemoryReader()
    runs = []
    for i in range(2):
        run = reader.add(i + 1, f"2026-10-04T03:0{i}:00+11:00", [span(0, 100)])
        with_steps(reader, run, [steps(0, 50, [25]), steps(50, 100, [75])])
        runs.append(run)
    with pytest.raises(ValueError, match="step segments"):
        activity_analysis(reader, request(runs))


@pytest.mark.parametrize("zone,day,start", [
    ("Asia/Shanghai", "2026-10-04", "2026-10-03T23:59:50.123456+08:00"),
    ("Australia/Sydney", "2026-04-05", "2026-04-05T02:59:50.123456+11:00"),
    ("Australia/Sydney", "2026-10-04", "2026-10-04T01:59:50.123456+10:00"),
])
def test_event_counts_match_direct_timestamp_membership(zone, day, start):
    reader = MemoryReader()
    run = reader.add(1, start, [span(0, 2000)])
    points = list(range(0, 2000, 13))
    with_steps(reader, run, [steps(0, 2000, points)])
    calendar = activity_analysis(reader, request([run], timezone=zone, date_from=day, date_to=day))["calendar"]
    origin = datetime.fromisoformat(start).astimezone(timezone.utc)
    timestamps = [origin + timedelta(seconds=s/25) for s in points]
    for hour in calendar["hourly"]:
        lo, hi = (datetime.fromisoformat(hour[k]).astimezone(timezone.utc) for k in ["start", "end"])
        expected = sum(lo <= t < hi for t in timestamps)
        assert (hour["steps"]["estimated_step_count"] or 0) == expected


def test_empty_record_and_no_input_preserve_unknown():
    reader = MemoryReader()
    run = reader.add(1, "2026-10-04T03:00:00+11:00", [])
    with_steps(reader, run, [])
    for ids in [[], [run]]:
        got = activity_analysis(reader, request(ids))["calendar"]["steps"]
        assert got["estimated_step_count"] is None
        assert got["coverage_of_processed_valid"] is None
