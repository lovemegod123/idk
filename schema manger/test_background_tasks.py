"""Offline lifecycle tests for session-owned background processes."""
import asyncio
import os
import tempfile
import time

import background_tasks
from background_tasks import BackgroundTaskManager


class MemoryStore:
    def __init__(self, path):
        self.path = path
        self.records = {}
        self.events = {}
        self.next_event_id = 1

    def list_process_tasks(self, session_id, project_root, limit=100):
        return [
            dict(record) for (stored_session, _task_id), record in self.records.items()
            if stored_session == session_id and record.get("project_root") == project_root
        ]

    def save_process_task(self, session_id, project_root, record):
        record = {**record, "project_root": project_root}
        self.records[(session_id, record["task_id"])] = dict(record)

    def get_process_task(self, session_id, project_root, task_id):
        record = self.records.get((session_id, task_id))
        if record is None or record.get("project_root") != project_root:
            return None
        return dict(record)

    def delete_process_task(self, session_id, project_root, task_id):
        record = self.records.get((session_id, task_id))
        if record is not None and record.get("project_root") == project_root:
            self.records.pop((session_id, task_id), None)
        self.delete_process_events_for_task(session_id, project_root, task_id)

    def append_process_event(self, session_id, project_root, task_id, event):
        key = (session_id, project_root, task_id, event["event_id"])
        if key not in self.events:
            self.events[key] = (self.next_event_id, dict(event))
            self.next_event_id += 1
        return self.events[key][0]

    def list_pending_process_events(self, session_id, project_root, *, after_id=0, limit=20):
        rows = [
            {"id": row_id, "event": dict(event)}
            for (stored_session, stored_root, _task_id, _event_id), (row_id, event)
            in self.events.items()
            if stored_session == session_id and stored_root == project_root and row_id > after_id
        ]
        return sorted(rows, key=lambda row: row["id"])[:limit]

    def acknowledge_process_event(self, session_id, project_root, event_id):
        self.events = {
            key: event for key, event in self.events.items()
            if key[:2] != (session_id, project_root) or key[3] != event_id
        }

    def delete_process_events_for_task(self, session_id, project_root, task_id):
        deleted = [
            key[3] for key in self.events
            if key[:3] == (session_id, project_root, task_id)
        ]
        self.events = {
            key: event for key, event in self.events.items()
            if key[:3] != (session_id, project_root, task_id)
        }
        return deleted


async def main():
    if os.name != "posix":
        print("⏭️ Background process tests currently require POSIX process groups")
        return

    with tempfile.TemporaryDirectory() as root:
        store = MemoryStore(os.path.join(root, "agent.db"))
        events = []
        manager = BackgroundTaskManager(
            store, "session-a", root, on_complete=events.append,
        )

        task = await manager.start(
            "printf 'hello background\\n'",
            timeout_ms=2_000,
            description="background output test",
        )
        assert task["status"] == "running"
        completed = await manager.wait(task["task_id"])
        assert completed["status"] == "completed", completed
        assert completed["exit_code"] == 0
        with open(completed["output_file"], encoding="utf-8") as output:
            assert output.read() == "hello background\n"
        assert len(events) == 1
        assert events[0]["untrusted"] is True
        assert events[0]["event_id"] == f"{task['task_id']}:completed"
        print("✅ Background task completion persists bounded file output")

        replayed_events = []
        replay_manager = BackgroundTaskManager(
            store, "session-a", root, on_complete=replayed_events.append,
        )
        await asyncio.sleep(0)
        assert len(replayed_events) == 1
        replay_manager.acknowledge_completion(task["task_id"])
        acknowledged_events = []
        BackgroundTaskManager(
            store, "session-a", root, on_complete=acknowledged_events.append,
        )
        await asyncio.sleep(0)
        assert not acknowledged_events
        print("✅ Unacknowledged completion replays once and acknowledged events stay quiet")

        stop_task = await manager.start("sleep 10", timeout_ms=5_000)
        stopped = await manager.stop(stop_task["task_id"])
        assert stopped["status"] == "cancelled", stopped
        assert len(events) == 1, "user cancellation must suppress completion wake"
        print("✅ Stop terminates the owned process without completion notification")

        ephemeral = await manager.start(
            "sleep 0.1",
            timeout_ms=2_000,
            background_visible=False,
            notify_completion=False,
        )
        assert not any(
            task["task_id"] == ephemeral["task_id"]
            for task in manager.list_tasks()
        )
        manager.promote(ephemeral["task_id"])
        assert any(
            task["task_id"] == ephemeral["task_id"]
            for task in manager.list_tasks()
        )
        await manager.wait(ephemeral["task_id"])
        manager.forget(ephemeral["task_id"], delete_output=True)
        assert ("session-a", ephemeral["task_id"]) not in store.records
        print("✅ Foreground tasks promote to visible handles and clean up on completion")

        store.save_process_task("session-d", root, {
            "schema_version": "1.0",
            "task_id": "b87654321",
            "type": "local_bash",
            "status": "completed",
            "background_visible": False,
            "notify_completion": False,
        })
        hidden_events = []
        hidden_manager = BackgroundTaskManager(
            store, "session-d", root, on_complete=hidden_events.append,
        )
        await asyncio.sleep(0)
        assert not hidden_events
        assert not hidden_manager.list_tasks()
        print("✅ Unpromoted foreground tasks stay hidden and do not wake after restart")

        limited = BackgroundTaskManager(
            store,
            "session-b",
            root,
            max_output_bytes=8,
        )
        limited_task = await limited.start(
            "printf '0123456789'; sleep 2",
            timeout_ms=3_000,
        )
        limited_result = await limited.wait(limited_task["task_id"])
        assert limited_result["status"] == "failed", limited_result
        assert limited_result["output_truncated"] is True
        assert limited_result["output_bytes"] == 8
        with open(limited_result["output_file"], "rb") as output:
            assert output.read() == b"01234567"
        print("✅ Output cap terminates the process and keeps the bounded prefix")

        monitor_events = []
        monitor = BackgroundTaskManager(
            store, "session-monitor", root, on_monitor_event=monitor_events.append,
        )
        monitored = await monitor.start(
            "printf 'first event\\n'; printf 'second event\\n'",
            timeout_ms=2_000,
            task_type="monitor",
            on_output=monitor_events.append,
        )
        await monitor.wait(monitored["task_id"])
        assert [event["message"] for event in monitor_events] == [
            "first event", "second event",
        ]
        replayed_monitor_events = []
        monitor_restart = BackgroundTaskManager(
            store,
            "session-monitor",
            root,
            on_monitor_event=replayed_monitor_events.append,
        )
        assert [event["event_id"] for event in replayed_monitor_events] == [
            event["event_id"] for event in monitor_events
        ]
        for event in replayed_monitor_events:
            monitor_restart.acknowledge_monitor_event(event["event_id"])
        assert not store.list_pending_process_events("session-monitor", root)
        after_ack = []
        BackgroundTaskManager(
            store, "session-monitor", root, on_monitor_event=after_ack.append,
        )
        assert not after_ack
        print("✅ Monitor events replay after restart and stop replay after acknowledgement")

        previous_monitor_rate = background_tasks.MAX_MONITOR_EVENTS_PER_SECOND
        background_tasks.MAX_MONITOR_EVENTS_PER_SECOND = 100
        batched_events = []
        batched_monitor = BackgroundTaskManager(
            store,
            "session-monitor-batched",
            root,
            on_monitor_event=batched_events.append,
        )
        batched_task = await batched_monitor.start(
            "i=0; while [ \"$i\" -lt 25 ]; do printf 'event-%s\\n' \"$i\"; i=$((i + 1)); done",
            timeout_ms=2_000,
            task_type="monitor",
            on_output=batched_events.append,
        )
        await batched_monitor.wait(batched_task["task_id"])
        background_tasks.MAX_MONITOR_EVENTS_PER_SECOND = previous_monitor_rate
        assert len(batched_events) == 20
        for event in list(batched_events):
            batched_monitor.acknowledge_monitor_event(event["event_id"])
        assert len(batched_events) == 25
        for event in batched_events[20:]:
            batched_monitor.acknowledge_monitor_event(event["event_id"])
        assert not store.list_pending_process_events("session-monitor-batched", root)
        print("✅ Monitor outbox drains bounded pages without losing later events")

        queued_monitor_event = asyncio.Event()
        queued_monitor_events = []

        def capture_queued_event(event):
            queued_monitor_events.append(event)
            queued_monitor_event.set()

        cancellable_monitor = BackgroundTaskManager(
            store,
            "session-monitor-cancel",
            root,
            on_monitor_event=capture_queued_event,
        )
        running_monitor = await cancellable_monitor.start(
            "printf 'queued event\\n'; exec sleep 10",
            timeout_ms=5_000,
            task_type="monitor",
            on_output=capture_queued_event,
        )
        await asyncio.wait_for(queued_monitor_event.wait(), timeout=2)
        stopped_monitor = await cancellable_monitor.stop(running_monitor["task_id"])
        assert stopped_monitor["status"] == "cancelled"
        assert not store.list_pending_process_events("session-monitor-cancel", root)
        after_cancel_events = []
        BackgroundTaskManager(
            store,
            "session-monitor-cancel",
            root,
            on_monitor_event=after_cancel_events.append,
        )
        assert not after_cancel_events
        print("✅ Stopping a Monitor discards queued events and prevents a later wake")

        shutdown_event_ready = asyncio.Event()
        shutdown_events = []
        shutdown_completions = []

        def capture_shutdown_event(event):
            shutdown_events.append(event)
            shutdown_event_ready.set()

        shutdown_monitor = BackgroundTaskManager(
            store,
            "session-monitor-shutdown",
            root,
            on_complete=shutdown_completions.append,
            on_monitor_event=capture_shutdown_event,
        )
        shutdown_task = await shutdown_monitor.start(
            "printf 'preserve across shutdown\\n'; exec sleep 10",
            timeout_ms=10_000,
            task_type="monitor",
            on_output=capture_shutdown_event,
        )
        await asyncio.wait_for(shutdown_event_ready.wait(), timeout=2)
        await shutdown_monitor.close()
        replay_after_shutdown = []
        shutdown_restart = BackgroundTaskManager(
            store,
            "session-monitor-shutdown",
            root,
            on_complete=shutdown_completions.append,
            on_monitor_event=replay_after_shutdown.append,
        )
        assert [event["message"] for event in replay_after_shutdown] == [
            "preserve across shutdown",
        ]
        assert not shutdown_completions
        shutdown_restart.acknowledge_monitor_event(
            replay_after_shutdown[0]["event_id"],
        )
        print("✅ Shutdown preserves captured Monitor events without waking a cancelled monitor")

        stale_task_id = "b12345678"
        store.save_process_task("session-c", root, {
            "schema_version": "1.0",
            "task_id": stale_task_id,
            "type": "local_bash",
            "status": "running",
            "description": "stale process record",
            "owner_agent_id": None,
            "started_at": 1.0,
            "output_file": os.path.join(root, "missing.log"),
        })
        recovered = BackgroundTaskManager(store, "session-c", root)
        stale = recovered.list_tasks()[0]
        assert stale["status"] == "orphaned", stale
        assert "could not be verified" in stale["error"]
        print("✅ Restart marks unverifiable live records orphaned")

        retention_manager = BackgroundTaskManager(
            store, "session-retention", root,
        )
        retention_directory = retention_manager._output_directory()
        expired_output = os.path.join(retention_directory, "bexpired01.log")
        pending_output = os.path.join(retention_directory, "bpending01.log")
        for path in (expired_output, pending_output):
            with open(path, "w", encoding="utf-8") as output:
                output.write("retained test output")
        old_end = time.time() - 31 * 86_400
        common_record = {
            "schema_version": "1.0",
            "type": "local_bash",
            "status": "completed",
            "owner_agent_id": None,
            "ended_at": old_end,
            "background_visible": True,
            "notify_completion": True,
            "completion_suppressed": False,
        }
        store.save_process_task("session-retention", root, {
            **common_record,
            "task_id": "bexpired01",
            "output_file": expired_output,
            "completion_acknowledged": True,
        })
        store.save_process_task("session-retention", root, {
            **common_record,
            "task_id": "bpending01",
            "output_file": pending_output,
            "completion_acknowledged": False,
        })
        previous_retention = os.environ.get("AGENT_BACKGROUND_TASK_RETENTION_DAYS")
        os.environ["AGENT_BACKGROUND_TASK_RETENTION_DAYS"] = "30"
        try:
            pruned_manager = BackgroundTaskManager(
                store, "session-retention", root,
            )
            assert store.get_process_task("session-retention", root, "bexpired01") is None
            assert not os.path.exists(expired_output)
            assert store.get_process_task("session-retention", root, "bpending01") is not None
            assert os.path.exists(pending_output)
        finally:
            if previous_retention is None:
                os.environ.pop("AGENT_BACKGROUND_TASK_RETENTION_DAYS", None)
            else:
                os.environ["AGENT_BACKGROUND_TASK_RETENTION_DAYS"] = previous_retention
        print("✅ Retention prunes acknowledged old output but preserves unacknowledged records")

        await manager.close()
        await replay_manager.close()
        await limited.close()
        await recovered.close()
        await hidden_manager.close()
        await monitor.close()
        await monitor_restart.close()
        await batched_monitor.close()
        await cancellable_monitor.close()
        await retention_manager.close()
        await pruned_manager.close()
        await shutdown_restart.close()


asyncio.run(main())
