"""Answer at `/` and under `/reports/rasa-incanta/` with the same app (CGO reports standard).

The portal forwards the full path, prefix included. For requests under the prefix this
wrapper sets `root_path` to the prefix and leaves `path` untouched; Starlette then matches
routes on the path below the prefix, so `/health` and `/reports/rasa-incanta/health` reach
the same handler. The path is never stripped, so logs and redirects keep the real URL.
"""
from starlette.types import ASGIApp, Receive, Scope, Send


class MountUnderPrefix:
    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self.prefix and scope["type"] == "http":
                await _redirect(send, self.prefix + "/", scope.get("query_string", b""))
                return
            if path.startswith(self.prefix + "/"):
                scope = dict(scope, root_path=self.prefix)  # path deliberately untouched
        await self.app(scope, receive, send)


async def _redirect(send: Send, location: str, query: bytes) -> None:
    target = location + ("?" + query.decode("latin-1") if query else "")
    await send({"type": "http.response.start", "status": 307,
                "headers": [(b"location", target.encode("latin-1")), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})
