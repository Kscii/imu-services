"""Date-clipped GP2 counts and coverage from immutable step events."""
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _us(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("explicit step clock timezone required")
    delta = parsed.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _statistics(values):
    count, analyzed, unresolved = values
    return {
        "estimated_step_count": count if analyzed else None,
        "analyzed_s": analyzed / 1_000_000,
        "unresolved_valid_s": unresolved / 1_000_000,
        "coverage_of_processed_valid": analyzed / (analyzed + unresolved) if analyzed + unresolved else None,
        "experimental": True,
    }


def add_calendar_steps(calendar, records):
    """Reuse actual calendar hour edges, including repeated or missing DST hours.

    Each record is (immutable run metadata, step_segment events). Counts and
    coverage use integer microseconds; no clipping by activity classifications.
    """
    hours = calendar["hourly"]
    edges = [_us(h["start"]) for h in hours] + [_us(hours[-1]["end"])]
    totals = [[0, 0, 0] for _ in hours]
    for run, segments in records:
        statistics = run["summary"]["steps"]
        if not statistics["enabled"]:
            if segments:
                raise ValueError("disabled step estimation has step events")
            continue
        origin = _us(run["start_time"])
        previous_end, count, analyzed_samples, unresolved_samples = 0, 0, 0, 0
        for segment in sorted(segments, key=lambda s: s["start"]):
            start, end = segment["start"], segment["end"]
            samples = segment["event_samples"]
            analyzed = segment["analyzed"]
            if (type(start) is not int or type(end) is not int or
                    not previous_end <= start < end <= run["sealed_end"] or type(analyzed) is not bool):
                raise ValueError("invalid or overlapping step segment")
            if (any(type(s) is not int or not start <= s < end for s in samples) or
                    any(b <= a for a, b in zip(samples, samples[1:])) or (samples and not analyzed)):
                raise ValueError("invalid or repeated step event")
            previous_end = end
            count += len(samples)
            analyzed_samples += (end - start) if analyzed else 0
            unresolved_samples += (end - start) if not analyzed else 0
            a, b = origin + start * 40000, origin + end * 40000
            points = [origin + s * 40000 for s in samples]
            first = max(0, bisect_right(edges, a) - 1)
            stop = min(len(hours), bisect_left(edges, b))
            for i in range(first, stop):
                lo, hi = max(a, edges[i]), min(b, edges[i + 1])
                if hi <= lo:
                    continue
                totals[i][0] += bisect_left(points, hi) - bisect_left(points, lo)
                totals[i][1 if analyzed else 2] += hi - lo
        expected = statistics["estimated_step_count"]
        if count != (expected if expected is not None else 0):
            raise ValueError("step events disagree with final count")
        for key, actual in (("analyzed_s", analyzed_samples / 25),
                            ("unresolved_valid_s", unresolved_samples / 25)):
            expected = statistics.get(key)
            if expected is not None and abs(actual - expected) > 1e-7:
                raise ValueError("step events disagree with final coverage")
    daily = {d["date"]: [0, 0, 0] for d in calendar["daily"]}
    for hour, values in zip(hours, totals):
        hour["steps"] = _statistics(values)
        day = daily[hour["local_start"][:10]]
        for i, value in enumerate(values):
            day[i] += value
    for day in calendar["daily"]:
        day["steps"] = _statistics(daily[day["date"]])
    for week in calendar["weekly"]:
        week["steps"] = _statistics([sum(daily[d][i] for d in week["included_dates"]) for i in range(3)])
    calendar["steps"] = _statistics([sum(v[i] for v in totals) for i in range(3)])
