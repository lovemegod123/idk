"""Permission-aware inspection and cancellation for background tasks."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from background_tasks import BackgroundTaskError, get_background_task_manager
from permission_types import (
    OtherReason,
    PermissionAllowDecision,
    PermissionAskDecision,
)
from tool import ToolResult, ToolUseContext, build_tool


class TaskListInput(BaseModel):
    limit: int = Field(default=30, ge=1, le=100)
    include_completed: bool = True


class TaskStopInput(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)


class TaskForgetInput(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)


def _map_result(data: dict[str, Any], tool_use_id: str) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(data, ensure_ascii=True, sort_keys=True),
        "is_error": bool(data.get("is_error", False)),
    }


async def _list_tasks(
    input: TaskListInput,
    context: ToolUseContext,
    _can_use_tool=None,
    _parent_message=None,
    _on_progress=None,
) -> ToolResult:
    manager = get_background_task_manager(context)
    tasks = manager.list_tasks(limit=input.limit)
    if not input.include_completed:
        tasks = [task for task in tasks if task.get("status") in {"queued", "running"}]
    return ToolResult(data={"tasks": tasks})


async def _stop_task(
    input: TaskStopInput,
    context: ToolUseContext,
    _can_use_tool=None,
    _parent_message=None,
    _on_progress=None,
) -> ToolResult:
    try:
        task = await get_background_task_manager(context).stop(
            input.task_id,
            owner_agent_id=getattr(context, "agent_id", None),
        )
    except BackgroundTaskError as error:
        return ToolResult(data={"is_error": True, "error": str(error)})
    return ToolResult(data={"task": task})


async def _forget_task(
    input: TaskForgetInput,
    context: ToolUseContext,
    _can_use_tool=None,
    _parent_message=None,
    _on_progress=None,
) -> ToolResult:
    try:
        manager = get_background_task_manager(context)
        manager.forget(
            input.task_id,
            owner_agent_id=getattr(context, "agent_id", None),
            delete_output=True,
        )
    except BackgroundTaskError as error:
        return ToolResult(data={"is_error": True, "error": str(error)})
    return ToolResult(data={
        "forgotten_task_id": input.task_id,
        "output_deleted": True,
    })


async def _allow_list(input: TaskListInput, _context: ToolUseContext):
    return PermissionAllowDecision(updated_input=input.model_dump())


async def _ask_to_stop(input: TaskStopInput, _context: ToolUseContext):
    return PermissionAskDecision(
        message=f"Stop background task {input.task_id}? Its process group will be terminated.",
        decision_reason=OtherReason(reason="background_task_stop"),
        updated_input=input.model_dump(),
    )


async def _ask_to_forget(input: TaskForgetInput, _context: ToolUseContext):
    return PermissionAskDecision(
        message=(
            f"Forget completed task {input.task_id} and permanently delete its output file?"
        ),
        decision_reason=OtherReason(reason="background_task_forget"),
        updated_input=input.model_dump(),
    )


TaskListTool = build_tool(
    name="TaskList",
    description="List background tasks owned by this agent session",
    prompt="Inspect running and completed background process handles.",
    input_schema=TaskListInput,
    call=_list_tasks,
    map_tool_result_to_tool_result_block_param=_map_result,
    render_tool_use_message=lambda _input, _options: "TaskList",
    max_result_size_chars=12_000,
    is_read_only=lambda _input: True,
    is_concurrency_safe=lambda _input: True,
    check_permissions=_allow_list,
)

TaskStopTool = build_tool(
    name="TaskStop",
    description="Stop a background task owned by this agent session",
    prompt="Stop a background process by its exact task ID after user approval.",
    input_schema=TaskStopInput,
    call=_stop_task,
    map_tool_result_to_tool_result_block_param=_map_result,
    render_tool_use_message=lambda input, _options: f"TaskStop({input.get('task_id', '')})",
    max_result_size_chars=4_000,
    is_read_only=lambda _input: False,
    is_concurrency_safe=lambda _input: False,
    check_permissions=_ask_to_stop,
    requires_user_interaction=lambda: True,
)

TaskForgetTool = build_tool(
    name="TaskForget",
    description="Delete a completed background task record and its output file",
    prompt="Use this only after task output is no longer needed; it permanently deletes the log.",
    input_schema=TaskForgetInput,
    call=_forget_task,
    map_tool_result_to_tool_result_block_param=_map_result,
    render_tool_use_message=lambda input, _options: f"TaskForget({input.get('task_id', '')})",
    max_result_size_chars=2_000,
    is_read_only=lambda _input: False,
    is_concurrency_safe=lambda _input: False,
    check_permissions=_ask_to_forget,
    requires_user_interaction=lambda: True,
)


__all__ = [
    "TaskForgetInput", "TaskForgetTool", "TaskListInput", "TaskListTool",
    "TaskStopInput", "TaskStopTool",
]
