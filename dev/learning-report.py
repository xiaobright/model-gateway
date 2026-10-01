"""Read-only inventory of learning metadata; no gateway import or database access."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re

PATTERN = re.compile(r"events-\d{8}T\d{12}Z-[0-9a-f]{32}\.jsonl")
MAX_LINE = 128 * 1024


def report(directory: Path) -> dict:
    files = sorted(
        p for p in directory.glob("events-*.jsonl")
        if PATTERN.fullmatch(p.name) and p.is_file() and not p.is_symlink()
    )
    kinds, notes, statuses = Counter(), Counter(), Counter()
    starts, ends, attempt_starts, attempt_ends = set(), set(), set(), set()
    earliest = latest = None
    bad_lines = unknown_schema = lost_windows = interrupted = completion_candidates = 0
    protocol_errors = observation_errors = 0
    recovered_gaps = 0
    writer_status = {}
    for path in files:
        with path.open("rb") as stream:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(MAX_LINE + 1)
                    bad_lines += 1
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or "kind" not in row:
                        raise ValueError("not a record")
                except (ValueError, UnicodeError):
                    bad_lines += 1
                    continue
                if row.get("schema_version") != 1:
                    unknown_schema += 1
                    continue
                kind = row["kind"]
                kinds[kind] += 1
                ts = row.get("ts")
                if isinstance(ts, str):
                    earliest = min(earliest, ts) if earliest else ts
                    latest = max(latest, ts) if latest else ts
                if kind == "request_start":
                    starts.add(row.get("request_id"))
                elif kind == "request_end":
                    ends.add(row.get("request_id"))
                elif kind == "attempt_start":
                    attempt_starts.add(row.get("attempt_id"))
                elif kind == "attempt_end":
                    attempt_ends.add(row.get("attempt_id"))
                    notes[row.get("note")] += 1
                    statuses[str(row.get("http_status"))] += 1
                    lost_windows += row.get("windows_dropped", 0)
                    recovered_gaps += row.get("recovered_gaps_ge_5s", 0)
                    interrupted += bool(row.get("interrupted"))
                    protocol_errors += bool(row.get("protocol_error_seen"))
                    observed_badly = bool(
                        row.get("observer_oversized_frames")
                        or row.get("event_counts", {}).get("malformed")
                        or row.get("event_counts", {}).get("observation_error")
                    )
                    observation_errors += observed_badly
                    completion_candidates += bool(
                        row.get("completion_seen") and row.get("note") == "ok"
                        and 200 <= (row.get("http_status") or 0) < 300
                        and not row.get("protocol_error_seen") and not observed_badly
                    )
                elif kind == "collector_status":
                    writer_status[row["run_id"]] = {
                        key: row.get(key) for key in ("ts", "dropped", "errors", "last_error")
                    }
    return {
        "files": len(files), "bytes": sum(p.stat().st_size for p in files),
        "earliest_utc": earliest, "latest_utc": latest, "records": dict(kinds),
        "attempt_notes": dict(notes), "attempt_http_statuses": dict(statuses),
        "request_starts_without_end": len(starts - ends),
        "request_ends_without_start": len(ends - starts),
        "attempt_starts_without_end": len(attempt_starts - attempt_ends),
        "attempt_ends_without_start": len(attempt_ends - attempt_starts),
        "interrupted_attempts": interrupted,
        "sse_transport_completion_candidates": completion_candidates,
        "protocol_error_attempts": protocol_errors, "observation_error_attempts": observation_errors,
        "recovered_content_gaps_ge_5s": recovered_gaps,
        "coalesced_windows_dropped": lost_windows, "bad_lines": bad_lines,
        "unknown_schema_lines": unknown_schema, "last_writer_status_by_run": writer_status,
        "caution": "Missing ends may mean active requests, retention, crash or loss; not failure labels.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1] / "data" / "learning")
    args = parser.parse_args()
    print(json.dumps(report(args.directory), ensure_ascii=False, indent=2))
