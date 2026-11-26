"""Offline tests for bounded Monitor command events and URL policy."""
import asyncio
import os
import tempfile
from unittest.mock import patch

import chainlit_app
from execute import execute_tool
from registry import clear, register
from state import AppState, FileStateCache, ToolPermissionContext
from store import Store
from tool import ToolUseContext
from tools.monitor import MonitorInput, MonitorTool, _resolve_public_websocket_target
from websockets.asyncio.server import serve


async def approve(_decision):
    return True


async def main():
    clear()
    register(MonitorTool)
    with tempfile.TemporaryDirectory() as root:
        state = AppState(
            messages=[],
            tool_permission_context=ToolPermissionContext(mode="bypassPermissions"),
            read_file_cache=FileStateCache(),
        )
        context = ToolUseContext(
            get_app_state=lambda: state,
            set_app_state=lambda _update: None,
            read_file_state=state.read_file_cache,
            project_root=root,
            session_id="monitor-test",
        )
        context._store = Store(os.path.join(root, "agent.db"))
        events = []
        context.on_background_monitor_event = events.append

        result = await execute_tool({
            "id": "monitor-1",
            "name": "Monitor",
            "input": {
                "command": "printf 'ready\\n'; sleep 0.1; printf 'done\\n'",
                "timeout_ms": 2_000,
                "description": "monitor test",
            },
        }, context, approve)
        assert not result.is_error, result.result_message.content
        task = context._background_task_manager.list_tasks()[0]
        assert task["type"] == "monitor"
        finished = await context._background_task_manager.wait(task["task_id"])
        assert finished["status"] == "completed", finished
        assert [event["message"] for event in events] == ["ready", "done"]
        assert all(event["untrusted"] for event in events)
        with open(finished["output_file"], encoding="utf-8") as output:
            assert output.read() == "ready\ndone\n"
        print("✅ Monitor streams bounded untrusted line events and retains output")

        async def websocket_handler(connection):
            await connection.send("websocket ready")
            await connection.close()

        events.clear()
        async with serve(websocket_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            ws_task = await context._background_task_manager.start_websocket_monitor(
                f"ws://127.0.0.1:{port}/events",
                resolved_host="127.0.0.1",
                port=port,
                hostname="127.0.0.1",
                timeout_ms=2_000,
                on_output=events.append,
            )
            ws_result = await context._background_task_manager.wait(ws_task["task_id"])
            assert ws_result["status"] == "completed", ws_result
            assert events[-1]["message"] == "websocket ready"
        print("✅ WebSocket monitor emits frames and completes on clean close")

        delivered_messages = []

        class FakeMessage:
            def __init__(self, content):
                self.content = content

            async def send(self):
                delivered_messages.append(self.content)

        websocket_event = events[-1]
        context._pending_monitor_events = [websocket_event]
        context._monitor_flush_scheduled = True
        with patch.dict(os.environ, {"AGENT_MONITOR_AUTO_WAKE": "0"}), \
             patch.object(chainlit_app.cl, "Message", FakeMessage):
            await chainlit_app._flush_monitor_events(context)
        assert delivered_messages
        assert not context._store.has_pending_process_event(
            context.session_id, root, websocket_event["event_id"],
        )
        print("✅ Chainlit surfaces and acknowledges a persisted Monitor event")

        for unsafe in (
            "ws://public.example/socket",
            "wss://user:pass@example.com/socket",
            "wss://example.com/socket?token=x",
        ):
            try:
                MonitorInput(ws_url=unsafe)
                raise AssertionError(f"unsafe WebSocket URL accepted: {unsafe}")
            except ValueError:
                pass
        try:
            _resolve_public_websocket_target("wss://127.0.0.1/socket")
            raise AssertionError("loopback WebSocket target accepted")
        except ValueError as error:
            assert "non-public" in str(error)
        print("✅ Monitor rejects insecure and non-public WebSocket targets")

        await context._background_task_manager.close()
        context._store.close()
    clear()


asyncio.run(main())
