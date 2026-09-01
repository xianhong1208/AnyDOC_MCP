"""Request/response access-log middleware."""

import json as _json
import time

from src.log import get_api_logger, get_mcptools_logger


class RequestLogMiddleware:
    """Access log for every HTTP request and its response.

    MCP requests (POST /mcp etc.) have their JSON-RPC body parsed so the method and
    tool name are logged; other requests (e.g. /api/*) log the HTTP method and path.
    The response side logs status code and elapsed time, plus the body at DEBUG level.
    Health checks, docs and the landing page are not logged.

    Attributes:
        MCP_PATHS: Path prefixes treated as MCP.
        SKIP_PREFIXES: Path prefixes that are not logged (noise).
        SKIP_EXACT: Exact paths that are not logged.
        MAX_BODY_LOG: Byte cap for the logged response body.
        QUIET_METHODS: MCP methods demoted to DEBUG (handshake / health checks).
    """

    MCP_PATHS = ["/mcp", "/messages", "/sse"]
    SKIP_PREFIXES = ["/health", "/docs", "/redoc", "/openapi.json", "/favicon"]
    SKIP_EXACT = {"/"}
    MAX_BODY_LOG = 2000
    QUIET_METHODS = {"initialize", "notifications/initialized", "ping"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        method = scope.get("method", "")
        is_mcp = method == "POST" and any(path.startswith(p) for p in self.MCP_PATHS)

        if not is_mcp and self._should_skip(path):
            await self.app(scope, receive, send)
            return

        quiet = False
        if is_mcp:
            body = await self._read_body(receive)
            if body is None:
                return
            label, quiet = self._log_mcp_request(body)
            receive = self._make_receive(body, receive)
            logger, kind = get_mcptools_logger(), "MCP"
        else:
            label = f"{method} {path}"
            logger, kind = get_api_logger(), "API"
            logger.info(f"[{kind}_REQ] {label}")

        cap = {"status": None, "ct": "", "buf": bytearray()}

        async def send_wrapper(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                cap["status"] = message["status"]
                for k, v in message.get("headers", []):
                    if k.lower() == b"content-type":
                        cap["ct"] = v.decode("latin-1")
                        break
            elif mtype == "http.response.body":
                ct = cap["ct"]
                if (ct.startswith("application/json") or ct.startswith("text/event-stream")) \
                        and len(cap["buf"]) < self.MAX_BODY_LOG:
                    chunk = message.get("body", b"")
                    cap["buf"].extend(chunk[: self.MAX_BODY_LOG - len(cap["buf"])])
            # Forward every message immediately as-is and only tap a copy for logging,
            # so SSE streams are never stalled
            await send(message)

        start = time.perf_counter()
        await self.app(scope, receive, send_wrapper)
        ms = round((time.perf_counter() - start) * 1000)

        (logger.debug if quiet else logger.info)(
            f"[{kind}_RESP] {label} -> {cap['status']} ({ms}ms)"
        )
        if cap["buf"]:
            text = bytes(cap["buf"]).decode("utf-8", "replace").strip().replace("\n", " ")
            logger.debug(f"[{kind}_RESP_BODY] {label} {text}")

    def _should_skip(self, path: str) -> bool:
        """Decide whether a non-MCP path should be skipped.

        Args:
            path: Request path.

        Returns:
            True for noise paths (health check, docs, landing page, or non-POST MCP
            transport requests).
        """
        if path in self.SKIP_EXACT:
            return True
        if any(path.startswith(p) for p in self.MCP_PATHS):
            return True
        return any(path.startswith(p) for p in self.SKIP_PREFIXES)

    async def _read_body(self, receive):
        """Read and buffer the request body.

        Args:
            receive: ASGI receive callable.

        Returns:
            The full request body bytes, or None if the client disconnected midway.
        """
        chunks = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return None
        return b"".join(chunks)

    def _log_mcp_request(self, body: bytes):
        """Parse the MCP JSON-RPC body and log the request (single or batch).

        Args:
            body: Raw JSON-RPC bytes of the request.

        Returns:
            (label, quiet) tuple. label is a short tag for the response log; quiet is
            True when the whole batch is handshake / health-check methods, in which
            case the response log should drop to DEBUG.
        """
        if not body:
            return "(empty)", True
        logger = get_mcptools_logger()
        try:
            data = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "(non-json)", False

        items = data if isinstance(data, list) else [data]
        label = "(?)"
        all_quiet = True
        for item in items:
            if not isinstance(item, dict):
                continue
            mcp_method = item.get("method")
            if not mcp_method:
                continue
            quiet = mcp_method in self.QUIET_METHODS
            all_quiet = all_quiet and quiet
            log_at = logger.debug if quiet else logger.info
            if mcp_method == "tools/call":
                params = item.get("params") or {}
                tool = params.get("name", "?")
                label = f"tools/call tool={tool}"
                log_at(f"[MCP_REQ] method=tools/call tool={tool}")
                args = params.get("arguments")
                if args is not None:
                    logger.debug(
                        f"[MCP_REQ_ARGS] tool={tool} args={_json.dumps(args, ensure_ascii=False)}"
                    )
            else:
                label = f"method={mcp_method}"
                log_at(f"[MCP_REQ] method={mcp_method}")
        if len(items) > 1:
            label = f"batch({len(items)})"
        return label, all_quiet

    def _make_receive(self, body: bytes, original_receive):
        """Build a receive callable that replays the buffered body first.

        Args:
            body: The request body read and buffered earlier.
            original_receive: The original ASGI receive callable.

        Returns:
            A receive callable that returns the buffered body once, then delegates to
            the original receive (so downstream can detect client disconnects).
        """
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()

        return receive
