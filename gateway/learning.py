"""Bounded, metadata-only observation. No routing decisions and no request-path I/O."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid

SCHEMA_VERSION = 1
POLICY_VERSION = "rules-v1"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
KEEP_DAYS = 30
QUEUE_SIZE = 128
MAX_RECORD_BYTES = 128 * 1024
MAX_WINDOWS = 256
WINDOW_MS = 250
FILE_PATTERN = re.compile(r"events-\d{8}T\d{12}Z-[0-9a-f]{32}\.jsonl")
EVENT_KINDS = frozenset({
    "content", "reasoning", "tool", "heartbeat", "metadata", "completion",
    "protocol_error", "malformed", "observation_error", "body",
})
CONTENT_KINDS = frozenset({"content", "reasoning", "tool"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Collector:
    """One app lifespan, one bounded queue, one writer. Own files only."""

    def __init__(self, directory: Path, *, enabled: bool = True):
        self.directory = directory.absolute()
        self.enabled = enabled
        self.run_id = uuid.uuid4().hex
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.written = 0
        self.dropped = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_write: str | None = None
        self.active_groups: dict[int, int] = {}
        self._file: Path | None = None
        self._day: str | None = None
        self._size = 0
        self._maintenance_at = 0.0

    def start(self) -> None:
        if self.enabled:
            try:
                self.thread = threading.Thread(
                    target=self._run, name="gateway-learning", daemon=True,
                )
                self.thread.start()
            except Exception as exc:
                self.error(exc)
                self.enabled = False

    def error(self, exc: Exception) -> None:
        self.errors += 1
        # Never persist exception messages: they can contain paths or payloads.
        self.last_error = type(exc).__name__

    def submit(self, row: dict) -> None:
        if not self.enabled or self.stopping.is_set():
            self.dropped += 1
            return
        try:
            self.queue.put_nowait({
                "schema_version": SCHEMA_VERSION, "run_id": self.run_id,
                "ts": utc_now(), **row,
            })
        except queue.Full:
            self.dropped += 1

    def status(self) -> dict:
        return {
            "enabled": self.enabled, "writer_alive": bool(self.thread and self.thread.is_alive()),
            "queued": self.queue.qsize(), "written": self.written, "dropped": self.dropped,
            "errors": self.errors, "last_error": self.last_error, "last_write": self.last_write,
            "run_id": self.run_id, "schema_version": SCHEMA_VERSION,
            "keep_days": KEEP_DAYS, "max_total_bytes": MAX_TOTAL_BYTES,
        }

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=3)

    def _owned_files(self) -> list[Path]:
        return sorted(
            p for p in self.directory.iterdir()
            if FILE_PATTERN.fullmatch(p.name) and p.is_file() and not p.is_symlink()
        )

    def _prune(self, incoming: int = 0) -> None:
        # Refuse redirected directories rather than pruning an unexpected target.
        if self.directory.resolve() != self.directory:
            raise OSError("redirected learning directory")
        files = self._owned_files()
        total = sum(
            max(p.stat().st_size, self._size) if p == self._file else p.stat().st_size
            for p in files
        )
        cutoff = time.time() - KEEP_DAYS * 86400
        for path in files:
            stat = path.stat()
            if path == self._file:
                continue
            if stat.st_mtime < cutoff or total + incoming > MAX_TOTAL_BYTES:
                if path.resolve().parent != self.directory:
                    raise OSError("unexpected learning file")
                path.unlink()
                total -= stat.st_size

    def _write(self, batch: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.resolve() != self.directory:
            raise OSError("redirected learning directory")
        lines = []
        for row in batch:
            data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(data) > min(MAX_RECORD_BYTES, MAX_FILE_BYTES, MAX_TOTAL_BYTES):
                self.dropped += 1
            else:
                lines.append(data)
        while lines:
            data = lines.pop(0)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            if (self._file is None or self._size + len(data) > MAX_FILE_BYTES
                    or self._day != stamp[:8]):
                self._file = self.directory / f"events-{stamp}-{uuid.uuid4().hex}.jsonl"
                self._day = stamp[:8]
                self._size = 0
            self._prune(len(data))
            if self._file.is_symlink() or self._file.resolve().parent != self.directory:
                raise OSError("redirected learning file")
            # A batch is already off the event loop. Closing flushes the file;
            # a forced process exit can still lose the OS cache or a partial line.
            written = 0
            with self._file.open("ab") as stream:
                stream.write(data)
                self._size += len(data)
                written += 1
                # Coalesce the rest of this batch into the same buffered write.
                while lines and self._size + len(lines[0]) <= MAX_FILE_BYTES:
                    data = lines.pop(0)
                    self._prune(len(data))
                    stream.write(data)
                    self._size += len(data)
                    written += 1
            self.written += written
            self.last_write = utc_now()

    def _run(self) -> None:
        while not self.stopping.is_set() or not self.queue.empty():
            batch = []
            try:
                try:
                    batch.append(self.queue.get(timeout=0.25))
                except queue.Empty:
                    pass
                while len(batch) < 32:
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                if batch:
                    self._write(batch)
                if time.monotonic() - self._maintenance_at >= 60:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    # No active file handle; old current shard may also expire.
                    if self._file and self._file.stat().st_mtime < time.time() - KEEP_DAYS * 86400:
                        self._file = None
                    self._prune()
                    self._write([self._status_row()])
                    self._maintenance_at = time.monotonic()
            except Exception as exc:
                self.error(exc)
                self.dropped += len(batch)  # Conservative: a partial batch may have reached disk.
                self._file = None
                self._size = 0
                self.stopping.wait(1)
            finally:
                for _ in batch:
                    self.queue.task_done()
        try:
            self._write([self._status_row()])
        except Exception as exc:
            self.error(exc)

    def _status_row(self) -> dict:
        return {
            "kind": "collector_status", "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id, "ts": utc_now(), **self.status(),
        }


def best_effort(fn):
    @wraps(fn)
    def wrapped(self, *args, **kwargs):
        if self.collector is None:
            return None
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:
            self.collector.error(exc)
            return None
    return wrapped


class Trace:
    def __init__(self, collector: Collector | None):
        self.collector = collector
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.attempt: dict | None = None
        self.attempt_seq = 0
        self.finished = False
        self.result_status: int | None = None
        self.result_note: str | None = None
        self.response_started_ms: int | None = None
        self.response_status: int | None = None
        self.downstream_bytes = 0
        self.response_body_finished = False
        self.windows: deque = deque(maxlen=MAX_WINDOWS)

    def ms(self) -> int:
        return max(0, round((time.monotonic() - self.started) * 1000))

    def emit(self, kind: str, **fields) -> None:
        self.collector.submit({"kind": kind, "request_id": self.id, "t_ms": self.ms(), **fields})

    @best_effort
    def start(self, *, protocol, endpoint, model, stream, req_bytes, stateful,
              can_failover, stall_s, candidate_ids, start_deadline_s, client) -> None:
        self.emit(
            "request_start", protocol=protocol, endpoint=endpoint, model=model[:128],
            stream=stream, req_bytes=req_bytes, stateful=stateful,
            client=client if client in {"Codex Desktop", "Codex CLI", "Claude Code"} else "other",
            policy_version=POLICY_VERSION, can_failover=can_failover,
            stall_timeout_s=stall_s, start_deadline_s=start_deadline_s,
            candidate_ids=candidate_ids[:64], candidate_count=len(candidate_ids),
        )

    @best_effort
    def start_attempt(self, *, route_id, upstream_id, group_id, remote_model,
                      candidate_attempt, same_retries, req_bytes) -> None:
        if self.attempt is not None:
            self.end_attempt(status=None, note="unobserved", action="unknown")
        self.attempt_seq += 1
        peers = self.collector.active_groups.get(group_id, 0)
        self.collector.active_groups[group_id] = peers + 1
        self.attempt = {
            "attempt_id": f"{self.id}:{self.attempt_seq}", "attempt_seq": self.attempt_seq,
            "route_id": route_id, "upstream_id": upstream_id, "group_id": group_id,
            "remote_model": remote_model[:128], "candidate_attempt": candidate_attempt,
            "same_retries": same_retries, "req_bytes": req_bytes,
            "start_ms": self.ms(), "active_group_peers": peers,
            "headers_ms": None, "first_byte_ms": None, "first_content_ms": None,
            "last_content_ms": None, "max_content_gap_ms": None,
            "recovered_gaps_ge_5s": 0,
            "resp_bytes": 0, "content_events": 0, "event_counts": {},
            "windows_dropped": 0, "completion_seen": False, "protocol_error_seen": False,
            "observer_oversized_frames": 0,
        }
        self.windows.clear()
        self.emit("attempt_start", **{**self.attempt, "event_counts": {}})

    @best_effort
    def headers(self, status: int, is_sse: bool) -> None:
        self.attempt["headers_ms"] = self.ms()
        self.attempt["http_status"] = status
        self.attempt["is_sse"] = is_sse

    @best_effort
    def chunk(self, size: int) -> None:
        if size:
            if self.attempt["first_byte_ms"] is None:
                self.attempt["first_byte_ms"] = self.ms()
            self.attempt["resp_bytes"] += size
            if not self.attempt.get("is_sse", False):
                self.event("body")

    @best_effort
    def event(self, kind: str, text_bytes: int = 0) -> None:
        if kind not in EVENT_KINDS or self.attempt is None:
            return
        at = self.ms()
        counts = self.attempt["event_counts"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind in CONTENT_KINDS:
            self.attempt["content_events"] += 1
            previous = self.attempt["last_content_ms"]
            if previous is not None:
                self.attempt["max_content_gap_ms"] = max(
                    self.attempt["max_content_gap_ms"] or 0, at - previous,
                )
                if at - previous >= 5000:
                    self.attempt["recovered_gaps_ge_5s"] += 1
            if self.attempt["first_content_ms"] is None:
                self.attempt["first_content_ms"] = at
            self.attempt["last_content_ms"] = at
        if kind == "completion":
            self.attempt["completion_seen"] = True
        if kind == "protocol_error":
            self.attempt["protocol_error_seen"] = True
        # Adjacent same-kind events only. Keep first AND last time so a long
        # pause is not invented by coalescing; a heartbeat never counts as content.
        if (self.windows and self.windows[-1]["kind"] == kind
                and at - self.windows[-1]["first_ms"] < WINDOW_MS):
            window = self.windows[-1]
            window["last_ms"] = at
            window["count"] += 1
            window["text_bytes"] += text_bytes
        else:
            if len(self.windows) == MAX_WINDOWS:
                self.attempt["windows_dropped"] += 1
            self.windows.append({
                "kind": kind, "first_ms": at, "last_ms": at,
                "count": 1, "text_bytes": text_bytes,
            })

    @best_effort
    def observe_end(self, *, ended, oversized_frames, text_bytes) -> None:
        self.attempt["completion_seen"] = bool(ended)
        self.attempt["observer_oversized_frames"] = oversized_frames
        self.attempt["resp_text_bytes"] = text_bytes

    @best_effort
    def end_attempt(self, *, status, note, action="return", delay_s=None, usage=None) -> None:
        if self.attempt is None:
            return
        row = self.attempt
        self.attempt = None
        row["http_status"] = status if status is not None else row.get("http_status")
        group_id = row["group_id"]
        peers = self.collector.active_groups.get(group_id, 1) - 1
        if peers > 0:
            self.collector.active_groups[group_id] = peers
        else:
            self.collector.active_groups.pop(group_id, None)
        at = self.ms()
        last = row["last_content_ms"]
        censored = note in {"manual_abort", "client_abort", "stall_timeout", "unobserved", "internal_error"}
        if censored and action == "return":
            action = "stop"
        self.emit(
            "attempt_end", **row, end_ms=at, note=note, action=action,
            planned_delay_ms=None if delay_s is None else round(delay_s * 1000),
            tail_silence_ms=None if last is None else at - last,
            interrupted=censored, manual_reason="unknown" if note == "manual_abort" else None,
            windows=list(self.windows), usage=list(usage) if usage is not None else None,
        )

    @best_effort
    def result(self, status: int, note: str) -> None:
        self.result_status, self.result_note = status, note

    @best_effort
    def downstream(self, message: dict) -> None:
        if message["type"] == "http.response.start":
            self.response_started_ms = self.ms()
            self.response_status = message["status"]
        elif message["type"] == "http.response.body":
            self.downstream_bytes += len(message.get("body", b""))
            self.response_body_finished = not message.get("more_body", False)

    @best_effort
    def finish(self, fallback: str = "unobserved") -> None:
        if self.finished:
            return
        self.finished = True
        note = self.result_note or fallback
        self.end_attempt(status=self.result_status, note=note)
        self.emit(
            "request_end", status=self.result_status, note=note,
            attempt_count=self.attempt_seq, response_started_ms=self.response_started_ms,
            response_status=self.response_status,
            downstream_bytes=self.downstream_bytes,
            response_body_finished=self.response_body_finished,
            delivery_note="returned" if fallback == "unobserved" else fallback,
        )


def begin(scope: dict, **fields) -> Trace:
    collector = getattr(scope["app"].state, "learning", None)
    trace = Trace(collector if collector and collector.enabled else None)
    if trace.collector:
        scope["learning_trace"] = trace
        trace.start(**fields)
    return trace


class Lifecycle:
    """Always end a trace, including cancellation before relay's first iteration."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def observed_send(message):
            await send(message)
            trace = scope.get("learning_trace")
            if trace:
                trace.downstream(message)

        fallback = "unobserved"
        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            fallback = "client_abort"
            raise
        except Exception:
            fallback = "internal_error"
            raise
        finally:
            trace = scope.get("learning_trace")
            if trace:
                trace.finish(fallback)


def enabled_from_env() -> bool:
    return os.environ.get("MODEL_GATEWAY_LEARNING", "1").strip().lower() not in {"0", "off", "false"}
