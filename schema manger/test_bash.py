"""test_bash.py — no network. Real commands in subprocesses."""
import asyncio
import os
import shutil
import tempfile

from registry import register, clear
from execute import execute_tool
from tools.bash import (
    BashTool, BashInput, is_read_only_command, is_destructive_command,
    split_compound_command, interpret_exit_code,
    DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, MAX_OUTPUT_LENGTH,
)
from state import AppState, FileStateCache, ToolPermissionContext
from store import Store
from tool import ToolUseContext


def make_ctx(mode="bypassPermissions"):
    state = AppState(
        messages=[],
        tool_permission_context=ToolPermissionContext(mode=mode),
        read_file_cache=FileStateCache(),
    )
    return ToolUseContext(
        get_app_state=lambda: state, set_app_state=lambda f: None,
        read_file_state=state.read_file_cache,
    )


async def _bash(ctx, command, on_progress=None, **kwargs):
    return await execute_tool(
        {"id": "tu", "name": "Bash",
         "input": {"command": command, **kwargs}},
        ctx, on_progress=on_progress,
    )


async def main():
    clear()
    register(BashTool)

    # ═══ 1. Compound split ═══
    assert split_compound_command("ls && pwd") == ["ls", "pwd"]
    assert split_compound_command("echo 'a && b'") == ["echo 'a && b'"]
    assert split_compound_command("ls; pwd | wc -l") == ["ls", "pwd", "wc -l"]
    print("✅ Compound split OK")

    # ═══ 2. is_read_only classifier ═══
    assert is_read_only_command("ls") is True
    assert is_read_only_command("ls -la /tmp") is True
    assert is_read_only_command("cat foo.py") is True
    assert is_read_only_command("grep -r pattern .") is True
    assert is_read_only_command("git status") is True
    assert is_read_only_command("git log --oneline") is True
    # Compound: ALL parts must be read-only
    assert is_read_only_command("ls && pwd") is True
    assert is_read_only_command("ls && rm foo") is False
    # git subcommand check
    assert is_read_only_command("git push origin main") is False
    assert is_read_only_command("git reset --hard") is False
    # Redirects imply writing
    assert is_read_only_command("ls > /tmp/out") is False
    print("✅ is_read_only classifier OK")

    # ═══ 2b. Heredoc content is not classified as a filesystem path ═══
    heredoc_input = BashInput(command=(
        "cat > api.md <<'EOF'\n"
        "Example contains ... in prose\n"
        "EOF"
    ))
    heredoc_decision = await BashTool.check_permissions(heredoc_input, make_ctx())
    assert heredoc_decision.behavior == "allow", heredoc_decision.message
    suspicious_path_input = BashInput(command="cat '.../api.md'")
    path_decision = await BashTool.check_permissions(suspicious_path_input, make_ctx())
    assert path_decision.behavior == "deny"
    print("✅ Heredoc path-safety classification OK")

    # ═══ 3. is_destructive ═══
    assert is_destructive_command("rm -rf build") is True
    assert is_destructive_command("ls") is False
    assert is_destructive_command("mv a b") is True
    print("✅ is_destructive OK")

    # ═══ 4. Exit code interpretation ═══
    assert interpret_exit_code("grep foo file", 1) == "exit 1: no matches found (not an error)"
    assert interpret_exit_code("grep foo file", 0) is None
    assert interpret_exit_code("ls", 2) == "exit 2"
    assert "differences present" in (interpret_exit_code("git diff", 1) or "")
    print("✅ interpret_exit_code OK")

    # ═══ 5. Simple command ═══
    ctx = make_ctx()
    res = await _bash(ctx, "echo hello")
    assert not res.is_error, res.result_message.content
    assert "hello" in res.result_message.content
    print("✅ Simple echo OK")

    # ═══ 5b. Background process handle + Read-compatible output file ═══
    with tempfile.TemporaryDirectory() as task_root:
        ctx = make_ctx()
        ctx.project_root = task_root
        ctx.session_id = "background-bash-test"
        ctx._store = Store(os.path.join(task_root, "agent.db"))
        res = await _bash(
            ctx,
            "printf 'detached output\\n'",
            run_in_background=True,
            description="background integration test",
        )
        assert not res.is_error, res.result_message.content
        manager = ctx._background_task_manager
        task = manager.list_tasks()[0]
        completed = await manager.wait(task["task_id"])
        assert completed["status"] == "completed", completed
        assert completed["output_file"] in res.result_message.content
        with open(completed["output_file"], encoding="utf-8") as output:
            assert output.read() == "detached output\n"
        await manager.close()
        ctx._store.close()
    print("✅ Background Bash returns a task handle and Read-compatible output")

    # ═══ 5c. Foreground command auto-promotes after configured grace ═══
    with tempfile.TemporaryDirectory() as task_root:
        previous_delay = os.environ.get("AGENT_BASH_AUTO_BACKGROUND_MS")
        os.environ["AGENT_BASH_AUTO_BACKGROUND_MS"] = "50"
        ctx = make_ctx()
        ctx.project_root = task_root
        ctx.session_id = "auto-background-test"
        ctx._store = Store(os.path.join(task_root, "agent.db"))
        try:
            res = await _bash(
                ctx,
                "python -c \"import time; time.sleep(0.25); print('promoted')\"",
                timeout=1_000,
            )
            assert not res.is_error, res.result_message.content
            assert "Background task" in res.result_message.content
            task = ctx._background_task_manager.list_tasks()[0]
            completed = await ctx._background_task_manager.wait(task["task_id"])
            assert completed["status"] == "completed", completed
            with open(completed["output_file"], encoding="utf-8") as output:
                assert "promoted" in output.read()
            await ctx._background_task_manager.close()
        finally:
            ctx._store.close()
            if previous_delay is None:
                os.environ.pop("AGENT_BASH_AUTO_BACKGROUND_MS", None)
            else:
                os.environ["AGENT_BASH_AUTO_BACKGROUND_MS"] = previous_delay
    print("✅ Long foreground Bash auto-promotes without changing its hard timeout")

    # ═══ 5d. Manual promotion request detaches the active foreground command ═══
    with tempfile.TemporaryDirectory() as task_root:
        previous_delay = os.environ.get("AGENT_BASH_AUTO_BACKGROUND_MS")
        os.environ["AGENT_BASH_AUTO_BACKGROUND_MS"] = "0"
        ctx = make_ctx()
        ctx.project_root = task_root
        ctx.session_id = "manual-background-test"
        ctx._store = Store(os.path.join(task_root, "agent.db"))
        try:
            foreground = asyncio.create_task(_bash(
                ctx,
                "python -c \"import time; time.sleep(0.25); print('manual')\"",
                timeout=1_000,
            ))
            for _ in range(200):
                if getattr(ctx, "_foreground_background_event", None) is not None:
                    break
                await asyncio.sleep(0)
            assert ctx._foreground_background_event is not None
            ctx._foreground_background_event.set()
            res = await asyncio.wait_for(foreground, timeout=1)
            assert not res.is_error, res.result_message.content
            assert "Background task" in res.result_message.content
            task = ctx._background_task_manager.list_tasks()[0]
            completed = await ctx._background_task_manager.wait(task["task_id"])
            assert completed["status"] == "completed", completed
            await ctx._background_task_manager.close()
        finally:
            ctx._store.close()
            if previous_delay is None:
                os.environ.pop("AGENT_BASH_AUTO_BACKGROUND_MS", None)
            else:
                os.environ["AGENT_BASH_AUTO_BACKGROUND_MS"] = previous_delay
    print("✅ Manual background request detaches the foreground command")

    # ═══ 6. Nonzero exit that's NOT an error (grep no matches) ═══
    tmp = tempfile.mkdtemp()
    empty = os.path.join(tmp, "empty.txt")
    open(empty, "w").close()
    ctx = make_ctx()
    res = await _bash(ctx, f"grep zzz {empty}")
    # grep 1 = no matches → is_error=False per semantics
    assert not res.is_error, res.result_message.content
    assert "not an error" in res.result_message.content
    print("✅ grep no-matches treated as non-error OK")

    ctx = make_ctx()
    res = await _bash(ctx, f'grep "not found" "{empty}"')
    assert not res.is_error, res.result_message.content
    print("✅ Path safety ignores command whitespace OK")

    # ═══ 7. Nonzero exit that IS an error ═══
    ctx = make_ctx()
    res = await _bash(ctx, "false")
    assert res.is_error
    assert "exit 1" in res.result_message.content.lower() or "Exit code" in res.result_message.content
    print("✅ genuine failure marked as error OK")

    # ═══ 8. stderr captured ═══
    ctx = make_ctx()
    res = await _bash(ctx, "echo out; echo err >&2; exit 1")
    assert "err" in res.result_message.content
    assert res.is_error
    print("✅ stderr captured OK")

    # ═══ 9. Timeout interrupts ═══
    ctx = make_ctx()
    res = await _bash(ctx, "sleep 5", timeout=200)
    assert res.is_error
    assert "interrupted" in res.result_message.content.lower()
    print("✅ Timeout interrupts OK")

    # ═══ 10. Abort signal interrupts a running process ═══
    class AbortController:
        def __init__(self):
            self.aborted = False
            self.event = asyncio.Event()

        async def wait(self):
            await self.event.wait()

        def abort(self):
            self.aborted = True
            self.event.set()

    ctx = make_ctx()
    abort_controller = AbortController()
    ctx.abort_controller = abort_controller
    task = asyncio.create_task(_bash(ctx, "sleep 5"))
    await asyncio.sleep(0.1)
    abort_controller.abort()
    res = await asyncio.wait_for(task, timeout=2)
    assert res.is_error
    assert "interrupted" in res.result_message.content.lower()
    print("✅ Abort signal interrupts running process OK")

    # ═══ 11. Output chunks reach the progress callback ═══
    progress = []
    ctx = make_ctx()
    res = await _bash(
        ctx, "printf 'first\\n'; sleep 0.1; printf 'second\\n'",
        on_progress=progress.append,
    )
    assert not res.is_error
    assert any("first" in item["output"] for item in progress)
    assert any("second" in item["output"] for item in progress)
    print("✅ Incremental output progress OK")

    # ═══ 12. Destructive path refused ═══
    ctx = make_ctx()
    res = await _bash(ctx, "rm -rf /")
    assert res.is_error
    assert "dangerous removal path" in res.result_message.content
    print("✅ Dangerous removal refused OK")

    # ═══ 13. Empty command rejected ═══
    ctx = make_ctx()
    res = await _bash(ctx, "")
    assert res.is_error
    assert "Empty command" in res.result_message.content
    print("✅ Empty command rejected OK")

    # ═══ 14. Timeout > max rejected ═══
    ctx = make_ctx()
    res = await _bash(ctx, "ls", timeout=MAX_TIMEOUT_MS + 1)
    assert res.is_error
    assert "exceeds the maximum" in res.result_message.content
    print("✅ Timeout > max rejected OK")

    # ═══ 15. Output truncation ═══
    ctx = make_ctx()
    # 40K chars of output > 30K limit
    res = await _bash(ctx, "python3 -c \"print('x' * 40000)\"")
    assert not res.is_error
    assert "output truncated" in res.result_message.content.lower()
    assert "Read to see more" in res.result_message.content
    print("✅ Output truncation OK")

    # ═══ 16. is_silent_command ═══
    from tools.bash import is_silent_command
    assert is_silent_command("mv a b") is True
    assert is_silent_command("echo hi") is False
    print("✅ is_silent_command OK")

    # ═══ 17. Tool metadata ═══
    assert BashTool.name == "Bash"
    assert BashTool.max_result_size_chars == 30_000
    assert BashTool.is_read_only(BashInput(command="ls")) is True
    assert BashTool.is_read_only(BashInput(command="rm foo")) is False
    assert BashTool.is_concurrency_safe(BashInput(command="ls")) is True
    assert BashTool.is_concurrency_safe(BashInput(command="rm foo")) is False
    assert BashTool.is_destructive(BashInput(command="rm foo")) is True
    assert BashTool.to_auto_classifier_input(BashInput(command="ls")) == "ls"
    print("✅ Tool metadata OK")

    # ═══ 18. Sandboxed workflow output never spills to the host ═══
    if shutil.which("bwrap"):
        with tempfile.TemporaryDirectory() as sandbox_root:
            large_path = os.path.join(sandbox_root, "large.txt")
            with open(large_path, "w", encoding="utf-8") as source:
                source.write("x" * (MAX_OUTPUT_LENGTH + 1))
            context = make_ctx()
            context.workflow_sandbox_root = sandbox_root
            result = await BashTool.call(
                BashInput(command='cat "$WORKFLOW_ROOT/large.txt"'),
                context,
            )
            assert result.data["truncated"] is True
            assert result.data["fullOutputPath"] is None
            assert "output truncated" in BashTool.map_tool_result_to_tool_result_block_param(
                result.data, "sandbox-large-output",
            )["content"].lower()
            assert os.path.getsize(large_path) == MAX_OUTPUT_LENGTH + 1
    print("✅ Sandboxed workflow output stays in memory OK")

    print("\n🎉 All bash.py tests passed")


asyncio.run(main())