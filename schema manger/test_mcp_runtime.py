"""Offline MCP lifecycle and permission tests using a fake stdio server."""
import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import httpx
import mcp_runtime
from mcp.client.auth.exceptions import OAuthRegistrationError
from execute import execute_tool
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from query import _tools_for_context
from registry import clear, find, register
from state import AppState, FileStateCache, ToolPermissionContext
from tool import ToolUseContext


class FakeSession:
    def __init__(self, _read_stream, _write_stream):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=[
            SimpleNamespace(
                name="create_issue",
                description="Create an issue in the project tracker.",
                inputSchema={
                    "type": "object",
                    "properties": {"title": {"type": "string"}},
                    "required": ["title"],
                    "additionalProperties": False,
                },
            ),
            SimpleNamespace(
                name="search_issues",
                description="Search issues in the project tracker.",
                inputSchema={"type": "object", "properties": {}},
            ),
            SimpleNamespace(
                name="list_projects",
                description="List available workspaces.",
                inputSchema={"type": "object", "properties": {}},
            ),
        ])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(
            content=[SimpleNamespace(text="created")],
            structuredContent={"ok": True},
            isError=False,
        )


class FailingInitSession(FakeSession):
    async def initialize(self):
        raise BaseExceptionGroup("simulated AnyIO transport failure", [
            asyncio.CancelledError("transport cancelled after OAuth failure"),
            OAuthRegistrationError("Registration failed: 404 404 page not found"),
        ])


def make_context(root: str) -> ToolUseContext:
    state = AppState(
        messages=[],
        tool_permission_context=ToolPermissionContext(mode="bypassPermissions"),
        read_file_cache=FileStateCache(),
    )
    return ToolUseContext(
        get_app_state=lambda: state,
        set_app_state=lambda _update: None,
        read_file_state=state.read_file_cache,
        project_root=root,
    )


async def main() -> None:
    dcr_error = mcp_runtime.format_connection_error(
        RuntimeError("Registration failed: 404 404 page not found"),
    )
    assert "Dynamic Client Registration" in dcr_error
    assert "client_metadata_url" in dcr_error

    assert mcp_runtime._validate_remote_url("https://mcp.example.test/mcp") == "https://mcp.example.test/mcp"
    assert mcp_runtime._validate_remote_url("http://127.0.0.1:8000/mcp") == "http://127.0.0.1:8000/mcp"
    for unsafe_url in (
        "http://mcp.example.test/mcp",
        "https://user:secret@mcp.example.test/mcp",
        "https://mcp.example.test/mcp?token=secret",
    ):
        try:
            mcp_runtime._validate_remote_url(unsafe_url)
            raise AssertionError(f"unsafe remote URL accepted: {unsafe_url}")
        except ValueError:
            pass

    provider = SimpleNamespace(context=SimpleNamespace(
        oauth_metadata=SimpleNamespace(issuer="https://login.example.test/"),
        auth_server_url="https://login.example.test/",
    ))
    mcp_runtime._validate_callback_issuer("https://login.example.test/", provider)
    try:
        mcp_runtime._validate_callback_issuer("https://attacker.example.test/", provider)
        raise AssertionError("mismatched OAuth issuer was accepted")
    except RuntimeError as error:
        assert "issuer did not match" in str(error)
    assert mcp_runtime._validate_redirect_uri(
        "https://agent.example.test/oauth/callback",
    ) == (None, None, "/oauth/callback")
    for invalid_redirect in (
        "http://agent.example.test/oauth/callback",
        "https://agent.example.test/other",
        "https://agent.example.test/oauth/callback?next=/",
    ):
        try:
            mcp_runtime._validate_redirect_uri(invalid_redirect)
            raise AssertionError(f"invalid OAuth redirect URI accepted: {invalid_redirect}")
        except ValueError:
            pass

    callback = mcp_runtime.LoopbackOAuthCallback()
    original_browser_open = mcp_runtime.webbrowser.open
    mcp_runtime.webbrowser.open = lambda _url: True
    try:
        await callback.start()
        await callback.redirect("https://login.example.test/authorize?state=expected-state")
        callback_port = urlsplit(callback.redirect_uri).port

        async def send_callback(parameters: dict[str, str]) -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", callback_port)
            query = urlencode(parameters)
            writer.write(
                f"GET /oauth/callback?{query} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{callback_port}\r\n\r\n".encode("ascii"),
            )
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            return response

        rejected = await send_callback({"code": "wrong", "state": "wrong-state"})
        assert b"400 Bad Request" in rejected
        accepted = await send_callback({
            "code": "auth-code",
            "state": "expected-state",
            "iss": "https://login.example.test/",
        })
        assert b"200 OK" in accepted
        result = await callback.wait_for_callback()
        assert (result.code, result.state, result.issuer) == (
            "auth-code", "expected-state", "https://login.example.test/",
        )
    finally:
        await callback.close()
        mcp_runtime.webbrowser.open = original_browser_open
    print("✅ OAuth callback validates loopback state and captures issuer")

    public_callback = mcp_runtime.LoopbackOAuthCallback(
        "https://agent.example.test/oauth/callback",
    )
    published_urls = []

    async def publish_authorization_url(url: str) -> None:
        published_urls.append(url)

    try:
        await public_callback.start()
        await public_callback.redirect(
            "https://login.example.test/authorize?state=public-state",
            redirect_handler=publish_authorization_url,
        )
        assert published_urls == [
            "https://login.example.test/authorize?state=public-state",
        ]
        rejected_status, _ = await mcp_runtime.LoopbackOAuthCallback.accept_public_callback({
            "code": ["wrong"],
            "state": ["wrong-state"],
        })
        assert rejected_status == 400
        import chainlit_server

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=chainlit_server.app),
            base_url="https://agent.example.test",
        ) as client:
            response = await client.get("/oauth/callback", params={
                "code": "auth-code",
                "state": "public-state",
                "iss": "https://login.example.test/",
            })
        assert response.status_code == 200
        assert response.text.startswith("Authorization complete")
        replay_status, _ = await mcp_runtime.LoopbackOAuthCallback.accept_public_callback({
            "code": ["replayed-code"],
            "state": ["public-state"],
        })
        assert replay_status == 400
        result = await public_callback.wait_for_callback()
        assert (result.code, result.state, result.issuer) == (
            "auth-code", "public-state", "https://login.example.test/",
        )
    finally:
        await public_callback.close()
    print("✅ Public OAuth callback validates state and delivers the authorization code")

    class FakeKeyring:
        values = {}

        def get_password(self, service, account):
            return self.values.get((service, account))

        def set_password(self, service, account, value):
            self.values[(service, account)] = value

    fake_keyring = FakeKeyring()
    original_keyring = mcp_runtime.KeyringTokenStorage._keyring
    mcp_runtime.KeyringTokenStorage._keyring = staticmethod(lambda: fake_keyring)
    try:
        storage = mcp_runtime.KeyringTokenStorage("test-server:test-endpoint")
        token = OAuthToken(access_token="access-token", refresh_token="refresh-token")
        client_info = OAuthClientInformationFull(
            client_id="client-id",
            redirect_uris=["http://127.0.0.1:12345/oauth/callback"],
        )
        await storage.set_tokens(token)
        await storage.set_client_info(client_info)
        assert await storage.get_tokens() == token
        assert await storage.get_client_info() == client_info
    finally:
        mcp_runtime.KeyringTokenStorage._keyring = staticmethod(original_keyring)
    print("✅ OAuth tokens and registration data round-trip through credential storage")

    try:
        mcp_runtime.MCPManager._validate_tool_schema({
            "type": "object",
            "properties": {"value": {"$ref": "https://example.invalid/schema"}},
        })
        raise AssertionError("remote schema references should be rejected")
    except ValueError as error:
        assert "Remote MCP schema references" in str(error)

    original_stdio_client = mcp_runtime.stdio_client
    original_http_client = mcp_runtime.streamable_http_client
    original_client_session = mcp_runtime.ClientSession
    fake_sessions = []

    @asynccontextmanager
    async def fake_stdio_client(_params):
        yield object(), object()

    @asynccontextmanager
    async def fake_http_client(url, *, http_client):
        assert url == "https://mcp.example.test/mcp"
        assert http_client is not None
        yield object(), object(), lambda: None

    def fake_client_session(read_stream, write_stream):
        session = FakeSession(read_stream, write_stream)
        fake_sessions.append(session)
        return session

    mcp_runtime.stdio_client = fake_stdio_client
    mcp_runtime.streamable_http_client = fake_http_client
    mcp_runtime.ClientSession = fake_client_session
    clear()
    try:
        with tempfile.TemporaryDirectory() as root:
            config_path = os.path.join(root, "mcp.json")
            with open(config_path, "w", encoding="utf-8") as target:
                json.dump({
                    "servers": {
                        "tracker": {
                            "description": "Issue tracking and pull requests.",
                            "command": "trusted-server",
                            "args": ["--stdio"],
                        },
                        "calendar": {
                            "description": "Calendar and scheduling.",
                            "command": "unused-server",
                            "args": [],
                        },
                    },
                }, target)

            os.environ["MCP_TEST_TOKEN"] = "test-token"
            assert mcp_runtime.MCPManager._server_environment({
                "TRACKER_TOKEN": "${MCP_TEST_TOKEN}",
            })["TRACKER_TOKEN"] == "test-token"
            assert "MCP_TEST_TOKEN" not in mcp_runtime.MCPManager._server_environment({})
            del os.environ["MCP_TEST_TOKEN"]

            remote_config_path = os.path.join(root, "remote-mcp.json")
            with open(remote_config_path, "w", encoding="utf-8") as target:
                json.dump({
                    "servers": {
                        "remote": {
                            "transport": "streamable_http",
                            "url": "https://mcp.example.test/mcp",
                            "oauth": False,
                        },
                    },
                }, target)
            remote_manager = mcp_runtime.MCPManager(remote_config_path)
            remote_manager.save_server_config("local-added", {
                "command": "trusted-server",
                "args": ["--stdio"],
            })
            saved_config = json.loads(open(remote_config_path, encoding="utf-8").read())
            assert "local-added" in saved_config["servers"]
            assert os.stat(remote_config_path).st_mode & 0o777 == 0o600
            remote_manager.remove_server_config("local-added")
            await remote_manager.connect(["remote"])
            assert remote_manager.connected
            assert "mcp__remote__create_issue" in remote_manager._tools
            await remote_manager.close()
            fake_sessions.clear()
            print("✅ Streamable HTTP connects through the existing tool adapter")

            oauth_config_path = os.path.join(root, "oauth-mcp.json")
            with open(oauth_config_path, "w", encoding="utf-8") as target:
                json.dump({
                    "servers": {
                        "oauth": {
                            "transport": "streamable_http",
                            "url": "https://mcp.example.test/mcp",
                        },
                    },
                }, target)
            oauth_manager = mcp_runtime.MCPManager(oauth_config_path)
            original_keyring = mcp_runtime.KeyringTokenStorage._keyring

            def unavailable_keyring():
                raise RuntimeError("OAuth requires an available OS credential-store backend")

            try:
                mcp_runtime.KeyringTokenStorage._keyring = staticmethod(unavailable_keyring)
                try:
                    await oauth_manager.connect(["oauth"])
                    raise AssertionError("OAuth connected without secure token storage")
                except RuntimeError as error:
                    assert "available OS credential-store backend" in str(error)
                assert not oauth_manager.connected
                assert not fake_sessions
                mcp_runtime.KeyringTokenStorage._keyring = staticmethod(
                    lambda: fake_keyring,
                )
                await oauth_manager.connect(["oauth"])
                assert oauth_manager.connected
                assert "mcp__oauth__create_issue" in oauth_manager._tools
                await oauth_manager.close()
                fake_sessions.clear()
            finally:
                mcp_runtime.KeyringTokenStorage._keyring = staticmethod(original_keyring)
            print("✅ OAuth-enabled remote connection initializes without a live login")

            static_oauth_path = os.path.join(root, "static-oauth-mcp.json")
            static_secret = "synthetic-test-secret"
            previous_client_secret = os.environ.get("MCP_TEST_CLIENT_SECRET")
            os.environ["MCP_TEST_CLIENT_SECRET"] = static_secret
            with open(static_oauth_path, "w", encoding="utf-8") as target:
                json.dump({
                    "servers": {
                        "registered": {
                            "transport": "streamable_http",
                            "url": "https://mcp.example.test/mcp",
                            "oauth": {
                                "client_id": "registered-client-id",
                                "client_secret_env": "MCP_TEST_CLIENT_SECRET",
                                "token_endpoint_auth_method": "client_secret_basic",
                            },
                        },
                    },
                }, target)
            static_manager = mcp_runtime.MCPManager(static_oauth_path)
            static_original_keyring = mcp_runtime.KeyringTokenStorage._keyring
            mcp_runtime.KeyringTokenStorage._keyring = staticmethod(lambda: fake_keyring)
            try:
                await static_manager.connect(["registered"])
                credential_identity = (
                    "https://mcp.example.test/mcp\0client_id:registered-client-id"
                )
                endpoint_key = hashlib.sha256(
                    credential_identity.encode("utf-8"),
                ).hexdigest()[:24]
                storage = mcp_runtime.KeyringTokenStorage(f"registered:{endpoint_key}")
                client_info = await storage.get_client_info()
                assert client_info is not None
                assert client_info.client_id == "registered-client-id"
                assert client_info.client_secret == static_secret
                assert client_info.token_endpoint_auth_method == "client_secret_basic"
                with open(static_oauth_path, encoding="utf-8") as source:
                    assert static_secret not in source.read()
                await static_manager.close()
            finally:
                mcp_runtime.KeyringTokenStorage._keyring = staticmethod(static_original_keyring)
                if previous_client_secret is None:
                    os.environ.pop("MCP_TEST_CLIENT_SECRET", None)
                else:
                    os.environ["MCP_TEST_CLIENT_SECRET"] = previous_client_secret
            print("✅ Pre-registered OAuth client secrets are seeded from env into keyring")

            cleanup_errors = []

            @asynccontextmanager
            async def tracking_http_client(_url, *, http_client):
                assert http_client is not None
                try:
                    yield object(), object(), lambda: None
                except BaseException as error:
                    cleanup_errors.append(type(error).__name__)
                    raise

            failed_manager = mcp_runtime.MCPManager(oauth_config_path)
            original_client_session = mcp_runtime.ClientSession
            original_streamable_http_client = mcp_runtime.streamable_http_client
            original_keyring = mcp_runtime.KeyringTokenStorage._keyring
            mcp_runtime.ClientSession = FailingInitSession
            mcp_runtime.streamable_http_client = tracking_http_client
            mcp_runtime.KeyringTokenStorage._keyring = staticmethod(lambda: fake_keyring)
            try:
                try:
                    await failed_manager.connect(["oauth"])
                    raise AssertionError("failing MCP initialization unexpectedly connected")
                except OAuthRegistrationError as error:
                    assert "Registration failed: 404" in str(error)
                assert cleanup_errors == []
                assert not failed_manager.connected
            finally:
                await failed_manager.close()
                mcp_runtime.ClientSession = original_client_session
                mcp_runtime.streamable_http_client = original_streamable_http_client
                mcp_runtime.KeyringTokenStorage._keyring = staticmethod(original_keyring)
            print("✅ Failed MCP initialization closes with the original error")

            multi_config_path = os.path.join(root, "multi-mcp.json")
            with open(multi_config_path, "w", encoding="utf-8") as target:
                json.dump({
                    "servers": {
                        "tracker": {"command": "tracker-server", "args": []},
                        "calendar": {"command": "calendar-server", "args": []},
                    },
                }, target)
            multi_manager = mcp_runtime.MCPManager(multi_config_path)
            await multi_manager.connect()
            assert len(multi_manager._connections) == 2
            await multi_manager.disconnect_server("tracker")
            assert set(multi_manager._connections) == {"calendar"}
            assert find("mcp__tracker__create_issue") is None
            assert find("mcp__calendar__create_issue") is not None
            assert multi_manager.connected
            await multi_manager.close()
            fake_sessions.clear()
            print("✅ Disconnecting one server leaves other MCP connections active")

            bound_config_path = os.path.join(root, "task-bound-mcp.json")
            with open(bound_config_path, "w", encoding="utf-8") as target:
                json.dump({
                    "servers": {
                        "bound": {"command": "bound-server", "args": []},
                    },
                }, target)
            context_owners = {}

            @asynccontextmanager
            async def task_bound_stdio(_params):
                context_owners["enter"] = asyncio.current_task()
                try:
                    yield object(), object()
                finally:
                    context_owners["exit"] = asyncio.current_task()

            original_bound_stdio = mcp_runtime.stdio_client
            mcp_runtime.stdio_client = task_bound_stdio
            bound_manager = mcp_runtime.MCPManager(bound_config_path)
            try:
                await asyncio.create_task(bound_manager.connect(["bound"]))
                await asyncio.create_task(bound_manager.disconnect_server("bound"))
                assert context_owners["enter"] is context_owners["exit"]
            finally:
                await bound_manager.close()
                mcp_runtime.stdio_client = original_bound_stdio
                fake_sessions.clear()
            print("✅ MCP transport context is closed by its owning task")

            context = make_context(root)
            manager = mcp_runtime.MCPManager(config_path)
            context.mcp_manager = manager
            assert not manager.connected
            assert not manager._tools
            assert _tools_for_context(context) == []
            print("✅ MCP servers stay stopped until discovery")

            search_result = await manager.search_and_activate("create issue", context)
            assert manager.connected
            assert len(fake_sessions) == 1
            assert len(manager._tools) == 3
            assert "mcp__tracker__create_issue" in search_result
            assert context.active_mcp_tool_names == {
                "mcp__tracker__create_issue",
                "mcp__tracker__search_issues",
            }
            exposed = _tools_for_context(context)
            assert {tool.name for tool in exposed} == context.active_mcp_tool_names
            print("✅ ToolSearch activates only matching schema")

            denied = await execute_tool({
                "id": "mcp-denied",
                "name": "mcp__tracker__create_issue",
                "input": {"title": "Review request"},
            }, context, can_use_tool=lambda _decision: asyncio.sleep(0, result=False))
            assert denied.is_error
            assert not fake_sessions[0].calls
            print("✅ MCP call waits for explicit user approval")

            approved = await execute_tool({
                "id": "mcp-approved",
                "name": "mcp__tracker__create_issue",
                "input": {"title": "Review request"},
            }, context, can_use_tool=lambda _decision: asyncio.sleep(0, result=True))
            assert not approved.is_error
            assert "Untrusted data from MCP server" in approved.result_message.content
            assert fake_sessions[0].calls == [
                ("create_issue", {"title": "Review request"}),
            ]
            print("✅ MCP results are bounded and marked untrusted")

            inactive = await execute_tool({
                "id": "mcp-inactive",
                "name": "mcp__tracker__list_projects",
                "input": {},
            }, context)
            assert inactive.is_error
            assert "not been activated" in inactive.result_message.content
            print("✅ Unactivated MCP tools cannot be invoked")

            tool = find("mcp__tracker__create_issue")
            await manager.close()
            assert not manager.connected
            assert find("mcp__tracker__create_issue") is None
            assert tool is not None
            print("✅ MCP sessions close and unregister tool wrappers")
    finally:
        mcp_runtime.stdio_client = original_stdio_client
        mcp_runtime.streamable_http_client = original_http_client
        mcp_runtime.ClientSession = original_client_session
        clear()

    print("\n🎉 All MCP runtime tests passed")


asyncio.run(main())