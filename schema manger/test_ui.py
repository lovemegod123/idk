"""Headless smoke tests for the Textual agent UI."""
import asyncio
import os

os.environ["AGENT_STORE_PATH"] = ":memory:"

from textual.containers import VerticalScroll
from textual.widgets import Button, Input, OptionList, Select

import ui
from main import _persist_query_messages
from messages import ProgressMessage, ToolResultMessage
from permission_types import OtherReason
from registry import find
from query_transitions import Terminal
from ui import (
    AgentApp, AssistantBlock, Composer, McpActionButton, PermissionScreen,
    PlanPanel, StatusBar, StreamingMarkdown, SystemNotice, ToolCard, UserMessage,
    build_server_config, convert_server_spec, parse_mcp_json, validate_env_name,
    validate_remote_url, validate_server_name,
)


async def main():
    assert validate_server_name("github") == "github"
    assert validate_env_name("DOCS_API_KEY", "Bearer token variable") == "DOCS_API_KEY"
    for invalid_env in ("token-value", "ghp_example", "has spaces"):
        try:
            validate_env_name(invalid_env, "Bearer token variable")
            raise AssertionError("literal or malformed secret was accepted")
        except ValueError:
            pass
    assert validate_remote_url("https://mcp.example.test/mcp") == "https://mcp.example.test/mcp"
    try:
        validate_remote_url("https://mcp.example.test/sse")
        raise AssertionError("legacy SSE endpoint was accepted")
    except ValueError as error:
        assert "Streamable HTTP" in str(error)
    form_config = build_server_config(
        transport="streamable_http",
        endpoint="https://mcp.example.test/mcp",
        oauth=False,
        bearer_env="DOCS_API_KEY",
        env_headers_json='{"X-API-Key":"DOCS_API_KEY"}',
    )
    assert form_config["bearer_token_env_var"] == "DOCS_API_KEY"
    assert form_config["env_http_headers"] == {"X-API-Key": "DOCS_API_KEY"}
    imported = parse_mcp_json(
        '{"mcpServers":{"docs":{"type":"http","url":"https://mcp.example.test/mcp",'
        '"headers":{"Authorization":"Bearer ${DOCS_API_KEY}","X-Tenant":"team"}}}}',
    )
    assert imported[0][1]["bearer_token_env_var"] == "DOCS_API_KEY"
    assert imported[0][1]["http_headers"] == {"X-Tenant": "team"}
    assert convert_server_spec("local", {
        "type": "stdio", "command": ["uvx", "trusted-mcp"], "env": {"TOKEN": "${TOKEN}"},
    })["args"] == ["trusted-mcp"]

    app = AgentApp()
    async with app.run_test(size=(110, 32)) as pilot:
        assert app.query_one("#topbar")
        composer = app.query_one("#composer", Composer)
        assert app.query_one(StatusBar)
        assert app.query_one(PlanPanel)
        assert app.query_one("#activity-view")
        assert app.query_one("#runtime-view")
        assert app.query_one("#session-list", OptionList)
        await pilot.press("h", "i")
        assert composer.text == "hi"
        await pilot.press("ctrl+j")
        assert composer.text == "hi\n"
        composer.set_value("")
        composer.remember("previous command")
        composer.set_value("draft text")
        await pilot.press("up")
        assert composer.text == "previous command"
        await pilot.press("down")
        assert composer.text == "draft text"
        composer.set_value("/he")
        await pilot.pause()
        assert "/help" in composer.completions
        await pilot.press("tab")
        assert composer.text == "/help "
        composer.set_value("")
        assert find("Read") is not None
        assert find("Write") is not None
        assert find("Bash") is not None
        assert app.ctx._store.count_messages(app.ctx.session_id) == 0
        restored_id = "ui-restored-session"
        app.ctx._store.append_message(restored_id, "user", "Older task title")
        app.ctx._store.append_message(
            restored_id,
            "assistant",
            "**I will inspect the source.**\n\n- Preserve Markdown formatting",
            tool_calls=[
                {"id": "completed-tool", "name": "Read", "input": {"file_path": "src.py"}},
                {"id": "unfinished-tool", "name": "Grep", "input": {"pattern": "needle"}},
            ],
        )
        app.ctx._store.append_message(
            restored_id, "tool", "source contents", tool_use_id="completed-tool",
        )
        app.ctx._store.append_message("ui-other-session", "user", "Another task")
        await app._refresh_sessions("older task")
        sessions = app.query_one("#session-list", OptionList)
        assert sessions.option_count == 1
        assert sessions.get_option_at_index(0).id == restored_id
        await app._select_session(restored_id)
        assert app.ctx.session_id == restored_id
        assert any(
            isinstance(markdown, StreamingMarkdown)
            and markdown._buffer.startswith("**I will inspect")
            for block in app.query_one("#transcript").query(AssistantBlock)
            for markdown in block.query(StreamingMarkdown)
        )
        restored_messages = app.ctx.get_app_state().messages
        restored_calls = [
            call.id for message in restored_messages
            if getattr(message, "type", None) == "assistant"
            for call in getattr(message, "tool_calls", [])
        ]
        assert restored_calls == ["completed-tool", "unfinished-tool"]
        unfinished_result = next(
            message for message in restored_messages
            if getattr(message, "type", None) == "tool_result"
            and message.tool_use_id == "unfinished-tool"
        )
        assert unfinished_result.is_error
        assert "not re-executed" in unfinished_result.content
        restored_messages.append(ui.create_user_message("follow-up"))
        _persist_query_messages(app.ctx, restored_messages)
        assert app.ctx._store.count_messages(restored_id) == 4
        print("✅ Session filtering/restoration avoids replay and duplicate history")

        long_session_id = "ui-long-session"
        for index in range(225):
            app.ctx._store.append_message(
                long_session_id, "user", f"Historical message {index}",
            )
        await app._select_session(long_session_id)
        assert sum(
            getattr(message, "type", None) == "user"
            for message in app.ctx.get_app_state().messages
        ) == 225
        await pilot.pause()
        transcript = app.query_one("#transcript")
        assert transcript.scroll_y == transcript.max_scroll_y
        print("✅ Session switching restores histories over 200 messages")

        await app.action_new_session()
        assert app.ctx.session_id != restored_id
        assert app.ctx.get_app_state().messages == []
        assert app.ctx._store.count_messages(restored_id) == 4
        app._show_view("tools-view")
        assert app._active_view == "tools-view"
        assert app.query_one("#views").current == "tools-view"
        app._show_view("activity-view")
        assert app._active_view == "activity-view"
        assert app.query_one("#views").current == "activity-view"
        app._show_view("runtime-view")
        assert app._active_view == "runtime-view"
        assert app.query_one("#views").current == "runtime-view"
        assert app.query_one("#composer").display
        app._show_view("mcp-view")
        assert app._active_view == "mcp-view"
        assert app.query_one("#views").current == "mcp-view"
        assert app.query_one("#mcp-transport", Select).value == "stdio"
        assert app.query_one("#mcp-stdio-fields").display
        assert not app.query_one("#mcp-remote-fields").display
        app.query_one("#mcp-transport", Select).value = "streamable_http"
        app._apply_transport_visibility("streamable_http")
        assert not app.query_one("#mcp-stdio-fields").display
        assert app.query_one("#mcp-remote-fields").display
        assert app.query_one("#mcp-oauth-fields").display
        app._apply_oauth_visibility(False)
        assert app.query_one("#mcp-bearer-fields").display
        assert not app.query_one("#mcp-oauth-fields").display
        app.query_one("#mcp-transport", Select).value = "stdio"
        app._apply_transport_visibility("stdio")

        class FakeMCPManager:
            def __init__(self):
                self._connections = {
                    "calendar": type("Connection", (), {"tool_names": ["list_events"]})(),
                }
                self.connected_names = []
                self.servers = {
                    "calendar": {
                        "transport": "streamable_http",
                        "url": "https://calendar.example.test/mcp",
                    },
                    "tracker": {
                        "transport": "stdio",
                        "command": "trusted-tracker-server",
                        "args": ["--stdio"],
                    },
                }

            def configured_servers(self):
                return dict(self.servers)

            def save_server_config(self, name, config):
                self.servers[name] = config

            async def connect(self, names):
                self.connected_names.extend(names)
                for name in names:
                    self._connections[name] = type("Connection", (), {"tool_names": ["fake_tool"]})()

            async def disconnect_server(self, name):
                self._connections.pop(name, None)

            def remove_server_config(self, name):
                self.servers.pop(name, None)

            async def close(self):
                return None

        original_mcp_manager = app.ctx.mcp_manager
        app.ctx.mcp_manager = FakeMCPManager()
        await app._refresh_mcp_connections()
        await pilot.pause()
        assert len(app.query_one("#mcp-servers-list").query(".mcp-server-row")) == 2
        async def allow_connection(*_args):
            return True

        app._confirm_mcp_connection = allow_connection
        await app._mcp_action("connect", "tracker")
        assert app.ctx.mcp_manager.connected_names == ["tracker"]
        assert "tracker" in app.ctx.mcp_manager._connections
        await app._mcp_action("disconnect", "tracker")
        assert "tracker" not in app.ctx.mcp_manager._connections

        app.query_one("#mcp-name", Input).value = "local-test"
        app.query_one("#mcp-endpoint", Input).value = "trusted-server"
        app.query_one("#mcp-args", Input).value = '--label "two words"'
        await app._save_and_connect_mcp()
        assert app.ctx.mcp_manager.servers["local-test"]["args"] == [
            "--label", "two words",
        ]
        assert "local-test" in app.ctx.mcp_manager.connected_names
        app.ctx.mcp_manager = original_mcp_manager
        app._show_view("transcript")
        assert app._active_view == "transcript"
        assert app.query_one("#views").current == "transcript"
        assert app.query_one("#composer").display
        print("✅ New session and tool catalog views work")

        app._append_widget(UserMessage("hello from user"))
        streaming = StreamingMarkdown()
        app._append_widget(streaming)
        streaming.append_text("assistant response")
        streaming.finish()
        await pilot.pause()
        assert streaming._buffer == "assistant response"
        composer.set_value("/tools")
        await pilot.press("enter")
        await pilot.pause()
        assert app._active_view == "tools-view"

        card = ToolCard("tool-1", "Bash", "echo test")
        app._tool_cards["tool-1"] = card
        app._append_widget(card)
        await pilot.pause()
        await app._dispatch_event(ProgressMessage(
            tool_use_id="tool-1",
            parent_tool_use_id="",
            data={"type": "bash_progress", "output": "still running"},
        ))
        assert card._tc_progress == ["still running"]
        await app._dispatch_event(ProgressMessage(
            tool_use_id="task-tool",
            parent_tool_use_id="",
            data={
                "type": "task_lifecycle", "task_id": "inspect",
                "status": "succeeded", "needs": [], "duration_ms": 12,
            },
        ))
        assert any("Task inspect: succeeded" in notice.message
                   for notice in app.query(SystemNotice))
        assert any(
            "Task inspect: succeeded" in notice.message
            for notice in app.query_one("#activity-view").query(SystemNotice)
        )
        activity_session_id = app.ctx.session_id
        assert app._activity_entries[activity_session_id]
        await app._dispatch_event(ToolResultMessage(
            tool_use_id="tool-1", content="done", is_error=False,
        ))
        assert card._tc_state == "done"
        await app.action_new_session()
        assert not app.query_one("#activity-view").query(SystemNotice)
        assert app._activity_entries[activity_session_id]

        app.ctx._store.save_plan(app.ctx.session_id, "UI test", [
            {"content": "verify rendering", "status": "in_progress"},
        ], "verify rendering")
        app._refresh_plan()
        assert app.query_one(PlanPanel).title == "PLAN  0/1"

        await app._dispatch_event({
            "type": "runtime_activity", "area": "memory",
            "status": "injected", "source": "project_files", "count": 1,
            "names": ["Deployment Guide"],
        })
        assert any("Memory context injected" in notice.message
                   for notice in app.query(SystemNotice))
        await app._dispatch_event(ProgressMessage(
            tool_use_id="todo-tool",
            parent_tool_use_id="",
            data={
                "type": "plan_activity", "status": "updated",
                "item_count": 3, "completed_count": 1,
                "in_progress_count": 1, "cleared": False,
            },
        ))
        assert any("Plan updated: 3 items" in notice.message
                   for notice in app.query(SystemNotice))
        await app._dispatch_event({
            "type": "runtime_activity", "area": "provider",
            "status": "fallback", "model": "nvidia_nim/model-a",
            "alternate_model": "groq/model-b", "category": "provider_overloaded",
        })
        assert any("Provider fallback" in notice.message
                   for notice in app.query(SystemNotice))

        app._turn_had_visible_output = False
        await app._dispatch_event(Terminal(reason="completed"))
        assert any(
            "completed without a visible response" in notice.message
            for notice in app.query(SystemNotice)
        )

        first_permission = app.run_worker(
            app._can_use_tool(
                type("Decision", (), {
                    "message": "Allow the first worker?",
                    "updated_input": {"command": "git status"},
                })(),
            ),
            group="permission-test",
            exclusive=False,
        )
        await pilot.pause()
        assert isinstance(app.screen, PermissionScreen)
        app.screen.query_one("#allow", Button)
        app.screen.query_one("#deny", Button)
        app.screen.query_one("#permission-scroll", VerticalScroll)
        assert "Arguments to review" in app.screen._perm_message
        assert "git status" in app.screen._perm_message
        second_permission = app.run_worker(
            app._can_use_tool(
                type("Decision", (), {"message": "Allow the second worker?"})(),
            ),
            group="permission-test",
            exclusive=False,
        )
        await pilot.pause()
        assert isinstance(app.screen, PermissionScreen)
        await pilot.press("y")
        assert await first_permission.wait() is True
        await pilot.pause()
        assert isinstance(app.screen, PermissionScreen)
        app.screen.query_one("#allow", Button)
        app.screen.query_one("#deny", Button)
        await pilot.press("n")
        assert await second_permission.wait() is False
        await pilot.pause()
        assert app.query_one(StatusBar).activity == "Ready"

        app._set_auto_approve(True)
        app._always_allow.add("mcp__test__search")
        mcp_permission = app.run_worker(
            app._can_use_tool(type("Decision", (), {
                "message": "Call the external MCP tool?",
                "updated_input": {"query": "status"},
                "metadata": {"tool_name": "mcp__test__search"},
                "decision_reason": OtherReason(reason="external_mcp_tool"),
            })()),
            group="permission-test",
            exclusive=False,
        )
        await pilot.pause()
        assert isinstance(app.screen, PermissionScreen)
        assert not app.screen._perm_always
        await pilot.press("y")
        assert await mcp_permission.wait() is True
        app._set_auto_approve(False)

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not app.is_running
        await pilot.pause(0.1)
    query_started = asyncio.Event()
    query_closed = asyncio.Event()

    async def stalled_query(_params):
        query_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            query_closed.set()
            if False:
                yield None

    original_query = ui.query
    ui.query = stalled_query
    app = AgentApp()
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            app._start_turn("close the interface")
            await asyncio.wait_for(query_started.wait(), timeout=1)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert query_closed.is_set()
            assert app.is_running
    finally:
        ui.query = original_query

    async def scripted_response(params):
        tool_call = ui.create_assistant_message(tool_calls=[
            ui.ToolUseBlock(
                id="scripted-read", name="Read", input={"file_path": "src.py"},
            ),
        ])
        yield tool_call
        yield ui.create_tool_result("scripted-read", "source contents")
        response = ui.create_assistant_message("The reply should survive a session switch.")
        yield response
        yield Terminal(reason="completed")

    ui.query = scripted_response
    app = AgentApp()
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            original_session_id = app.ctx.session_id
            app._start_turn("hey")
            app._submit_text("queued follow-up")
            assert app._ui_queue == ["queued follow-up"]
            await app._worker.wait()
            await pilot.pause(0.1)
            assert not app._ui_queue
            assert sum(
                getattr(message, "type", None) == "user"
                for message in app.ctx.get_app_state().messages
            ) == 2
            assert any(
                getattr(message, "content", None) == "The reply should survive a session switch."
                for message in app.ctx.get_app_state().messages
            )
            await app.action_new_session()
            await app._select_session(original_session_id)
            assert any(
                getattr(message, "content", None) == "The reply should survive a session switch."
                for message in app.ctx.get_app_state().messages
            )
            assert any(
                isinstance(markdown, StreamingMarkdown)
                and markdown._buffer == "The reply should survive a session switch."
                for block in app.query_one("#transcript").query(AssistantBlock)
                for markdown in block.query(StreamingMarkdown)
            )
            assert any(
                isinstance(card, ToolCard) and card._tc_state == "done"
                for card in app.query_one("#transcript").query(ToolCard)
            )
    finally:
        ui.query = original_query
    print("Textual UI mounts with registered tools and session store")


if __name__ == "__main__":
    asyncio.run(main())
