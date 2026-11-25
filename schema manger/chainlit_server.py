"""Direct Uvicorn entry point for the Chainlit Schema Manager UI."""

import chainlit_app  # Register Chainlit callbacks before exporting the ASGI app.
from fastapi import Request
from fastapi.responses import PlainTextResponse
from chainlit.server import app
from mcp_runtime import LoopbackOAuthCallback


@app.get("/oauth/callback", include_in_schema=False)
async def mcp_oauth_callback(request: Request) -> PlainTextResponse:
	parameters: dict[str, list[str]] = {}
	for key, value in request.query_params.multi_items():
		parameters.setdefault(key, []).append(value)
	status, message = await LoopbackOAuthCallback.accept_public_callback(parameters)
	return PlainTextResponse(message, status_code=status)


callback_route = next(
	route for route in app.router.routes
	if getattr(route, "endpoint", None) is mcp_oauth_callback
)
app.router.routes.remove(callback_route)
app.router.routes.insert(0, callback_route)

__all__ = ["app"]