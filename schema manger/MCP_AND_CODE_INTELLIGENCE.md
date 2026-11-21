# MCP And Code Intelligence

Updated: 2026-11-20.

## Design

The runtime remains the host: `query.py`, `execute.py`, permissions, Task DAGs, budgets, and persistence stay authoritative. The official MCP Python SDK supplies stdio and Streamable HTTP clients; remote MCP tools are adapted to the local `Tool` contract. No agent framework replaces the existing planner or query loop.

`ToolSearch` is the always-loaded discovery entry point. It first ranks configured server names and descriptions, then connects only the best-matching one or two servers. After interactive approval, it discovers their descriptors and activates at most five matching schemas in the current query context. Repeated searches replace the active set. Each external tool call independently requires interactive approval, including in bypass-permissions mode. MCP metadata and results are untrusted data. MCP tools are not granted to Task workers or the Phase 0 runner.

## Configuration

By default, the manager reads `~/.agent/mcp_servers.json`. `AGENT_MCP_CONFIG` can point to another user-controlled file. The file must contain an explicit `servers` object; project files are not used to launch processes.

```json
{
  "servers": {
    "tracker": {
      "description": "Issue tracking and pull requests.",
      "command": "uvx",
      "args": ["trusted-tracker-mcp-server"],
      "env": {
        "TRACKER_TOKEN": "${TRACKER_TOKEN}"
      }
    }
  }
}
```

Remote servers use Streamable HTTP and OAuth by default:

```json
{
  "servers": {
    "tracker-cloud": {
      "description": "Issue tracking and pull requests.",
      "transport": "streamable_http",
      "url": "https://mcp.example.com/mcp",
      "oauth": {
        "client_metadata_url": "https://agent.example.com/oauth/client-metadata.json",
        "redirect_uri": "http://127.0.0.1:6276/oauth/callback"
      }
    }
  }
}
```

Without `client_metadata_url`, the OAuth SDK uses its registration compatibility path. CIMD is preferred by the current MCP authorization specification; its public metadata document must declare the exact configured redirect URI. If `redirect_uri` is omitted, the client uses an ephemeral loopback port, which is suitable for local dynamic registration. Set `"oauth": false` only for an intentionally unauthenticated remote server. The first authorization opens the system browser in the Textual UI or posts a sign-in link in Chainlit; tokens and client registration data are stored in the OS credential store through `keyring`. OAuth fails closed if no usable credential-store backend is available.

For Chainlit behind a remote browser (for example, Codespaces), configure the public HTTPS callback URL with `AGENT_MCP_OAUTH_REDIRECT_URI=https://<forwarded-host>/oauth/callback`, or set `oauth.redirect_uri` on an individual server. The callback host must route to the Chainlit server on port 8000. Chainlit posts the provider authorization link in the chat, and its `/oauth/callback` route delivers the redirect to the pending connection after validating OAuth state. CIMD metadata must list this exact public redirect URI; dynamic registration must allow it. Servers that disable DCR may instead use `oauth.client_id` with optional `oauth.client_secret_env` and `oauth.token_endpoint_auth_method`; the referenced secret is loaded from the process environment and pre-registered client info is stored in the OS keyring. Keep the app private and use a host with an available OS credential store. Without this setting, the existing loopback callback remains in use.

Remote MCP URLs must use HTTPS; HTTP is accepted only for loopback development endpoints. URLs containing user information, query strings, or fragments are rejected. OAuth callbacks bind only to `127.0.0.1`, validate state, and reject a returned issuer that does not match discovered authorization-server metadata. The SDK's `mcp` dependency is constrained to `<2.0.0` because the currently included `langchain-mcp-adapters` requires MCP SDK 1.x; the client callback validates `iss` at the loopback boundary.

In the Textual UI, open **MCP connections** or press `Ctrl+5` to add a local stdio command or remote HTTPS endpoint, connect/disconnect a saved server, or remove its configuration. The UI confirms before launching a local process or contacting a remote endpoint. Stdio arguments are parsed as an argv list and are never passed through a shell. Connector configuration is atomically saved with mode `0600`; raw credentials are not accepted by this form. Environment-backed stdio secrets should be referenced as `${NAME}` in a manually edited user config. Removing a connector does not revoke provider-side OAuth grants.

A browser UI over the coding-agent runtime is available in `chainlit_app.py`. Start it from this directory with `uvicorn chainlit_server:app --host 127.0.0.1 --port 8000`; it uses the environment-configured provider and the same user-level MCP configuration. The chat start view displays MCP connection cards with add/connect/disconnect/remove actions and optional OAuth CIMD settings. The direct Uvicorn entry point avoids a static-file serving failure in the current Chainlit CLI environment. Keep this single-user app private.

For stdio servers, only a small allowlist of non-secret process environment values is inherited. Other variables must be explicitly mapped. A value exactly matching `${NAME}` is resolved from the parent environment; missing variables fail closed. Commands and arguments are passed directly to the subprocess API without a shell. `cwd`, when provided, must be an existing absolute directory.

## Current Limits

- The optional code-intelligence environment is installed from `requirements-code-intel.txt` and `package.json` in this folder. Tree-Sitter symbol extraction currently covers Python, TypeScript/TSX, JavaScript, Go, and Rust; the grammar pack's broader language count is not the same as extraction coverage.
- From this directory, install with `python -m pip install -r requirements-code-intel.txt` and `npm install`. `AstGrep` patterns support Python, TypeScript, TSX, JavaScript, Go, and Rust in this runtime.
- `IndexCode` creates a private mode-0600 SQLite index under `~/.agent/code-index` (override with `AGENT_CODE_INDEX_DIR`), incrementally reuses content hashes, and reports syntax errors/skipped files. Task-scoped indexing is in-memory and contains only manifest-visible files.
- `FindCallers`/`FindCallees` are syntactic name-match candidates; `FindImports` reports syntax-level import strings. They are not a resolved graph and must never be used as proof that code is unused.
- `AstGrep` uses a pinned local CLI and is read-only. Pattern search is bounded to explicitly enumerated files; rewriting is not exposed.
- Pyright references/definitions run on demand over one project root or a Task-scope snapshot. Multi-root/worktree correctness, diagnostics, rename, and cache reuse are not implemented yet.
- Connecting a stdio server starts a configured subprocess as the current OS user; remote servers are contacted over Streamable HTTP. Per-tool permission prompts are not an OS sandbox or a server trust guarantee; select and configure trusted servers.
- Server tool schemas are checked locally, remote `$ref` resolution is rejected, and schemas/results are bounded. Descriptor hashes are recorded for diagnostics but there is not yet a persistent approval manifest or `tools/list_changed` refresh flow.
- Tool discovery uses bounded local keyword matching, not BM25 or a third-party gateway. Only selected schemas are sent to the model.
- MCP resources, prompts, sampling, subscriptions, and MCP Apps are not integrated. Remote OAuth currently uses the system browser; persistent per-tool approval grants and provider-side OAuth revocation are not implemented.
- The current coordinator deliberately does not pass MCP capabilities to child workers.

## Existing Code-Intelligence Layer

`tools/search.py` provides bounded literal `Grep` and filename `Glob` over the project, excluding common generated and credential-like paths. Task-worker searches are restricted to each worker's declared file manifest. `tools/code_intelligence.py` provides Python `GetEditContext`, optional Jedi references, tested Pyright references/definition, Tree-Sitter symbols/imports/static call candidates and a token-bounded map, plus bounded ast-grep structural search. Persistent indexes are parent-only; Task queries use manifest-limited in-memory indexes/snapshots. All file outputs distinguish syntax candidates from semantic results. `Edit` retains the read-before-write and stale-file checks. `loop_guard.py` warns on repeated actions, and warnings now reach the model before a hard block.

The headless index and map are not the Aider RepoMap implementation or a complete resolved call graph. Pyright is a real LSP-backed Python navigation layer, but there is still no semantic rename or diagnostics UI. Jedi remains an optional fallback. The existing top-level `aider_mcp_server.py` is not wired into this runtime and its auto-commit behavior is not used here.

## Next Evaluation

1. Run `test_code_index.py`, `test_code_intelligence.py`, `test_pyright_lsp.py`, `test_mcp_runtime.py`, `test_search.py`, `test_execute.py`, Task contract/query integration tests, and then the full offline suite.
2. Exercise one explicitly trusted local stdio server with a human reviewing startup and each call. Do not use a live remote service or credentials in an offline test.
3. Measure context size, reference recall, and edit correctness for AST/Jedi, Pyright, and the static index. Keep unknown/dynamic references explicit; do not make safe-delete decisions from a static candidate graph.
4. Validate Pyright on real multi-root and detached-worktree cases before adding persistent LSP session reuse or diagnostics.
5. Keep embedding search and large agent frameworks optional. They overlap with existing search, workflow, and orchestration layers and should be justified by measured task gaps.

No performance, security, language-coverage, or upstream-maintenance claims from pasted research are treated as verified local evidence.
