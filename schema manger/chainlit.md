# Schema Manager

Provider-agnostic coding agent running on the Schema Manager Python runtime.

Use the **MCP connectors** action to inspect configured integrations, or **Add connector** to set up a local stdio server or remote Streamable HTTP server. Connections and external tool calls require approval.

The browser UI is intended for one trusted local user. Start it from this directory with:

```bash
uvicorn chainlit_server:app --host 127.0.0.1 --port 8000
```
