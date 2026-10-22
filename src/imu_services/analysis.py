"""Descriptive analysis of explicitly selected immutable inference results."""
from datetime import datetime, timedelta, timezone as utc_timezone
import hashlib
import json

from .readers import ResultReader
from .errors import Conflict


MAX_SEGMENTS = 100000


def _record_events(reader, run_id, run, budget):
    from imu_inference.reporting import events_to_segments

    if run["sealed_end"] is None or not run["summary"] or not run["summary"]["finished"]:
        raise Conflict("completed run has incomplete final metadata")
    segments, step_segments = [], []
    cursor, final_count, row_count, final_hash = 0, 0, 0, None
    while True:
        rows = reader.results(run_id, cursor, 500)
        if not rows:
            break
        for row in rows:
            if type(row["id"]) is not int or row["id"] <= cursor:
                raise ValueError("reader result cursor must strictly advance")
            if str(row["run_id"]) != str(run_id) or final_count:
                raise ValueError("reader returned another run or events after its final result")
            cursor = row["id"]
            row_count += 1
            final_count += bool(row["final"])
            if row["final"]:
                final_hash = row["content_sha256"]
            activity = events_to_segments(row["events"], start_time=run["start_time"],
                                          recording_id=run["recording_id"])
            steps = [e for e in row["events"] if e["kind"] == "step_segment"]
            budget[0] += len(activity)
            budget[1] += len(steps)
            if max(budget) > MAX_SEGMENTS:
                raise ValueError("analysis exceeds 100000 activity or step segments; narrow the input selection")
            segments.extend(activity)
            step_segments.extend(steps)
    if final_count != 1:
        raise Conflict("completed run must have exactly one final result")
    source = {
        "run_id": str(run_id), "recording_id": run["recording_id"], "device_id": run["device_id"],
        "start_time": _utc(run["start_time"]).isoformat(), "samples": run["sealed_end"],
        "fingerprint": run["fingerprint"], "provenance": run["provenance"],
        "result_rows": row_count, "last_result_cursor": cursor, "final_content_sha256": final_hash,
    }
    return segments, step_segments, source


def recording_summary(reader: ResultReader, run_id):
    run = reader.run(run_id)
    return {"schema": "imu.analysis.recording.v1", "analysis_version": "descriptive-v1",
            "run_id": str(run_id), "inference_provenance": run["provenance"],
            "processing_status": run["status"], "summary": run["summary"],
            "limitations": ["descriptive_estimates_not_clinical_measurements",
                            "experimental_step_accuracy_not_validated_on_chest",
                            "activity_and_step_progress_have_separate_frontiers"]}


def calendar_summary(reader: ResultReader, run_id, timezone, date_from, date_to):
    from imu_inference.reporting import calendar_statistics
    from .step_calendar import add_calendar_steps
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    if not 0 <= (date_to-date_from).days < 31:
        raise ValueError("calendar request must span 1 to 31 inclusive dates")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown IANA timezone") from None
    run = reader.run(run_id)
    if run["status"] != "completed":
        raise Conflict("calendar export requires a completed run; use /analysis for incremental totals")
    segments, steps, source = _record_events(reader, run_id, run, [0, 0])
    result = calendar_statistics(segments, participant_id=run["device_id"],
                                 model_version=run["fingerprint"], timezone=timezone,
                                 date_from=date_from.isoformat(), date_to=date_to.isoformat())
    result["run_id"] = str(run_id)
    result["inference_provenance"] = run["provenance"]
    result["subject_scope"] = "device recording; participant identity mapping is upstream-owned"
    add_calendar_steps(result, [(run, steps)])
    result["analysis_version"] = "activity-calendar-v2"
    result["sources"] = [source]
    return result


def _utc(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("explicit recording start timezone required")
    return result.astimezone(utc_timezone.utc)


def activity_analysis(reader: ResultReader, request):
    """Completed-record snapshots only. No database, scheduling or identity lookup."""
    from imu_inference.reporting import calendar_statistics

    ids = sorted({str(value) for value in request.run_ids})
    runs, recordings, fingerprints, spans = [], set(), set(), []
    for run_id in ids:
        run = reader.run(run_id)
        if run["status"] != "completed":
            raise Conflict(f"run {run_id} is not completed")
        if run["recording_id"] in recordings:
            raise Conflict("multiple versions of one recording; select exactly one run")
        recordings.add(run["recording_id"])
        fingerprints.add(run["fingerprint"])
        start = _utc(run["start_time"])
        if run["sealed_end"] is None or not run["summary"] or not run["summary"]["finished"]:
            raise Conflict("completed run has incomplete final metadata")
        end = start+timedelta(microseconds=run["sealed_end"]*40000)
        if end > start:
            spans.append((start, end))
        runs.append((run_id, run))
    if len(fingerprints) > 1:
        raise Conflict("mixed processing fingerprints; select the same model, code and configuration")
    spans.sort()
    if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
        raise Conflict("recording time ranges overlap; reconcile records upstream")

    segments, sources, steps, step_records = [], [], [], []
    budget = [0, 0]
    for run_id, run in runs:
        activity, step_segments, source = _record_events(reader, run_id, run, budget)
        segments.extend(activity)
        sources.append(source)
        steps.append({"run_id": run_id, "recording_id": run["recording_id"],
                      "scope": "whole_recording_not_date_clipped", "statistics": run["summary"]["steps"]})
        step_records.append((run, step_segments))

    fingerprint = next(iter(fingerprints), None)
    calendar = calendar_statistics(
        segments, participant_id=request.participant_id, model_version=fingerprint or "no-input-records",
        timezone=request.timezone, date_from=request.date_from.isoformat(), date_to=request.date_to.isoformat())
    calendar["model_version"] = fingerprint
    from .step_calendar import add_calendar_steps
    add_calendar_steps(calendar, step_records)
    # This API serves aggregates. Avoid serializing the same long timeline several
    # times; immutable detailed events remain available through the result API.
    calendar.pop("strict_label_runs")
    calendar.pop("locomotion_periods")
    for day in calendar["daily"]:
        day.pop("timeline")
    # The selected source set is stable; no generated timestamp changes repeat requests.
    manifest = {"analysis_version": "activity-calendar-v2", "participant_id": request.participant_id,
                "timezone": request.timezone, "date_from": request.date_from.isoformat(),
                "date_to": request.date_to.isoformat(), "sources": sources}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema": "imu.analysis.activity.v1", "analysis_version": manifest["analysis_version"],
        "participant_id": request.participant_id, "subject_assignment": "caller_asserted",
        "source_manifest_sha256": digest, "sources": sources,
        "duplicate_run_ids_removed": len(request.run_ids)-len(ids),
        "calendar": calendar, "recording_steps": steps,
        "definitions": {
            "activity_fraction_denominator": "observed classified activity time",
            "coverage_denominator": "actual local calendar duration including DST",
            "weekly_scope": "only included dates; no extrapolation to missing dates",
            "label_runs": "model label runs; not verified physical transitions or bouts",
            "calendar_steps": "observed step events clipped to timezone-aware half-open calendar buckets",
        },
        "limitations": ["descriptive_estimates_not_clinical_measurements",
                        "observation_is_not_verified_wearing_compliance",
                        "experimental_step_accuracy_not_validated_on_chest",
                        "recording_steps_are_not_daily_or_weekly_estimates"],
    }
