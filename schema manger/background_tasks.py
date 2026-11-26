"""Session-owned background process handles and bounded output capture."""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import websockets

from state import generate_task_id


DEFAULT_TASK_TIMEOUT_MS = 120_000
MAX_TASK_TIMEOUT_MS = 600_000
MAX_MONITOR_TIMEOUT_MS = 1_800_000
DEFAULT_TASK_RETENTION_DAYS = 30
MAX_TASK_RETENTION_DAYS = 3_650
MAX_TASK_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_SESSION_TASK_OUTPUT_BYTES = 200 * 1024 * 1024
MAX_MONITOR_EVENT_BYTES = 8 * 1024
MAX_MONITOR_FRAME_BYTES = 1024 * 1024
MAX_MONITOR_EVENTS_PER_SECOND = 20
MAX_MONITOR_EVENT_DELIVERY_BATCH = 20
MAX_ACTIVE_TASKS = 8


class BackgroundTaskError(ValueError):
    """A task request cannot be run or controlled in this context."""


class BackgroundTaskManager:
    """Own subprocesses for one session and persist their inspectable records."""

    def __init__(
        self,
        store: Any,
        session_id: str,
        project_root: str,
        *,
        owner_agent_id: str | None = None,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
        on_monitor_event: Callable[[dict[str, Any]], None] | None = None,
        max_active: int = MAX_ACTIVE_TASKS,
        max_output_bytes: int = MAX_TASK_OUTPUT_BYTES,
        max_session_output_bytes: int = MAX_SESSION_TASK_OUTPUT_BYTES,
    ) -> None:
        if not session_id:
            raise ValueError("Background tasks require a session ID")
        self.store = store
        self.session_id = session_id
        self.project_root = os.path.realpath(os.path.abspath(project_root))
        self.owner_agent_id = owner_agent_id
        self.on_complete = on_complete
        self.on_monitor_event = on_monitor_event
        self.max_active = max_active
        self.max_output_bytes = max_output_bytes
        self.max_session_output_bytes = max_session_output_bytes
        self._records: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        self._monitor_stops: dict[str, asyncio.Event] = {}
        self._output_callbacks: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._output_event_stats: dict[str, list[float | int]] = {}
        self._pending_output_reservations: dict[str, int] = {}
        self._cancel_requested: set[str] = set()
        self._monitor_event_cursor = 0
        self._monitor_event_page: set[str] = set()
        self._closed = False
        self._load_records()
        self.prune_expired()
        self._replay_monitor_events()

    def _load_records(self) -> None:
        for record in self.store.list_process_tasks(
            self.session_id, self.project_root, limit=None,
        ):
            task_id = str(record.get("task_id", ""))
            if not task_id:
                continue
            loaded = dict(record)
            if loaded.get("status") in {"queued", "running"}:
                loaded.update(
                    status="orphaned",
                    error="Process ownership could not be verified after restart.",
                    ended_at=time.time(),
                )
                self.store.save_process_task(
                    self.session_id, self.project_root, loaded,
                )
            self._records[task_id] = loaded
            if (
                loaded.get("status") in {
                    "completed", "failed", "timed_out", "cancelled",
                }
                and not loaded.get("completion_acknowledged")
                and not loaded.get("completion_suppressed")
                and loaded.get("background_visible", True)
                and loaded.get("notify_completion", True)
                and self.on_complete is not None
            ):
                asyncio.get_running_loop().call_soon(self._notify_completion, loaded)

    def _output_directory(self) -> str:
        store_path = getattr(self.store, "path", None)
        if not isinstance(store_path, str) or not store_path:
            raise BackgroundTaskError("Background tasks require a persistent Store")
        session_key = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:24]
        directory = f"{store_path}.processes/{session_key}"
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(os.path.dirname(directory), 0o700)
            os.chmod(directory, 0o700)
        except OSError:
            pass
        return directory

    def _save(self, record: dict[str, Any]) -> None:
        self._records[record["task_id"]] = record
        self.store.save_process_task(
            self.session_id, self.project_root, record,
        )

    def _deliver_monitor_event(self, event: dict[str, Any]) -> bool:
        if self.on_monitor_event is None:
            return False
        try:
            self.on_monitor_event(event)
            return True
        except Exception:
            return False

    def _replay_monitor_events(self) -> None:
        if (
            self._closed
            or self.on_monitor_event is None
            or self._monitor_event_page
        ):
            return
        try:
            rows = self.store.list_pending_process_events(
                self.session_id,
                self.project_root,
                after_id=self._monitor_event_cursor,
                limit=MAX_MONITOR_EVENT_DELIVERY_BATCH,
            )
        except (AttributeError, OSError, ValueError):
            return
        for row in rows:
            event = row.get("event")
            if not isinstance(event, dict) or not self._deliver_monitor_event(event):
                break
            self._monitor_event_cursor = int(row["id"])
            self._monitor_event_page.add(str(event["event_id"]))

    def acknowledge_monitor_event(self, event_id: str) -> None:
        if not event_id:
            return
        self.store.acknowledge_process_event(
            self.session_id, self.project_root, event_id,
        )
        self._monitor_event_page.discard(event_id)
        if not self._monitor_event_page:
            self._replay_monitor_events()

    def _session_output_bytes(self) -> int:
        total = sum(self._pending_output_reservations.values())
        for record in self._records.values():
            if record.get("status") in {"queued", "running"}:
                total += int(record.get("output_limit", self.max_output_bytes))
                continue
            try:
                total += os.path.getsize(record.get("output_file", ""))
            except OSError:
                continue
        return total

    @staticmethod
    def _retention_days() -> int:
        raw = os.environ.get("AGENT_BACKGROUND_TASK_RETENTION_DAYS")
        if raw is None:
            return DEFAULT_TASK_RETENTION_DAYS
        try:
            return max(0, min(int(raw), MAX_TASK_RETENTION_DAYS))
        except ValueError:
            return DEFAULT_TASK_RETENTION_DAYS

    def prune_expired(self, *, now: float | None = None) -> int:
        retention_days = self._retention_days()
        if retention_days == 0:
            return 0
        cutoff = (time.time() if now is None else now) - retention_days * 86_400
        expired = [
            task_id
            for task_id, record in self._records.items()
            if record.get("status") in {
                "completed", "failed", "timed_out", "cancelled", "orphaned",
            }
            and record.get("completion_acknowledged") is True
            and isinstance(record.get("ended_at"), (int, float))
            and record["ended_at"] <= cutoff
        ]
        for task_id in expired:
            record = self._records.get(task_id, {})
            self.forget(
                task_id,
                owner_agent_id=record.get("owner_agent_id"),
                delete_output=True,
            )
        return len(expired)

    def _reserve_output(self, task_id: str) -> int:
        remaining = self.max_session_output_bytes - self._session_output_bytes()
        if remaining <= 0:
            raise BackgroundTaskError(
                "Background task output quota is full; forget completed tasks to free space.",
            )
        limit = min(self.max_output_bytes, remaining)
        self._pending_output_reservations[task_id] = limit
        return limit

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "task_id", "type", "status", "description", "owner_agent_id",
            "pid", "started_at", "ended_at", "deadline", "exit_code",
            "error", "output_file", "output_bytes", "output_truncated",
            "completion_event_id", "completion_delivered",
            "completion_acknowledged",
        )
        return {key: record.get(key) for key in fields}

    async def start(
        self,
        command: str,
        *,
        timeout_ms: int | None = None,
        description: str | None = None,
        owner_agent_id: str | None = None,
        task_type: str = "local_bash",
        on_output: Callable[[dict[str, Any]], None] | None = None,
        background_visible: bool = True,
        notify_completion: bool = True,
    ) -> dict[str, Any]:
        if self._closed:
            raise BackgroundTaskError("This task manager is shutting down")
        self.prune_expired()
        if not isinstance(command, str) or not command.strip():
            raise BackgroundTaskError("Background command must not be empty")
        if task_type not in {"local_bash", "monitor"}:
            raise BackgroundTaskError("Unsupported background task type")
        timeout = timeout_ms or DEFAULT_TASK_TIMEOUT_MS
        timeout_limit = MAX_MONITOR_TIMEOUT_MS if task_type == "monitor" else MAX_TASK_TIMEOUT_MS
        if not isinstance(timeout, int) or timeout < 1 or timeout > timeout_limit:
            raise BackgroundTaskError(
                f"Background timeout must be between 1 and {timeout_limit} ms",
            )
        active_count = sum(
            record.get("status") in {"queued", "running"}
            for record in self._records.values()
        )
        if active_count >= self.max_active:
            raise BackgroundTaskError(
                f"This session already has {self.max_active} active background tasks",
            )
        if owner_agent_id is None:
            owner_agent_id = self.owner_agent_id

        task_id = generate_task_id(task_type)
        output_limit = self._reserve_output(task_id)
        output_file = os.path.join(self._output_directory(), f"{task_id}.log")
        try:
            output_fd = os.open(output_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(output_fd)
        except OSError:
            self._pending_output_reservations.pop(task_id, None)
            raise
        started = time.time()
        timeout_seconds = timeout / 1000
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
                **process_options,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self._pending_output_reservations.pop(task_id, None)
            try:
                os.unlink(output_file)
            except OSError:
                pass
            raise BackgroundTaskError(f"Could not start background command: {error}") from error

        record: dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": task_id,
            "type": task_type,
            "project_root": self.project_root,
            "status": "running",
            "description": (description or "Background shell command").strip()[:300],
            "owner_agent_id": owner_agent_id,
            "pid": process.pid,
            "process_started_at": started,
            "started_at": started,
            "ended_at": None,
            "deadline": started + timeout_seconds,
            "exit_code": None,
            "error": None,
            "output_file": output_file,
            "output_limit": output_limit,
            "output_bytes": 0,
            "output_truncated": False,
            "output_event_sequence": 0,
            "completion_event_id": f"{task_id}:completed",
            "completion_delivered": False,
            "completion_acknowledged": False,
            "completion_suppressed": False,
            "background_visible": background_visible,
            "notify_completion": notify_completion,
        }
        try:
            self._save(record)
        except Exception:
            self._pending_output_reservations.pop(task_id, None)
            await self._terminate(process)
            try:
                os.unlink(output_file)
            except OSError:
                pass
            raise
        self._pending_output_reservations.pop(task_id, None)

        self._processes[task_id] = process
        if on_output is not None:
            self._output_callbacks[task_id] = on_output
            self._output_event_stats[task_id] = [time.monotonic(), 0, 0]
        self._watchers[task_id] = asyncio.create_task(
            self._watch(task_id, process, timeout_seconds),
            name=f"background-task-{task_id}",
        )
        return self._public_record(record)

    async def start_websocket_monitor(
        self,
        url: str,
        *,
        resolved_host: str,
        port: int,
        hostname: str,
        timeout_ms: int,
        description: str | None = None,
        owner_agent_id: str | None = None,
        on_output: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise BackgroundTaskError("This task manager is shutting down")
        self.prune_expired()
        if not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= MAX_MONITOR_TIMEOUT_MS:
            raise BackgroundTaskError(
                f"Monitor timeout must be between 1 and {MAX_MONITOR_TIMEOUT_MS} ms",
            )
        if sum(
            record.get("status") in {"queued", "running"}
            for record in self._records.values()
        ) >= self.max_active:
            raise BackgroundTaskError(
                f"This session already has {self.max_active} active background tasks",
            )
        owner = self.owner_agent_id if owner_agent_id is None else owner_agent_id
        task_id = generate_task_id("monitor")
        output_limit = self._reserve_output(task_id)
        output_file = os.path.join(self._output_directory(), f"{task_id}.log")
        try:
            output_fd = os.open(output_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(output_fd)
        except OSError:
            self._pending_output_reservations.pop(task_id, None)
            raise
        started = time.time()
        record: dict[str, Any] = {
            "schema_version": "1.0",
            "task_id": task_id,
            "type": "monitor",
            "project_root": self.project_root,
            "status": "running",
            "description": (description or "WebSocket monitor").strip()[:300],
            "owner_agent_id": owner,
            "pid": None,
            "process_started_at": started,
            "started_at": started,
            "ended_at": None,
            "deadline": started + timeout_ms / 1000,
            "exit_code": None,
            "error": None,
            "output_file": output_file,
            "output_limit": output_limit,
            "output_bytes": 0,
            "output_truncated": False,
            "output_event_sequence": 0,
            "background_visible": True,
            "notify_completion": True,
            "completion_event_id": f"{task_id}:completed",
            "completion_delivered": False,
            "completion_acknowledged": False,
            "completion_suppressed": False,
        }
        try:
            self._save(record)
        except Exception:
            self._pending_output_reservations.pop(task_id, None)
            try:
                os.unlink(output_file)
            except OSError:
                pass
            raise
        self._pending_output_reservations.pop(task_id, None)
        self._monitor_stops[task_id] = asyncio.Event()
        if on_output is not None:
            self._output_callbacks[task_id] = on_output
            self._output_event_stats[task_id] = [time.monotonic(), 0, 0]
        self._watchers[task_id] = asyncio.create_task(
            self._watch_websocket(
                task_id, url, resolved_host, port, hostname, timeout_ms / 1000,
            ),
            name=f"background-monitor-{task_id}",
        )
        return self._public_record(record)

    async def _watch_websocket(
        self,
        task_id: str,
        url: str,
        resolved_host: str,
        port: int,
        hostname: str,
        timeout_seconds: float,
    ) -> None:
        record = self._records[task_id]
        stop_event = self._monitor_stops[task_id]
        status = "completed"
        error = None
        try:
            connection_options = {
                "open_timeout": min(10, timeout_seconds),
                "max_size": MAX_MONITOR_FRAME_BYTES,
                "proxy": None,
                "host": resolved_host,
                "port": port,
            }
            if url.startswith("wss://"):
                connection_options["server_hostname"] = hostname
            async with websockets.connect(
                url,
                **connection_options,
            ) as socket:
                while True:
                    remaining = record["deadline"] - time.time()
                    if remaining <= 0:
                        status, error = "timed_out", "Monitor exceeded its time limit."
                        break
                    receive = asyncio.create_task(socket.recv())
                    stopped = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        (receive, stopped),
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if not done:
                        status, error = "timed_out", "Monitor exceeded its time limit."
                        break
                    if stopped in done:
                        status = "cancelled"
                        break
                    message = receive.result()
                    if isinstance(message, bytes):
                        status, error = "failed", "Binary WebSocket events are not supported."
                        break
                    raw = str(message).encode("utf-8")
                    if len(raw) > MAX_MONITOR_FRAME_BYTES:
                        status, error = "failed", "WebSocket event exceeded the size limit."
                        break
                    if record["output_bytes"] + len(raw) + 1 > int(
                        record.get("output_limit", self.max_output_bytes),
                    ):
                        record["output_truncated"] = True
                        status, error = "failed", "Monitor exceeded the output limit."
                        break
                    with open(record["output_file"], "ab") as output:
                        output.write(raw + b"\n")
                    record["output_bytes"] += len(raw) + 1
                    self._emit_monitor_line(task_id, record, raw)
                    self._save(record)
        except websockets.exceptions.ConnectionClosedOK:
            status = "completed"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exception:
            status = "failed"
            error = type(exception).__name__
        finally:
            cancelled = status == "cancelled" or task_id in self._cancel_requested
            record.update(
                status="cancelled" if cancelled else status,
                ended_at=time.time(),
                error=None if cancelled else error,
                completion_suppressed=cancelled or self._closed,
            )
            self._save(record)
            self._monitor_stops.pop(task_id, None)
            self._watchers.pop(task_id, None)
            self._output_callbacks.pop(task_id, None)
            self._output_event_stats.pop(task_id, None)
            self._cancel_requested.discard(task_id)
            if not record["completion_suppressed"]:
                self._notify_completion(record)

    async def _watch(
        self,
        task_id: str,
        process: asyncio.subprocess.Process,
        timeout_seconds: float,
    ) -> None:
        record = self._records[task_id]
        overflow = asyncio.Event()
        reader = asyncio.create_task(
            self._capture_output(task_id, record, process, overflow),
        )
        wait_task = asyncio.create_task(process.wait())
        overflow_task = asyncio.create_task(overflow.wait())
        timed_out = False
        try:
            done, _ = await asyncio.wait(
                (wait_task, overflow_task),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task not in done:
                timed_out = not overflow.is_set()
                await self._terminate(process)
            elif overflow.is_set():
                await self._terminate(process)
            await asyncio.wait_for(reader, timeout=2)
        except asyncio.CancelledError:
            await self._terminate(process)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        except asyncio.TimeoutError:
            await self._terminate(process)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            record["output_truncated"] = True
        finally:
            for pending in (wait_task, overflow_task):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(wait_task, overflow_task, return_exceptions=True)
            self._processes.pop(task_id, None)
            self._watchers.pop(task_id, None)
            self._output_callbacks.pop(task_id, None)
            self._output_event_stats.pop(task_id, None)

        cancelled = task_id in self._cancel_requested
        if cancelled:
            status, error = "cancelled", None
        elif timed_out:
            status, error = "timed_out", "Background command exceeded its time limit."
        elif overflow.is_set():
            status, error = "failed", "Background command exceeded the output limit."
        elif process.returncode == 0:
            status, error = "completed", None
        else:
            status, error = "failed", f"Process exited with code {process.returncode}."

        record.update(
            status=status,
            ended_at=time.time(),
            exit_code=process.returncode,
            error=error,
            completion_suppressed=cancelled or self._closed,
        )
        self._save(record)
        self._cancel_requested.discard(task_id)
        if not record["completion_suppressed"]:
            event = {
                "event_id": record["completion_event_id"],
                "type": "process_completed",
                "session_id": self.session_id,
                "task_id": task_id,
                "owner_agent_id": record.get("owner_agent_id"),
                "status": status,
                "exit_code": process.returncode,
                "output_file": record["output_file"],
                "output_bytes": record["output_bytes"],
                "output_truncated": record["output_truncated"],
                "untrusted": True,
            }
            self._notify_completion(record, event)

    def _notify_completion(
        self,
        record: dict[str, Any],
        event: dict[str, Any] | None = None,
    ) -> None:
        if (
            self.on_complete is None
            or record.get("completion_suppressed")
            or not record.get("notify_completion", True)
        ):
            return
        if event is None:
            event = {
                "event_id": record.get("completion_event_id"),
                "type": "process_completed",
                "session_id": self.session_id,
                "task_id": record.get("task_id"),
                "owner_agent_id": record.get("owner_agent_id"),
                "status": record.get("status"),
                "exit_code": record.get("exit_code"),
                "output_file": record.get("output_file"),
                "output_bytes": record.get("output_bytes", 0),
                "output_truncated": record.get("output_truncated", False),
                "untrusted": True,
            }
        try:
            self.on_complete(event)
            record["completion_delivered"] = True
            self._save(record)
        except Exception:
            pass

    def acknowledge_completion(self, task_id: str) -> None:
        record = self._records.get(task_id)
        if record is None or record.get("completion_suppressed"):
            return
        record["completion_acknowledged"] = True
        self._save(record)

    def promote(self, task_id: str) -> dict[str, Any]:
        record = self._records.get(task_id)
        if record is None:
            raise BackgroundTaskError("Unknown task ID")
        record["background_visible"] = True
        record["notify_completion"] = True
        self._save(record)
        if (
            record.get("status") not in {"queued", "running"}
            and not record.get("completion_acknowledged")
        ):
            self._notify_completion(record)
        return self._public_record(record)

    def forget(
        self,
        task_id: str,
        *,
        owner_agent_id: str | None = None,
        delete_output: bool = False,
    ) -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        owner = self.owner_agent_id if owner_agent_id is None else owner_agent_id
        if record.get("owner_agent_id") != owner:
            raise BackgroundTaskError("This task is owned by a different agent")
        if record.get("status") in {"queued", "running"}:
            raise BackgroundTaskError("Cannot forget a running background task")
        self.store.delete_process_task(
            self.session_id, self.project_root, task_id,
        )
        self._records.pop(task_id, None)
        if delete_output:
            try:
                os.unlink(record.get("output_file", ""))
            except OSError:
                pass
    async def _capture_output(
        self,
        task_id: str,
        record: dict[str, Any],
        process: asyncio.subprocess.Process,
        overflow: asyncio.Event,
    ) -> None:
        if process.stdout is None:
            return
        remaining = int(record.get("output_limit", self.max_output_bytes))
        output_path = Path(record["output_file"])
        line_buffer = bytearray()
        with output_path.open("ab") as output:
            while True:
                chunk = await process.stdout.read(8192)
                if not chunk:
                    break
                kept = chunk[:remaining]
                if kept:
                    output.write(kept)
                    output.flush()
                    record["output_bytes"] += len(kept)
                    remaining -= len(kept)
                if len(kept) < len(chunk):
                    record["output_truncated"] = True
                    overflow.set()
                    break
                if record.get("type") == "monitor" or task_id in self._output_callbacks:
                    line_buffer.extend(chunk)
                    while b"\n" in line_buffer:
                        line, _, rest = line_buffer.partition(b"\n")
                        self._emit_monitor_line(task_id, record, line)
                        line_buffer = bytearray(rest)
                    while len(line_buffer) > MAX_MONITOR_EVENT_BYTES:
                        self._emit_monitor_line(
                            task_id,
                            record,
                            bytes(line_buffer[:MAX_MONITOR_EVENT_BYTES]),
                            truncated=True,
                        )
                        del line_buffer[:MAX_MONITOR_EVENT_BYTES]
            if line_buffer and (
                record.get("type") == "monitor"
                or task_id in self._output_callbacks
            ):
                self._emit_monitor_line(task_id, record, bytes(line_buffer))
        self._save(record)

    def _emit_monitor_line(
        self,
        task_id: str,
        record: dict[str, Any],
        raw_line: bytes,
        *,
        truncated: bool = False,
    ) -> None:
        callback = self._output_callbacks.get(task_id)
        if callback is None:
            return
        if len(raw_line) > MAX_MONITOR_EVENT_BYTES:
            raw_line = raw_line[:MAX_MONITOR_EVENT_BYTES]
            truncated = True
        now = time.monotonic()
        stats = self._output_event_stats[task_id]
        if now - float(stats[0]) >= 1:
            stats[0], stats[1] = now, 0
        if int(stats[1]) >= MAX_MONITOR_EVENTS_PER_SECOND:
            stats[2] = int(stats[2]) + 1
            return
        stats[1] = int(stats[1]) + 1
        sequence = int(record.get("output_event_sequence", 0)) + 1
        record["output_event_sequence"] = sequence
        event = {
            "event_id": f"{task_id}:output:{sequence}",
            "type": "monitor_output",
            "session_id": self.session_id,
            "task_id": task_id,
            "owner_agent_id": record.get("owner_agent_id"),
            "message": raw_line.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "dropped_before": int(stats[2]),
            "untrusted": True,
        }
        stats[2] = 0
        event_row_id = self.store.append_process_event(
            self.session_id, self.project_root, task_id, event,
        )
        if len(self._monitor_event_page) >= MAX_MONITOR_EVENT_DELIVERY_BATCH:
            return
        self._monitor_event_page.add(event["event_id"])
        try:
            callback(event)
        except Exception:
            self._monitor_event_page.discard(event["event_id"])
            self._replay_monitor_events()
            return
        self._monitor_event_cursor = max(self._monitor_event_cursor, event_row_id)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=3)
            except (OSError, asyncio.TimeoutError):
                if process.returncode is None:
                    process.kill()
            if process.returncode is None:
                await process.wait()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()

    async def wait(self, task_id: str) -> dict[str, Any]:
        watcher = self._watchers.get(task_id)
        if watcher is not None:
            await asyncio.shield(watcher)
        record = self._records.get(task_id)
        if record is None:
            raise BackgroundTaskError("Unknown task ID")
        return self._public_record(record)

    def list_tasks(
        self,
        *,
        owner_agent_id: str | None = None,
        include_children: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        owner = self.owner_agent_id if owner_agent_id is None else owner_agent_id
        records = list(self._records.values())
        if not include_children:
            records = [record for record in records if record.get("owner_agent_id") == owner]
        records = [
            record for record in records
            if record.get("background_visible", True)
            or record.get("status") == "orphaned"
        ]
        records.sort(key=lambda item: item.get("started_at", 0), reverse=True)
        return [self._public_record(record) for record in records[:max(1, min(limit, 100))]]
    async def stop(self, task_id: str, *, owner_agent_id: str | None = None) -> dict[str, Any]:
        record = self._records.get(task_id)
        if record is None:
            raise BackgroundTaskError("Unknown task ID")
        owner = self.owner_agent_id if owner_agent_id is None else owner_agent_id
        if record.get("owner_agent_id") != owner:
            raise BackgroundTaskError("This task is owned by a different agent")
        if record.get("status") not in {"queued", "running"}:
            return self._public_record(record)
        watcher = self._watchers.get(task_id)
        process = self._processes.get(task_id)
        self._cancel_requested.add(task_id)
        monitor_stop = self._monitor_stops.get(task_id)
        if monitor_stop is not None:
            monitor_stop.set()
        elif process is not None:
            await self._terminate(process)
        if watcher is not None:
            await asyncio.shield(watcher)
        if record.get("type") == "monitor":
            deleted_event_ids = self.store.delete_process_events_for_task(
                self.session_id, self.project_root, task_id,
            )
            self._monitor_event_page.difference_update(deleted_event_ids)
            if not self._monitor_event_page:
                self._replay_monitor_events()
        return self._public_record(record)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for stop_event in self._monitor_stops.values():
            stop_event.set()
        for task_id, process in list(self._processes.items()):
            self._cancel_requested.add(task_id)
            await self._terminate(process)
        if self._watchers:
            await asyncio.gather(*list(self._watchers.values()), return_exceptions=True)


def get_background_task_manager(context: Any) -> BackgroundTaskManager:
    store = getattr(context, "_store", None)
    session_id = getattr(context, "session_id", None)
    project_root = getattr(context, "project_root", None)
    if store is None or not session_id or not project_root:
        raise BackgroundTaskError(
            "Background tasks require a persistent session Store and project root",
        )
    canonical_root = os.path.realpath(os.path.abspath(project_root))
    owner_agent_id = getattr(context, "agent_id", None)
    key = (session_id, canonical_root, owner_agent_id)
    managers = getattr(context, "_background_task_managers", None)
    if managers is None:
        managers = {}
        context._background_task_managers = managers
    manager = managers.get(key)
    if manager is None:
        manager = BackgroundTaskManager(
            store,
            session_id,
            canonical_root,
            owner_agent_id=owner_agent_id,
            on_complete=getattr(context, "on_background_task_complete", None),
            on_monitor_event=getattr(context, "on_background_monitor_event", None),
        )
        managers[key] = manager
    context._background_task_manager = manager
    return manager


__all__ = [
    "BackgroundTaskError", "BackgroundTaskManager", "DEFAULT_TASK_TIMEOUT_MS",
    "MAX_ACTIVE_TASKS", "MAX_TASK_OUTPUT_BYTES", "MAX_TASK_TIMEOUT_MS",
    "get_background_task_manager",
]
