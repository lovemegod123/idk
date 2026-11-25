"""Offline tests for TaskList and approval-gated TaskStop."""
import asyncio
import json
import os
import tempfile

from execute import execute_tool
from registry import clear, register
from state import AppState, FileStateCache, ToolPermissionContext
from store import Store
from tool import ToolUseContext
from tools.bash import BashTool
from tools.task_control import TaskListTool, TaskStopTool


async def approve(_decision):
    return True


async def main():
    clear()
    register(BashTool)
    register(TaskListTool)
    register(TaskStopTool)
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
            session_id="task-control-test",
        )
        context._store = Store(os.path.join(root, "agent.db"))

        launched = await execute_tool({
            "id": "bash-1",
            "name": "Bash",
            "input": {"command": "sleep 10", "run_in_background": True},
        }, context, approve)
        assert not launched.is_error, launched.result_message.content
        task = context._background_task_manager.list_tasks()[0]

        listed = await execute_tool({
            "id": "list-1",
            "name": "TaskList",
            "input": {"include_completed": False},
        }, context, approve)
        assert not listed.is_error
        listing = json.loads(listed.result_message.content)
        assert listing["tasks"][0]["task_id"] == task["task_id"]
        print("✅ TaskList returns owned active handles")

        stopped = await execute_tool({
            "id": "stop-1",
            "name": "TaskStop",
            "input": {"task_id": task["task_id"]},
        }, context, approve)
        assert not stopped.is_error, stopped.result_message.content
        assert json.loads(stopped.result_message.content)["task"]["status"] == "cancelled"
        print("✅ TaskStop requests approval and terminates the owned process")

        await context._background_task_manager.close()
        context._store.close()
    clear()


asyncio.run(main())
