"""Offline lifecycle tests for session-owned background processes."""
import asyncio
import os
import tempfile

from background_tasks import BackgroundTaskManager


class MemoryStore:
    def __init__(self, path):
        self.path = path
        self.records = {}

    def list_process_tasks(self, session_id, project_root):
        return [
            dict(record) for (stored_session, _task_id), record in self.records.items()
            if stored_session == session_id and record.get("project_root") == project_root
        ]

    def save_process_task(self, session_id, project_root, record):
        record = {**record, "project_root": project_root}
        self.records[(session_id, record["task_id"])] = dict(record)

    def delete_process_task(self, session_id, project_root, task_id):
        record = self.records.get((session_id, task_id))
        if record is not None and record.get("project_root") == project_root:
            self.records.pop((session_id, task_id), None)


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

        await manager.close()
        await replay_manager.close()
        await limited.close()
        await recovered.close()
        await hidden_manager.close()


asyncio.run(main())
