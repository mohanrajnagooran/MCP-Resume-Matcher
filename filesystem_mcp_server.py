#!/usr/bin/env python3
"""
filesystem_mcp_server.py
========================

A Model Context Protocol (MCP) server that exposes the Milestone 1 resume
file-system tools through a standard, JSON-RPC 2.0 interface.

Transports
----------
* ``stdio`` (default, the MCP standard transport): newline-delimited JSON-RPC
  messages on stdin/stdout. Logs go to stderr only, so stdout stays clean.
* ``http``: a small JSON-RPC-over-HTTP endpoint (``POST /mcp``) plus
  ``GET /health`` and ``GET /.well-known/mcp.json`` for discovery. Handy for
  curl demos and health checks; server-push notifications are stdio-only.

What the server offers
----------------------
Tools (``tools/list`` / ``tools/call``)
    Milestone 1 tools: ``list_roots``, ``list_files``, ``read_file``,
    ``extract_text``, ``get_file_metadata``, ``search_files``, ``write_file``
    New MCP capabilities: ``watch_directory``, ``batch_process``
    Configuration: ``get_server_config``, ``configure_server``

Resources (``resources/list`` / ``resources/read`` / ``resources/templates/list``)
    ``fs://{root}/{path}``         file contents (text, or text extracted from PDF/DOCX)
    ``fs-meta://{root}/{path}``    file metadata as JSON
    ``tools://catalog``            every tool and its schema (tools exposed as resources)
    ``tools://{name}``             a single tool's schema
    ``config://server``            the live server configuration
    ``watch://active``             active directory watches
    ``watch://{watch_id}/events``  pending events of one watch (read drains them)

Discovery
    Standard MCP discovery (``initialize`` capabilities + the list methods)
    plus a ``server/discover`` method that returns everything in one call.

Error handling
--------------
* JSON-RPC protocol errors use the standard codes (-32700, -32600, -32601,
  -32602, -32603) and the server-defined range -32000..-32099 (see ``ErrorCode``).
* Tool execution failures (file missing, access denied, ...) are returned as a
  tool result with ``isError: true`` and a structured error object, as the MCP
  specification recommends, so an agent or LLM can read and react to them.

Run
---
    python filesystem_mcp_server.py                       # stdio, default config
    python filesystem_mcp_server.py --config config/filesystem_server.json
    python filesystem_mcp_server.py --transport http --port 8765
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

SERVER_NAME = "resume-filesystem-mcp"
SERVER_VERSION = "2.0.0"
JSONRPC_VERSION = "2.0"

# Newest first. The server answers with the client's version when it supports
# it, otherwise with its own newest version (MCP version negotiation).
SUPPORTED_PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

log = logging.getLogger(SERVER_NAME)


# =============================================================================
# Errors
# =============================================================================
class ErrorCode:
    """JSON-RPC 2.0 standard codes plus this server's codes (-32000..-32099)."""

    # JSON-RPC 2.0 standard
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    # Server-defined
    ACCESS_DENIED = -32001
    RESOURCE_NOT_FOUND = -32002  # also used by MCP for resources/read
    FILE_TOO_LARGE = -32003
    UNSUPPORTED_FILE_TYPE = -32004
    ALREADY_EXISTS = -32005
    EXTRACTION_FAILED = -32006
    WATCH_NOT_FOUND = -32007
    LIMIT_EXCEEDED = -32008
    READ_ONLY = -32009
    NOT_INITIALIZED = -32010
    DEPENDENCY_MISSING = -32011
    REQUEST_CANCELLED = -32800


ERROR_NAMES = {v: k for k, v in vars(ErrorCode).items() if isinstance(v, int)}


class MCPError(Exception):
    """An error that maps directly onto a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    @property
    def type(self) -> str:
        return ERROR_NAMES.get(self.code, "SERVER_ERROR")

    def to_error_object(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# =============================================================================
# Minimal JSON-Schema validation for tool arguments
# =============================================================================
_JSON_TYPES: dict[str, tuple] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def validate_arguments(schema: dict, value: Any, where: str = "arguments") -> list[str]:
    """Validate ``value`` against the subset of JSON Schema used by our tools.

    Returns a list of human-readable problems (empty list means valid).
    """
    problems: list[str] = []
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        ok = False
        for t in types:
            py = _JSON_TYPES.get(t, ())
            if isinstance(value, py) and not (t in ("integer", "number") and isinstance(value, bool)):
                ok = True
                break
        if not ok:
            return [f"{where}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{where}: must be one of {schema['enum']}, got {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{where}: must be <= {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{where}: must be at least {schema['minLength']} characters")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{where}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{where}: allows at most {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                problems += validate_arguments(schema["items"], item, f"{where}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                problems.append(f"{where}: missing required property '{req}'")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    problems.append(f"{where}: unknown property '{key}'")
        for key, sub in props.items():
            if key in value:
                problems += validate_arguments(sub, value[key], f"{where}.{key}")
    return problems


# =============================================================================
# Generic MCP server: JSON-RPC 2.0 + MCP lifecycle, transport-agnostic
# =============================================================================
@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Any]
    annotations: dict = field(default_factory=dict)
    title: str | None = None

    def describe(self) -> dict:
        d = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.title:
            d["title"] = self.title
        if self.annotations:
            d["annotations"] = self.annotations
        return d


@dataclass
class PromptSpec:
    name: str
    description: str
    arguments: list[dict]
    render: Callable[[dict], list[dict]]


class RequestContext:
    """Per-request helpers handed to every handler: progress, logging, cancellation."""

    def __init__(self, server: "MCPServer", request_id: Any, meta: dict | None):
        self.server = server
        self.request_id = request_id
        self.progress_token = (meta or {}).get("progressToken")
        self.cancelled = threading.Event()

    def report_progress(self, progress: float, total: float | None = None, message: str | None = None):
        if self.progress_token is None:
            return
        params: dict[str, Any] = {"progressToken": self.progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message:
            params["message"] = message
        self.server.notify("notifications/progress", params)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise MCPError(ErrorCode.REQUEST_CANCELLED, "Request was cancelled by the client")


LOG_LEVELS = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]


class MCPServer:
    """Transport-agnostic MCP server core.

    Subclasses register tools/prompts and override the ``resource_*`` hooks.
    ``handle_payload`` turns one raw JSON-RPC payload (single message or batch)
    into the raw response (or ``None`` when nothing must be sent back).
    """

    def __init__(self, name: str, version: str, instructions: str = ""):
        self.server_info = {"name": name, "version": version}
        self.instructions = instructions
        self.tools: dict[str, ToolSpec] = {}
        self.prompts: dict[str, PromptSpec] = {}
        self.initialized = False
        self.client_info: dict | None = None
        self.client_capabilities: dict = {}
        self.protocol_version: str | None = None
        self.strict_lifecycle = True
        self.page_size = 50
        self.subscriptions: set[str] = set()
        self._log_level = "info"
        self._writer: Callable[[str], None] | None = None
        self._write_lock = threading.Lock()
        self._inflight: dict[Any, RequestContext] = {}
        self._inflight_lock = threading.Lock()
        self.started_at = utc_now()
        self.stats = {"requests": 0, "errors": 0, "notifications_sent": 0, "tool_calls": {}}

        self.methods: dict[str, Callable[[dict, RequestContext], Any]] = {
            "initialize": self._m_initialize,
            "ping": lambda params, ctx: {},
            "tools/list": self._m_tools_list,
            "tools/call": self._m_tools_call,
            "resources/list": self._m_resources_list,
            "resources/templates/list": self._m_resource_templates_list,
            "resources/read": self._m_resources_read,
            "resources/subscribe": self._m_resources_subscribe,
            "resources/unsubscribe": self._m_resources_unsubscribe,
            "prompts/list": self._m_prompts_list,
            "prompts/get": self._m_prompts_get,
            "logging/setLevel": self._m_logging_set_level,
            "server/discover": self._m_discover,
        }
        self.notification_handlers: dict[str, Callable[[dict], None]] = {
            "notifications/initialized": self._n_initialized,
            "notifications/cancelled": self._n_cancelled,
            "notifications/roots/list_changed": lambda params: None,
        }

    # ------------------------------------------------------------------ registry
    def tool(self, name: str, description: str, schema: dict, *, title: str | None = None,
             annotations: dict | None = None):
        def decorator(fn):
            self.tools[name] = ToolSpec(name, description, schema, fn, annotations or {}, title)
            return fn
        return decorator

    def prompt(self, name: str, description: str, arguments: list[dict]):
        def decorator(fn):
            self.prompts[name] = PromptSpec(name, description, arguments, fn)
            return fn
        return decorator

    # ------------------------------------------------- hooks for subclasses
    def capabilities(self) -> dict:
        return {
            "tools": {"listChanged": False},
            "resources": {"subscribe": True, "listChanged": True},
            "prompts": {"listChanged": False},
            "logging": {},
        }

    def resource_list(self) -> list[dict]:
        return []

    def resource_templates(self) -> list[dict]:
        return []

    def resource_read(self, uri: str) -> list[dict]:
        raise MCPError(ErrorCode.RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})

    def describe_extra(self) -> dict:
        return {}

    # ------------------------------------------------------------- outbound
    def _write(self, payload: str) -> None:
        if self._writer is None:
            return
        with self._write_lock:
            self._writer(payload)

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a server-to-client notification (stdio transport only)."""
        if self._writer is None or not self.initialized:
            log.debug("notification dropped (no push channel): %s", method)
            return
        msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
        if params is not None:
            msg["params"] = params
        self.stats["notifications_sent"] += 1
        self._write(json.dumps(msg, default=str))

    def log_to_client(self, level: str, data: Any, logger_name: str | None = None) -> None:
        """MCP structured logging (``notifications/message``) honouring logging/setLevel."""
        if LOG_LEVELS.index(level) < LOG_LEVELS.index(self._log_level):
            return
        params = {"level": level, "data": data}
        if logger_name:
            params["logger"] = logger_name
        self.notify("notifications/message", params)

    # ------------------------------------------------------------- inbound
    @staticmethod
    def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err}

    def handle_payload(self, raw: str | bytes) -> str | None:
        """Process one raw payload: a single message or a JSON-RPC batch."""
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return json.dumps(self._error(None, ErrorCode.PARSE_ERROR, "Parse error", {"detail": str(exc)}))
        if isinstance(message, list):
            if not message:
                return json.dumps(self._error(None, ErrorCode.INVALID_REQUEST, "Invalid Request: empty batch"))
            responses = [r for r in (self.handle_message(m) for m in message) if r is not None]
            return json.dumps(responses, default=str) if responses else None
        response = self.handle_message(message)
        return json.dumps(response, default=str) if response is not None else None

    def handle_message(self, msg: Any) -> dict | None:
        """Process one decoded JSON-RPC message and return the response dict (or None)."""
        if not isinstance(msg, dict):
            return self._error(None, ErrorCode.INVALID_REQUEST, "Invalid Request: message must be an object")

        has_id = "id" in msg
        req_id = msg.get("id")
        if has_id and (isinstance(req_id, bool) or not isinstance(req_id, (str, int, type(None)))):
            return self._error(None, ErrorCode.INVALID_REQUEST, "Invalid Request: id must be a string, integer or null")

        if "method" not in msg:
            if "result" in msg or "error" in msg:
                return None  # a response to a server->client request; we send none, so ignore
            return self._error(req_id, ErrorCode.INVALID_REQUEST, "Invalid Request: missing 'method'")
        if msg.get("jsonrpc") != JSONRPC_VERSION:
            return None if not has_id else self._error(
                req_id, ErrorCode.INVALID_REQUEST, "Invalid Request: 'jsonrpc' must be exactly \"2.0\"")

        method = msg["method"]
        params = msg.get("params", {})
        if not isinstance(method, str):
            return self._error(req_id, ErrorCode.INVALID_REQUEST, "Invalid Request: 'method' must be a string")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if not has_id:
                return None
            return self._error(req_id, ErrorCode.INVALID_PARAMS, "Invalid params: MCP uses by-name (object) params")

        # ---- notifications: never answered, errors only logged
        if not has_id:
            handler = self.notification_handlers.get(method)
            if handler is None:
                log.debug("ignored unknown notification %s", method)
                return None
            try:
                handler(params)
            except Exception:  # noqa: BLE001 - must never break the loop
                log.exception("notification handler failed: %s", method)
            return None

        # ---- requests
        self.stats["requests"] += 1
        if self.strict_lifecycle and not self.initialized and method not in ("initialize", "ping"):
            self.stats["errors"] += 1
            return self._error(req_id, ErrorCode.NOT_INITIALIZED,
                               "Server not initialized: send 'initialize' first", {"method": method})
        handler = self.methods.get(method)
        if handler is None:
            self.stats["errors"] += 1
            return self._error(req_id, ErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}",
                               {"method": method, "available": sorted(self.methods)})

        ctx = RequestContext(self, req_id, params.get("_meta"))
        with self._inflight_lock:
            self._inflight[req_id] = ctx
        try:
            result = handler(params, ctx)
            return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}
        except MCPError as exc:
            self.stats["errors"] += 1
            return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": exc.to_error_object()}
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            log.error("internal error in %s: %s\n%s", method, exc, traceback.format_exc())
            return self._error(req_id, ErrorCode.INTERNAL_ERROR, "Internal error",
                               {"method": method, "type": type(exc).__name__, "detail": str(exc)})
        finally:
            with self._inflight_lock:
                self._inflight.pop(req_id, None)

    # ------------------------------------------------------- method handlers
    def _m_initialize(self, params: dict, ctx: RequestContext) -> dict:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise MCPError(ErrorCode.INVALID_PARAMS, "initialize requires 'protocolVersion' (string)")
        self.protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        self.client_info = params.get("clientInfo") or {}
        self.client_capabilities = params.get("capabilities") or {}
        log.info("initialize from %s (requested %s, using %s)",
                 self.client_info.get("name", "?"), requested, self.protocol_version)
        result = {
            "protocolVersion": self.protocol_version,
            "capabilities": self.capabilities(),
            "serverInfo": self.server_info,
        }
        if self.instructions:
            result["instructions"] = self.instructions
        # HTTP mode is stateless, so treat the session as ready immediately.
        if not self.strict_lifecycle:
            self.initialized = True
        return result

    def _n_initialized(self, params: dict) -> None:
        self.initialized = True
        log.info("client signalled initialized")

    def _n_cancelled(self, params: dict) -> None:
        with self._inflight_lock:
            ctx = self._inflight.get(params.get("requestId"))
        if ctx:
            ctx.cancelled.set()
            log.info("request %s cancelled: %s", params.get("requestId"), params.get("reason", ""))

    def _paginate(self, items: list, params: dict, key: str) -> dict:
        cursor = params.get("cursor")
        start = 0
        if cursor:
            try:
                start = int(base64.urlsafe_b64decode(cursor.encode()).decode())
            except Exception:  # noqa: BLE001
                raise MCPError(ErrorCode.INVALID_PARAMS, "Invalid cursor", {"cursor": cursor}) from None
        page = items[start:start + self.page_size]
        result: dict[str, Any] = {key: page}
        if start + self.page_size < len(items):
            result["nextCursor"] = base64.urlsafe_b64encode(str(start + self.page_size).encode()).decode()
        return result

    def _m_tools_list(self, params: dict, ctx: RequestContext) -> dict:
        return self._paginate([t.describe() for t in self.tools.values()], params, "tools")

    def _m_tools_call(self, params: dict, ctx: RequestContext) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise MCPError(ErrorCode.INVALID_PARAMS, "tools/call requires 'name' (string)")
        spec = self.tools.get(name)
        if spec is None:
            raise MCPError(ErrorCode.INVALID_PARAMS, f"Unknown tool: {name}", {"available": sorted(self.tools)})
        if not isinstance(arguments, dict):
            raise MCPError(ErrorCode.INVALID_PARAMS, "'arguments' must be an object")
        problems = validate_arguments(spec.input_schema, arguments)
        if problems:
            raise MCPError(ErrorCode.INVALID_PARAMS, f"Invalid arguments for tool '{name}'",
                           {"tool": name, "problems": problems})

        self.stats["tool_calls"][name] = self.stats["tool_calls"].get(name, 0) + 1
        started = time.perf_counter()
        try:
            data = spec.handler(ctx=ctx, **arguments)
            is_error = False
        except MCPError as exc:
            # Tool execution errors are reported *inside* the result (MCP spec),
            # so the calling agent/LLM can see what went wrong and recover.
            data = {"error": {"code": exc.code, "type": exc.type, "message": exc.message, "details": exc.data}}
            is_error = True
            self.log_to_client("warning", {"tool": name, "error": exc.message}, "tools")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        log.info("tool %s -> %s in %sms", name, "error" if is_error else "ok", elapsed_ms)
        return {
            "content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}],
            "structuredContent": data,
            "isError": is_error,
            "_meta": {"elapsed_ms": elapsed_ms},
        }

    def _m_resources_list(self, params: dict, ctx: RequestContext) -> dict:
        return self._paginate(self.resource_list(), params, "resources")

    def _m_resource_templates_list(self, params: dict, ctx: RequestContext) -> dict:
        return self._paginate(self.resource_templates(), params, "resourceTemplates")

    def _m_resources_read(self, params: dict, ctx: RequestContext) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise MCPError(ErrorCode.INVALID_PARAMS, "resources/read requires 'uri' (string)")
        return {"contents": self.resource_read(uri)}

    def _m_resources_subscribe(self, params: dict, ctx: RequestContext) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise MCPError(ErrorCode.INVALID_PARAMS, "resources/subscribe requires 'uri'")
        self.resource_read(uri)  # validates that it exists / is readable
        self.subscriptions.add(uri)
        return {}

    def _m_resources_unsubscribe(self, params: dict, ctx: RequestContext) -> dict:
        self.subscriptions.discard(params.get("uri"))
        return {}

    def _m_prompts_list(self, params: dict, ctx: RequestContext) -> dict:
        items = [{"name": p.name, "description": p.description, "arguments": p.arguments}
                 for p in self.prompts.values()]
        return self._paginate(items, params, "prompts")

    def _m_prompts_get(self, params: dict, ctx: RequestContext) -> dict:
        spec = self.prompts.get(params.get("name"))
        if spec is None:
            raise MCPError(ErrorCode.INVALID_PARAMS, f"Unknown prompt: {params.get('name')}")
        args = params.get("arguments") or {}
        missing = [a["name"] for a in spec.arguments if a.get("required") and a["name"] not in args]
        if missing:
            raise MCPError(ErrorCode.INVALID_PARAMS, "Missing prompt arguments", {"missing": missing})
        return {"description": spec.description, "messages": spec.render(args)}

    def _m_logging_set_level(self, params: dict, ctx: RequestContext) -> dict:
        level = params.get("level")
        if level not in LOG_LEVELS:
            raise MCPError(ErrorCode.INVALID_PARAMS, "Invalid log level", {"allowed": LOG_LEVELS})
        self._log_level = level
        return {}

    def _m_discover(self, params: dict, ctx: RequestContext) -> dict:
        """One-shot discovery endpoint (server extension, not part of core MCP)."""
        return {
            "serverInfo": self.server_info,
            "protocolVersions": SUPPORTED_PROTOCOL_VERSIONS,
            "capabilities": self.capabilities(),
            "instructions": self.instructions,
            "methods": sorted(self.methods),
            "tools": [t.describe() for t in self.tools.values()],
            "resources": self.resource_list(),
            "resourceTemplates": self.resource_templates(),
            "prompts": [{"name": p.name, "description": p.description, "arguments": p.arguments}
                        for p in self.prompts.values()],
            "errorCodes": {name: code for code, name in sorted(ERROR_NAMES.items())},
            "stats": self.stats,
            "startedAt": self.started_at,
            **self.describe_extra(),
        }

    # ---------------------------------------------------------- transports
    def shutdown(self) -> None:
        """Hook for subclasses to release resources."""

    def serve_stdio(self, max_workers: int = 8) -> None:
        """MCP stdio transport: one JSON-RPC message per line on stdin/stdout.

        Requests run on a worker pool so a slow call (a long-poll on a watch,
        a big batch) does not block pings or cancellation notices.
        """
        out = sys.stdout

        def writer(payload: str) -> None:
            out.write(payload + "\n")
            out.flush()

        self._writer = writer
        pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mcp-req")

        def process(line: str) -> None:
            response = self.handle_payload(line)
            if response is not None:
                self._write(response)

        log.info("%s %s listening on stdio", *self.server_info.values())
        try:
            for raw in sys.stdin.buffer:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                # Lifecycle messages and notifications are processed inline so
                # ordering (initialize -> initialized -> requests) is preserved.
                inline = True
                try:
                    peek = json.loads(line)
                    if isinstance(peek, dict) and "id" in peek and peek.get("method") not in ("initialize",):
                        inline = False
                except json.JSONDecodeError:
                    pass
                if inline:
                    process(line)
                else:
                    pool.submit(process, line)
        except KeyboardInterrupt:
            pass
        finally:
            pool.shutdown(wait=True)
            self.shutdown()
            log.info("stdio transport closed")

    def serve_http(self, host: str = "127.0.0.1", port: int = 8765, max_body: int = 10 * 1024 * 1024) -> None:
        """JSON-RPC over HTTP: POST /mcp. Stateless; no server-push notifications."""
        server = self
        self.strict_lifecycle = False

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # route access logs to our logger
                log.info("http %s - " + fmt, self.client_address[0], *args)

            def _send(self, status: int, body: Any = None):
                data = b"" if body is None else (body if isinstance(body, bytes) else
                                                  json.dumps(body, default=str).encode())
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200, {"status": "ok", "server": server.server_info, "startedAt": server.started_at,
                                     "stats": server.stats})
                elif self.path in ("/.well-known/mcp.json", "/discover"):
                    self._send(200, server._m_discover({}, RequestContext(server, None, None)))
                else:
                    self._send(404, {"error": "not found", "endpoints": ["POST /mcp", "GET /health",
                                                                        "GET /.well-known/mcp.json"]})

            def do_POST(self):
                if self.path not in ("/mcp", "/"):
                    return self._send(404, {"error": "not found"})
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                if ctype != "application/json":
                    return self._send(415, server._error(None, ErrorCode.INVALID_REQUEST,
                                                         "Content-Type must be application/json"))
                length = int(self.headers.get("Content-Length") or 0)
                if length > max_body:
                    return self._send(413, server._error(None, ErrorCode.INVALID_REQUEST, "Payload too large"))
                body = self.rfile.read(length)
                response = server.handle_payload(body)
                if response is None:
                    return self._send(202)  # notification(s) only: accepted, no content
                parsed = json.loads(response)
                code = parsed.get("error", {}).get("code") if isinstance(parsed, dict) else None
                status = 400 if code in (ErrorCode.PARSE_ERROR, ErrorCode.INVALID_REQUEST) else 200
                self._send(status, response.encode())

            def do_PUT(self):
                self._send(405, {"error": "method not allowed"})

            do_DELETE = do_PATCH = do_PUT

        httpd = ThreadingHTTPServer((host, port), Handler)
        log.info("%s %s listening on http://%s:%s/mcp", *self.server_info.values(), host, port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            self.shutdown()


# =============================================================================
# Configuration management
# =============================================================================
@dataclass
class RootConfig:
    path: str
    writable: bool = False
    description: str = ""


@dataclass
class ServerConfig:
    roots: dict[str, RootConfig] = field(default_factory=lambda: {
        "resumes": RootConfig("data/resumes", False, "Candidate resumes (input)"),
        "jobs": RootConfig("data/jobs", False, "Job descriptions (input)"),
        "output": RootConfig("output", True, "Reports and results (writable)"),
    })
    allowed_extensions: list[str] = field(default_factory=lambda: [".txt", ".md", ".pdf", ".docx", ".json", ".csv"])
    max_file_size_mb: float = 10.0
    max_read_chars: int = 200_000
    batch_max_workers: int = 4
    batch_max_files: int = 500
    watch_poll_interval_s: float = 1.0
    watch_max_events: int = 1000
    watch_max_active: int = 10
    page_size: int = 50
    log_level: str = "info"

    # Keys a client may change at runtime via configure_server. Roots are
    # deliberately excluded: the sandbox can only be set by whoever starts the server.
    RUNTIME_MUTABLE = ("allowed_extensions", "max_file_size_mb", "max_read_chars", "batch_max_workers",
                       "batch_max_files", "watch_poll_interval_s", "page_size", "log_level")

    @classmethod
    def load(cls, config_path: str | None = None, base_dir: Path | None = None) -> "ServerConfig":
        """defaults  <  JSON config file  <  environment variables."""
        cfg = cls()
        base = base_dir or Path(__file__).resolve().parent
        config_path = config_path or os.environ.get("MCP_FS_CONFIG")
        if config_path:
            path = Path(config_path).expanduser()
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            raw = json.loads(path.read_text(encoding="utf-8"))
            base = path.parent
            if "roots" in raw:
                cfg.roots = {name: RootConfig(**spec) for name, spec in raw.pop("roots").items()}
            for key, value in raw.items():
                if key.startswith("_"):
                    continue
                if not hasattr(cfg, key):
                    raise ValueError(f"Unknown config key: {key}")
                setattr(cfg, key, value)
        for root_name in list(cfg.roots):
            env = os.environ.get(f"MCP_FS_{root_name.upper()}_DIR")
            if env:
                cfg.roots[root_name].path = env
        if os.environ.get("MCP_FS_LOG_LEVEL"):
            cfg.log_level = os.environ["MCP_FS_LOG_LEVEL"].lower()
        # Resolve root paths relative to the config file (or this script).
        for root in cfg.roots.values():
            p = Path(root.path).expanduser()
            root.path = str((p if p.is_absolute() else base / p).resolve())
        cfg.allowed_extensions = [e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.allowed_extensions]
        return cfg

    def to_dict(self) -> dict:
        d = asdict(self)
        d["runtime_mutable_keys"] = list(self.RUNTIME_MUTABLE)
        return d


# =============================================================================
# File-system service (the Milestone 1 tool logic, now sandboxed)
# =============================================================================
TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv"}
EXTRA_MIME = {
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def fs_error(code: int, message: str, **data) -> MCPError:
    return MCPError(code, message, data or None)


class FileSystemService:
    """Sandboxed file operations. Every path is resolved inside a configured root."""

    def __init__(self, config: ServerConfig):
        self.config = config
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._cache_max = 256

    # ------------------------------------------------------------ paths
    def roots(self) -> dict[str, Path]:
        return {name: Path(r.path) for name, r in self.config.roots.items()}

    def resolve(self, path: str, *, expect: str = "any", for_write: bool = False) -> tuple[str, Path]:
        """Map ``resumes/alice.pdf`` (or an absolute path inside a root, or an
        ``fs://`` URI) to ``(root_name, absolute_path)``. Rejects anything that
        escapes the sandbox, including via ``..`` or symlinks."""
        if not isinstance(path, str) or not path.strip():
            raise fs_error(ErrorCode.INVALID_PARAMS, "Path must be a non-empty string")
        p = path.strip().replace("\\", "/")
        if p.startswith("fs://"):
            p = p[len("fs://"):]
        roots = self.roots()
        candidate: Path | None = None
        root_name: str | None = None
        if os.path.isabs(p):
            candidate = Path(p).resolve()
            for name, rp in roots.items():
                if candidate == rp or candidate.is_relative_to(rp):
                    root_name = name
                    break
        else:
            first, _, rest = p.partition("/")
            if first in roots:
                root_name = first
                candidate = (roots[first] / rest).resolve() if rest else roots[first]
        if root_name is None or candidate is None:
            raise fs_error(ErrorCode.ACCESS_DENIED,
                           f"Path is outside the allowed roots: {path}",
                           path=path, hint=f"Start paths with one of: {', '.join(r + '/' for r in roots)}")
        if not (candidate == roots[root_name] or candidate.is_relative_to(roots[root_name])):
            raise fs_error(ErrorCode.ACCESS_DENIED, f"Path escapes root '{root_name}': {path}", path=path)
        if for_write and not self.config.roots[root_name].writable:
            raise fs_error(ErrorCode.READ_ONLY, f"Root '{root_name}' is read-only", root=root_name,
                           writable_roots=[n for n, r in self.config.roots.items() if r.writable])
        if expect == "file" and not candidate.is_file():
            raise fs_error(ErrorCode.RESOURCE_NOT_FOUND, f"File not found: {path}", path=path)
        if expect == "dir" and not candidate.is_dir():
            raise fs_error(ErrorCode.RESOURCE_NOT_FOUND, f"Directory not found: {path}", path=path)
        return root_name, candidate

    def rel(self, abs_path: Path) -> str:
        """Absolute path -> sandbox path ``root/relative``."""
        abs_path = abs_path.resolve()
        for name, rp in self.roots().items():
            if abs_path == rp:
                return name
            if abs_path.is_relative_to(rp):
                return f"{name}/{abs_path.relative_to(rp).as_posix()}"
        return abs_path.name

    def uri(self, abs_path: Path) -> str:
        return "fs://" + self.rel(abs_path)

    def _check_extension(self, abs_path: Path) -> str:
        ext = abs_path.suffix.lower()
        if ext not in self.config.allowed_extensions:
            raise fs_error(ErrorCode.UNSUPPORTED_FILE_TYPE, f"Unsupported file type '{ext or '(none)'}'",
                           path=self.rel(abs_path), allowed=self.config.allowed_extensions)
        return ext

    def _check_size(self, abs_path: Path) -> int:
        size = abs_path.stat().st_size
        limit = int(self.config.max_file_size_mb * 1024 * 1024)
        if size > limit:
            raise fs_error(ErrorCode.FILE_TOO_LARGE, f"File exceeds {self.config.max_file_size_mb} MB limit",
                           path=self.rel(abs_path), size_bytes=size, limit_bytes=limit)
        return size

    @staticmethod
    def mime_type(abs_path: Path) -> str:
        ext = abs_path.suffix.lower()
        return EXTRA_MIME.get(ext) or mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"

    def iter_files(self, directory: Path, pattern: str = "*", recursive: bool = False,
                   include_hidden: bool = False, only_allowed: bool = True) -> list[Path]:
        patterns = [p.strip() for p in pattern.split(",") if p.strip()] or ["*"]
        it = directory.rglob("*") if recursive else directory.glob("*")
        out = []
        for f in it:
            if not f.is_file():
                continue
            relparts = f.relative_to(directory).parts
            if not include_hidden and any(part.startswith(".") for part in relparts):
                continue
            if only_allowed and f.suffix.lower() not in self.config.allowed_extensions:
                continue
            if not any(fnmatch.fnmatch(f.name.lower(), pat.lower()) for pat in patterns):
                continue
            out.append(f)
        return sorted(out)

    # ---------------------------------------------------------- tools
    def list_roots(self) -> dict:
        return {"roots": [
            {"name": n, "uri": f"fs://{n}", "writable": r.writable, "description": r.description,
             "exists": Path(r.path).is_dir()}
            for n, r in self.config.roots.items()]}

    def list_files(self, directory: str = "resumes", pattern: str = "*", recursive: bool = False,
                   include_hidden: bool = False) -> dict:
        _, d = self.resolve(directory, expect="dir")
        files = []
        for f in self.iter_files(d, pattern, recursive, include_hidden):
            st = f.stat()
            files.append({
                "path": self.rel(f), "name": f.name, "extension": f.suffix.lower(),
                "size_bytes": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "uri": self.uri(f),
            })
        return {"directory": self.rel(d), "pattern": pattern, "recursive": recursive, "count": len(files),
                "files": files}

    def get_file_metadata(self, path: str) -> dict:
        _, f = self.resolve(path, expect="file")
        st = f.stat()
        h = hashlib.sha256()
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return {
            "path": self.rel(f), "name": f.name, "extension": f.suffix.lower(), "mime_type": self.mime_type(f),
            "size_bytes": st.st_size,
            "created": datetime.fromtimestamp(st.st_ctime, timezone.utc).isoformat(),
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "sha256": h.hexdigest(), "uri": self.uri(f),
            "supported": f.suffix.lower() in self.config.allowed_extensions,
        }

    def read_file(self, path: str, max_chars: int | None = None) -> dict:
        _, f = self.resolve(path, expect="file")
        ext = self._check_extension(f)
        if ext not in TEXT_EXTENSIONS:
            raise fs_error(ErrorCode.UNSUPPORTED_FILE_TYPE, f"read_file handles plain-text files; use extract_text for {ext}",
                           path=self.rel(f), hint="extract_text")
        self._check_size(f)
        text = f.read_text(encoding="utf-8", errors="replace")
        return self._clip({"path": self.rel(f), "format": ext.lstrip("."), "content": text}, "content", max_chars)

    def _clip(self, result: dict, key: str, max_chars: int | None) -> dict:
        limit = min(max_chars or self.config.max_read_chars, self.config.max_read_chars)
        text = result[key]
        result["chars"] = len(text)
        result["truncated"] = len(text) > limit
        if result["truncated"]:
            result[key] = text[:limit]
        return result

    def extract_text(self, path: str, max_chars: int | None = None) -> dict:
        _, f = self.resolve(path, expect="file")
        ext = self._check_extension(f)
        size = self._check_size(f)
        st = f.stat()
        key = (str(f), st.st_mtime_ns, size)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached:
                self._cache.move_to_end(key)
        if cached is None:
            cached = self._extract(f, ext)
            with self._cache_lock:
                self._cache[key] = cached
                while len(self._cache) > self._cache_max:
                    self._cache.popitem(last=False)
        result = {"path": self.rel(f), "format": ext.lstrip("."), **cached, "uri": self.uri(f)}
        result = dict(result)
        return self._clip(result, "text", max_chars)

    def _extract(self, f: Path, ext: str) -> dict:
        try:
            if ext in TEXT_EXTENSIONS:
                return {"text": f.read_text(encoding="utf-8", errors="replace"), "pages": None}
            if ext == ".pdf":
                try:
                    from pypdf import PdfReader
                except ImportError:
                    raise fs_error(ErrorCode.DEPENDENCY_MISSING, "PDF support needs 'pypdf' (pip install pypdf)")
                reader = PdfReader(str(f))
                pages = [(page.extract_text() or "") for page in reader.pages]
                text = "\n\n".join(pages).strip()
                if not text:
                    raise fs_error(ErrorCode.EXTRACTION_FAILED, "PDF contains no extractable text (scanned image?)",
                                   path=self.rel(f), pages=len(pages))
                return {"text": text, "pages": len(pages)}
            if ext == ".docx":
                try:
                    import docx  # python-docx
                except ImportError:
                    raise fs_error(ErrorCode.DEPENDENCY_MISSING, "DOCX support needs 'python-docx'")
                document = docx.Document(str(f))
                parts = [p.text for p in document.paragraphs if p.text.strip()]
                for table in document.tables:
                    for row in table.rows:
                        parts.append(" | ".join(c.text.strip() for c in row.cells))
                return {"text": "\n".join(parts), "pages": None}
        except MCPError:
            raise
        except Exception as exc:  # noqa: BLE001 - corrupt files etc.
            raise fs_error(ErrorCode.EXTRACTION_FAILED, f"Could not extract text: {exc}",
                           path=self.rel(f), exception=type(exc).__name__) from exc
        raise fs_error(ErrorCode.UNSUPPORTED_FILE_TYPE, f"No extractor for {ext}", path=self.rel(f))

    def search_files(self, query: str, directory: str = "resumes", case_sensitive: bool = False,
                     max_results: int = 20, recursive: bool = False) -> dict:
        _, d = self.resolve(directory, expect="dir")
        needle = query if case_sensitive else query.lower()
        matches, errors = [], []
        for f in self.iter_files(d, "*", recursive):
            try:
                text = self.extract_text(self.rel(f))["text"]
            except MCPError as exc:
                errors.append({"path": self.rel(f), "error": exc.message})
                continue
            hay = text if case_sensitive else text.lower()
            count = hay.count(needle)
            if count:
                i = hay.find(needle)
                snippet = text[max(0, i - 60): i + len(query) + 60].replace("\n", " ")
                matches.append({"path": self.rel(f), "occurrences": count, "snippet": f"...{snippet}...",
                                "uri": self.uri(f)})
        matches.sort(key=lambda m: -m["occurrences"])
        return {"query": query, "directory": self.rel(d), "total_matches": len(matches),
                "matches": matches[:max_results], "errors": errors}

    def write_file(self, path: str, content: str, overwrite: bool = False) -> dict:
        _, f = self.resolve(path, for_write=True)
        self._check_extension(f)
        if f.exists() and not overwrite:
            raise fs_error(ErrorCode.ALREADY_EXISTS, f"File exists: {path}", path=path,
                           hint="pass overwrite=true to replace it")
        data = content.encode("utf-8")
        if len(data) > self.config.max_file_size_mb * 1024 * 1024:
            raise fs_error(ErrorCode.FILE_TOO_LARGE, "Content exceeds max_file_size_mb", size_bytes=len(data))
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, f)  # atomic on the same filesystem
        return {"path": self.rel(f), "bytes_written": len(data), "uri": self.uri(f), "overwritten": overwrite}

    def file_hash(self, path: str) -> dict:
        meta = self.get_file_metadata(path)
        return {"path": meta["path"], "sha256": meta["sha256"], "size_bytes": meta["size_bytes"]}


# =============================================================================
# watch_directory(): polling watcher with settle detection
# =============================================================================
class DirectoryWatcher(threading.Thread):
    """Polls a directory and records created / modified / deleted events.

    Polling is used instead of OS hooks so it behaves the same on Linux, macOS,
    Windows and network drives. A new file is reported only once its size and
    mtime are unchanged across two scans ("settled"), so half-copied resumes
    are not picked up.
    """

    def __init__(self, watch_id: str, service: FileSystemService, directory: Path, patterns: str,
                 recursive: bool, interval: float, max_events: int, include_existing: bool,
                 auto_extract: bool, on_event: Callable[["DirectoryWatcher", dict], None]):
        super().__init__(name=f"watch-{watch_id}", daemon=True)
        self.watch_id = watch_id
        self.service = service
        self.directory = directory
        self.patterns = patterns
        self.recursive = recursive
        self.interval = interval
        self.auto_extract = auto_extract
        self.on_event = on_event
        self.created_at = utc_now()
        self.events: deque[dict] = deque(maxlen=max_events)
        self.dropped = 0
        self.total_events = 0
        self._seq = 0
        self._cond = threading.Condition()
        self._stop_evt = threading.Event()
        self._scan_lock = threading.Lock()
        self._pending: dict[str, tuple] = {}
        self._known: dict[str, tuple] = {} if include_existing else self._scan()

    def _scan(self) -> dict[str, tuple]:
        snap = {}
        for f in self.service.iter_files(self.directory, self.patterns, self.recursive):
            try:
                st = f.stat()
                snap[str(f)] = (st.st_mtime_ns, st.st_size)
            except FileNotFoundError:
                continue
        return snap

    def check(self) -> None:
        """Run one scan now (also called on every poll for responsiveness)."""
        with self._scan_lock:
            current = self._scan()
            for path, sig in current.items():
                if path not in self._known:
                    if self._pending.get(path) == sig:  # settled
                        self._pending.pop(path)
                        self._known[path] = sig
                        self._emit("created", Path(path), sig[1])
                    else:
                        self._pending[path] = sig
                elif self._known[path] != sig:
                    self._known[path] = sig
                    self._emit("modified", Path(path), sig[1])
            for path in list(self._known):
                if path not in current:
                    del self._known[path]
                    self._emit("deleted", Path(path), None)
            for path in list(self._pending):
                if path not in current:
                    del self._pending[path]

    def _emit(self, kind: str, path: Path, size: int | None) -> None:
        self._seq += 1
        event = {"event_id": self._seq, "watch_id": self.watch_id, "type": kind,
                 "path": self.service.rel(path), "uri": self.service.uri(path), "size_bytes": size,
                 "timestamp": utc_now()}
        if self.auto_extract and kind in ("created", "modified"):
            try:
                ex = self.service.extract_text(event["path"], max_chars=300)
                event["extraction"] = {"status": "ok", "chars": ex["chars"], "preview": ex["text"][:300]}
            except MCPError as exc:
                event["extraction"] = {"status": "error", "type": exc.type, "message": exc.message}
        with self._cond:
            if len(self.events) == self.events.maxlen:
                self.dropped += 1
            self.events.append(event)
            self.total_events += 1
            self._cond.notify_all()
        try:
            self.on_event(self, event)
        except Exception:  # noqa: BLE001
            log.exception("watch event callback failed")

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("watch %s scan failed", self.watch_id)

    def poll(self, timeout: float = 0.0, max_events: int = 100) -> list[dict]:
        deadline = time.monotonic() + timeout
        while True:
            self.check()
            with self._cond:
                if self.events or self._stop_evt.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, self.interval))
        with self._cond:
            out = []
            while self.events and len(out) < max_events:
                out.append(self.events.popleft())
            return out

    def stop(self) -> None:
        self._stop_evt.set()
        with self._cond:
            self._cond.notify_all()

    def describe(self) -> dict:
        return {"watch_id": self.watch_id, "directory": self.service.rel(self.directory), "patterns": self.patterns,
                "recursive": self.recursive, "interval_s": self.interval, "auto_extract": self.auto_extract,
                "created_at": self.created_at, "pending_events": len(self.events), "total_events": self.total_events,
                "dropped_events": self.dropped, "tracked_files": len(self._known), "uri": f"watch://{self.watch_id}/events"}


# =============================================================================
# The filesystem MCP server
# =============================================================================
INSTRUCTIONS = """\
Sandboxed file access for a resume-matching agent.
Paths always start with a root name: resumes/..., jobs/..., output/... (only output/ is writable).
Use list_files to discover resumes, extract_text for PDF/DOCX/TXT content, batch_process to handle
many files in one call, and watch_directory to be notified about newly arriving resumes.
Tool failures come back with isError=true and a structured 'error' object (code, type, message)."""


class FileSystemMCPServer(MCPServer):
    def __init__(self, config: ServerConfig):
        super().__init__(SERVER_NAME, SERVER_VERSION, INSTRUCTIONS)
        self.config = config
        self.fs = FileSystemService(config)
        self.page_size = config.page_size
        self._log_level = config.log_level if config.log_level in LOG_LEVELS else "info"
        self.watchers: dict[str, DirectoryWatcher] = {}
        self._watch_lock = threading.Lock()
        for root in config.roots.values():
            if root.writable:
                Path(root.path).mkdir(parents=True, exist_ok=True)
        self._register_tools()
        self._register_prompts()

    # ---------------------------------------------------------------- tools
    def _register_tools(self) -> None:
        fs = self.fs
        path_prop = {"type": "string", "description": "Sandbox path such as 'resumes/jane_doe.pdf'"}
        dir_prop = {"type": "string", "description": "Directory such as 'resumes' or 'resumes/2026'",
                    "default": "resumes"}
        read_only = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

        @self.tool("list_roots", "List the sandbox roots this server can access and whether they are writable.",
                   {"type": "object", "properties": {}, "additionalProperties": False},
                   title="List roots", annotations=read_only)
        def list_roots(ctx):
            return fs.list_roots()

        @self.tool("list_files", "List supported files (txt, md, pdf, docx, json, csv) in a directory.",
                   {"type": "object", "properties": {
                       "directory": dir_prop,
                       "pattern": {"type": "string", "default": "*",
                                   "description": "Glob pattern(s), comma separated, e.g. '*.pdf,*.docx'"},
                       "recursive": {"type": "boolean", "default": False},
                       "include_hidden": {"type": "boolean", "default": False}},
                    "additionalProperties": False},
                   title="List files", annotations=read_only)
        def list_files(ctx, directory="resumes", pattern="*", recursive=False, include_hidden=False):
            return fs.list_files(directory, pattern, recursive, include_hidden)

        @self.tool("read_file", "Read a plain-text file (txt, md, json, csv).",
                   {"type": "object", "properties": {
                       "path": path_prop,
                       "max_chars": {"type": "integer", "minimum": 1}},
                    "required": ["path"], "additionalProperties": False},
                   title="Read file", annotations=read_only)
        def read_file(ctx, path, max_chars=None):
            return fs.read_file(path, max_chars)

        @self.tool("extract_text", "Extract plain text from a resume or job description (PDF, DOCX, TXT, MD).",
                   {"type": "object", "properties": {
                       "path": path_prop,
                       "max_chars": {"type": "integer", "minimum": 1}},
                    "required": ["path"], "additionalProperties": False},
                   title="Extract text", annotations=read_only)
        def extract_text(ctx, path, max_chars=None):
            return fs.extract_text(path, max_chars)

        @self.tool("get_file_metadata", "Size, timestamps, MIME type and SHA-256 hash of a file.",
                   {"type": "object", "properties": {"path": path_prop},
                    "required": ["path"], "additionalProperties": False},
                   title="File metadata", annotations=read_only)
        def get_file_metadata(ctx, path):
            return fs.get_file_metadata(path)

        @self.tool("search_files", "Full-text search across files in a directory (PDF/DOCX included).",
                   {"type": "object", "properties": {
                       "query": {"type": "string", "minLength": 1},
                       "directory": dir_prop,
                       "case_sensitive": {"type": "boolean", "default": False},
                       "recursive": {"type": "boolean", "default": False},
                       "max_results": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20}},
                    "required": ["query"], "additionalProperties": False},
                   title="Search files", annotations=read_only)
        def search_files(ctx, query, directory="resumes", case_sensitive=False, recursive=False, max_results=20):
            return fs.search_files(query, directory, case_sensitive, max_results, recursive)

        @self.tool("write_file", "Write a text file into a writable root (output/). Atomic write.",
                   {"type": "object", "properties": {
                       "path": {"type": "string", "description": "e.g. 'output/report.json'"},
                       "content": {"type": "string"},
                       "overwrite": {"type": "boolean", "default": False}},
                    "required": ["path", "content"], "additionalProperties": False},
                   title="Write file",
                   annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True,
                                "openWorldHint": False})
        def write_file(ctx, path, content, overwrite=False):
            result = fs.write_file(path, content, overwrite)
            self.notify("notifications/resources/list_changed")
            return result

        @self.tool("watch_directory",
                   "Monitor a directory for new, changed or deleted resumes. "
                   "action=start returns a watch_id; action=poll returns events since the last poll "
                   "(optionally waiting up to timeout_s); action=stop ends the watch; action=list shows active watches. "
                   "On stdio, new files also trigger notifications/resources/list_changed and notifications/message.",
                   {"type": "object", "properties": {
                       "action": {"type": "string", "enum": ["start", "poll", "stop", "list"]},
                       "directory": dir_prop,
                       "patterns": {"type": "string", "default": "*",
                                    "description": "Comma-separated globs, e.g. '*.pdf,*.docx'"},
                       "recursive": {"type": "boolean", "default": False},
                       "include_existing": {"type": "boolean", "default": False,
                                            "description": "Report files already present as 'created'"},
                       "auto_extract": {"type": "boolean", "default": True,
                                        "description": "Attach a text preview to created/modified events"},
                       "interval_s": {"type": "number", "minimum": 0.1, "maximum": 60},
                       "watch_id": {"type": "string"},
                       "timeout_s": {"type": "number", "minimum": 0, "maximum": 60, "default": 0},
                       "max_events": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}},
                    "required": ["action"], "additionalProperties": False},
                   title="Watch directory",
                   annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                                "openWorldHint": False})
        def watch_directory(ctx, action, directory="resumes", patterns="*", recursive=False,
                            include_existing=False, auto_extract=True, interval_s=None, watch_id=None,
                            timeout_s=0, max_events=100):
            return self.watch_directory(action, directory, patterns, recursive, include_existing, auto_extract,
                                        interval_s, watch_id, timeout_s, max_events)

        @self.tool("batch_process",
                   "Run one operation over many files in parallel: extract_text, metadata, read or hash. "
                   "Give explicit 'paths', a 'directory' (+ 'pattern'), or both. Per-file failures do not stop "
                   "the batch unless continue_on_error=false. Sends notifications/progress when the request "
                   "carries a progressToken.",
                   {"type": "object", "properties": {
                       "operation": {"type": "string", "enum": ["extract_text", "metadata", "read", "hash"]},
                       "paths": {"type": "array", "items": {"type": "string"}, "maxItems": 5000},
                       "directory": {"type": "string"},
                       "pattern": {"type": "string", "default": "*"},
                       "recursive": {"type": "boolean", "default": False},
                       "max_workers": {"type": "integer", "minimum": 1, "maximum": 32},
                       "max_chars": {"type": "integer", "minimum": 1,
                                     "description": "Truncate extracted/read text per file"},
                       "continue_on_error": {"type": "boolean", "default": True}},
                    "required": ["operation"], "additionalProperties": False},
                   title="Batch process", annotations=read_only)
        def batch_process(ctx, operation, paths=None, directory=None, pattern="*", recursive=False,
                          max_workers=None, max_chars=None, continue_on_error=True):
            return self.batch_process(ctx, operation, paths, directory, pattern, recursive, max_workers,
                                      max_chars, continue_on_error)

        @self.tool("get_server_config", "Return the server's current configuration.",
                   {"type": "object", "properties": {}, "additionalProperties": False},
                   title="Get config", annotations=read_only)
        def get_server_config(ctx):
            return self.config.to_dict()

        @self.tool("configure_server",
                   "Change runtime settings (batch_max_workers, max_file_size_mb, watch_poll_interval_s, "
                   "allowed_extensions, max_read_chars, batch_max_files, page_size, log_level). "
                   "Sandbox roots cannot be changed at runtime.",
                   {"type": "object", "properties": {
                       "settings": {"type": "object", "description": "key -> new value"}},
                    "required": ["settings"], "additionalProperties": False},
                   title="Configure server",
                   annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True,
                                "openWorldHint": False})
        def configure_server(ctx, settings):
            return self.configure(settings)

    # ------------------------------------------------------------ prompts
    def _register_prompts(self) -> None:
        @self.prompt("screen_resume", "Ask an LLM to screen one resume against a job description.",
                     [{"name": "resume_path", "description": "e.g. resumes/jane_doe.pdf", "required": True},
                      {"name": "job_path", "description": "e.g. jobs/senior_python_engineer.md", "required": True}])
        def screen_resume(args):
            resume = self.fs.extract_text(args["resume_path"], max_chars=12000)["text"]
            job = self.fs.extract_text(args["job_path"], max_chars=6000)["text"]
            return [{"role": "user", "content": {"type": "text", "text":
                     "Score this candidate 0-100 against the job, list matched and missing skills, "
                     f"and give a one-paragraph rationale.\n\n## Job\n{job}\n\n## Resume\n{resume}"}}]

    # ------------------------------------------------- watch_directory()
    def _on_watch_event(self, watcher: DirectoryWatcher, event: dict) -> None:
        self.log_to_client("info", {"event": "file_" + event["type"], **event}, "watch_directory")
        if event["type"] in ("created", "deleted"):
            self.notify("notifications/resources/list_changed")
        if event["uri"] in self.subscriptions and event["type"] in ("modified", "deleted"):
            self.notify("notifications/resources/updated", {"uri": event["uri"]})

    def watch_directory(self, action, directory, patterns, recursive, include_existing, auto_extract,
                        interval_s, watch_id, timeout_s, max_events) -> dict:
        if action == "list":
            with self._watch_lock:
                return {"watches": [w.describe() for w in self.watchers.values()]}
        if action == "start":
            _, d = self.fs.resolve(directory, expect="dir")
            with self._watch_lock:
                if len(self.watchers) >= self.config.watch_max_active:
                    raise fs_error(ErrorCode.LIMIT_EXCEEDED, "Too many active watches",
                                   limit=self.config.watch_max_active)
                wid = "w_" + uuid.uuid4().hex[:10]
                watcher = DirectoryWatcher(wid, self.fs, d, patterns, recursive,
                                           interval_s or self.config.watch_poll_interval_s,
                                           self.config.watch_max_events, include_existing, auto_extract,
                                           self._on_watch_event)
                self.watchers[wid] = watcher
            watcher.start()
            return {"status": "watching", **watcher.describe()}
        if not watch_id:
            raise fs_error(ErrorCode.INVALID_PARAMS, f"action={action} requires watch_id")
        with self._watch_lock:
            watcher = self.watchers.get(watch_id)
        if watcher is None:
            raise fs_error(ErrorCode.WATCH_NOT_FOUND, f"No active watch with id {watch_id}", watch_id=watch_id)
        if action == "poll":
            events = watcher.poll(timeout=float(timeout_s or 0), max_events=max_events)
            return {"watch_id": watch_id, "count": len(events), "events": events,
                    "pending_events": len(watcher.events), "dropped_events": watcher.dropped}
        # stop
        watcher.stop()
        with self._watch_lock:
            self.watchers.pop(watch_id, None)
        remaining = list(watcher.events)
        return {"status": "stopped", "watch_id": watch_id, "total_events": watcher.total_events,
                "undelivered_events": remaining}

    # --------------------------------------------------- batch_process()
    def batch_process(self, ctx: RequestContext, operation, paths, directory, pattern, recursive,
                      max_workers, max_chars, continue_on_error) -> dict:
        if not paths and not directory:
            raise fs_error(ErrorCode.INVALID_PARAMS, "Provide 'paths', 'directory', or both")
        targets: list[str] = []
        seen = set()
        for p in paths or []:
            if p not in seen:
                seen.add(p)
                targets.append(p)
        if directory:
            _, d = self.fs.resolve(directory, expect="dir")
            for f in self.fs.iter_files(d, pattern, recursive):
                rp = self.fs.rel(f)
                if rp not in seen:
                    seen.add(rp)
                    targets.append(rp)
        if len(targets) > self.config.batch_max_files:
            raise fs_error(ErrorCode.LIMIT_EXCEEDED, f"Batch of {len(targets)} files exceeds batch_max_files",
                           limit=self.config.batch_max_files)

        ops: dict[str, Callable[[str], dict]] = {
            "extract_text": lambda p: self.fs.extract_text(p, max_chars),
            "read": lambda p: self.fs.read_file(p, max_chars),
            "metadata": self.fs.get_file_metadata,
            "hash": self.fs.file_hash,
        }
        op = ops[operation]
        workers = max(1, min(max_workers or self.config.batch_max_workers, 32, len(targets) or 1))
        results: list[dict | None] = [None] * len(targets)
        started = time.perf_counter()
        done_count = 0
        stop_reason = None

        def run_one(idx: int, p: str) -> tuple[int, dict]:
            t0 = time.perf_counter()
            try:
                data = op(p)
                return idx, {"path": p, "status": "ok", "data": data,
                             "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}
            except MCPError as exc:
                return idx, {"path": p, "status": "error",
                             "error": {"code": exc.code, "type": exc.type, "message": exc.message},
                             "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2)}

        ctx.report_progress(0, len(targets), f"starting {operation} on {len(targets)} files")
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch") as pool:
            pending = {pool.submit(run_one, i, p) for i, p in enumerate(targets)}
            while pending:
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    idx, res = fut.result()
                    results[idx] = res
                    done_count += 1
                    ctx.report_progress(done_count, len(targets), f"{res['status']}: {res['path']}")
                    if res["status"] == "error" and not continue_on_error and stop_reason is None:
                        stop_reason = f"stopped after error on {res['path']}"
                if ctx.cancelled.is_set() and stop_reason is None:
                    stop_reason = "cancelled by client"
                if stop_reason:
                    for fut in pending:
                        fut.cancel()
                    finished, _ = wait(pending)  # let running ones finish
                    for fut in finished:
                        if not fut.cancelled():
                            idx, res = fut.result()
                            results[idx] = res
                    break

        final = [r if r is not None else {"path": targets[i], "status": "skipped"} for i, r in enumerate(results)]
        summary = {s: sum(1 for r in final if r["status"] == s) for s in ("ok", "error", "skipped")}
        return {
            "operation": operation, "total": len(targets), "succeeded": summary["ok"], "failed": summary["error"],
            "skipped": summary["skipped"], "workers": workers,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "stopped_early": stop_reason, "results": final,
        }

    # -------------------------------------------------- configuration
    def configure(self, settings: dict) -> dict:
        changed, rejected = {}, {}
        checks = {
            "batch_max_workers": lambda v: isinstance(v, int) and 1 <= v <= 32,
            "batch_max_files": lambda v: isinstance(v, int) and 1 <= v <= 10000,
            "max_file_size_mb": lambda v: isinstance(v, (int, float)) and 0 < v <= 512,
            "max_read_chars": lambda v: isinstance(v, int) and 100 <= v <= 5_000_000,
            "watch_poll_interval_s": lambda v: isinstance(v, (int, float)) and 0.1 <= v <= 60,
            "page_size": lambda v: isinstance(v, int) and 1 <= v <= 1000,
            "log_level": lambda v: v in LOG_LEVELS,
            "allowed_extensions": lambda v: isinstance(v, list) and all(isinstance(e, str) for e in v),
        }
        for key, value in settings.items():
            if key not in self.config.RUNTIME_MUTABLE:
                rejected[key] = "not runtime-configurable"
            elif not checks[key](value):
                rejected[key] = "invalid value"
            else:
                if key == "allowed_extensions":
                    value = [e.lower() if e.startswith(".") else "." + e.lower() for e in value]
                old = getattr(self.config, key)
                setattr(self.config, key, value)
                changed[key] = {"old": old, "new": value}
        if "page_size" in changed:
            self.page_size = self.config.page_size
        if "log_level" in changed:
            self._log_level = self.config.log_level
        if rejected and not changed:
            raise fs_error(ErrorCode.INVALID_PARAMS, "No settings applied", rejected=rejected)
        if changed:
            self.notify("notifications/resources/updated", {"uri": "config://server"})
        return {"changed": changed, "rejected": rejected}

    # -------------------------------------------------------- resources
    def resource_list(self) -> list[dict]:
        items = [
            {"uri": "config://server", "name": "server-config", "title": "Server configuration",
             "mimeType": "application/json", "description": "Live configuration of this server"},
            {"uri": "tools://catalog", "name": "tool-catalog", "title": "Tool catalog",
             "mimeType": "application/json", "description": "All tools with their input schemas"},
            {"uri": "watch://active", "name": "active-watches", "title": "Active watches",
             "mimeType": "application/json", "description": "Directory watches currently running"},
        ]
        for name, rp in self.fs.roots().items():
            if not rp.is_dir():
                continue
            for f in self.fs.iter_files(rp, "*", recursive=True):
                st = f.stat()
                items.append({
                    "uri": self.fs.uri(f), "name": f.name, "title": f"{name}: {f.name}",
                    "mimeType": self.fs.mime_type(f), "size": st.st_size,
                    "description": f"{self.config.roots[name].description or name} - {f.suffix.lower()} file",
                    "annotations": {"audience": ["assistant"],
                                    "lastModified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()},
                })
        return items

    def resource_templates(self) -> list[dict]:
        return [
            {"uriTemplate": "fs://{root}/{path}", "name": "file", "title": "File contents",
             "description": "Text of a file; PDF and DOCX are returned as extracted text", "mimeType": "text/plain"},
            {"uriTemplate": "fs-meta://{root}/{path}", "name": "file-metadata", "title": "File metadata",
             "description": "Size, timestamps, MIME type and SHA-256", "mimeType": "application/json"},
            {"uriTemplate": "tools://{name}", "name": "tool-schema", "title": "Tool schema",
             "description": "Description and input schema of one tool", "mimeType": "application/json"},
            {"uriTemplate": "watch://{watch_id}/events", "name": "watch-events", "title": "Watch events",
             "description": "Pending events of a watch (reading drains them)", "mimeType": "application/json"},
        ]

    def resource_read(self, uri: str) -> list[dict]:
        def as_json(obj) -> list[dict]:
            return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(obj, indent=2, default=str)}]

        try:
            if uri == "config://server":
                return as_json(self.config.to_dict())
            if uri == "tools://catalog":
                return as_json({"tools": [t.describe() for t in self.tools.values()]})
            if uri.startswith("tools://"):
                spec = self.tools.get(uri[len("tools://"):])
                if spec:
                    return as_json(spec.describe())
            elif uri == "watch://active":
                with self._watch_lock:
                    return as_json({"watches": [w.describe() for w in self.watchers.values()]})
            elif uri.startswith("watch://") and uri.endswith("/events"):
                wid = uri[len("watch://"):-len("/events")]
                watcher = self.watchers.get(wid)
                if watcher:
                    return as_json({"watch_id": wid, "events": watcher.poll(0)})
            elif uri.startswith("fs-meta://"):
                return as_json(self.fs.get_file_metadata(uri[len("fs-meta://"):]))
            elif uri.startswith("fs://"):
                path = uri[len("fs://"):]
                _, target = self.fs.resolve(path)
                if target.is_dir():
                    return as_json(self.fs.list_files(path))
                result = self.fs.extract_text(path)
                return [{"uri": uri, "mimeType": "text/plain" if result["format"] not in ("md", "json", "csv")
                         else {"md": "text/markdown", "json": "application/json", "csv": "text/csv"}[result["format"]],
                         "text": result["text"]}]
        except MCPError as exc:
            if exc.code in (ErrorCode.RESOURCE_NOT_FOUND, ErrorCode.ACCESS_DENIED):
                raise MCPError(ErrorCode.RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri, "reason": exc.message})
            raise
        raise MCPError(ErrorCode.RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})

    def describe_extra(self) -> dict:
        return {"roots": self.fs.list_roots()["roots"], "activeWatches": len(self.watchers)}

    def shutdown(self) -> None:
        with self._watch_lock:
            for w in self.watchers.values():
                w.stop()
            self.watchers.clear()


# =============================================================================
# Entry point
# =============================================================================
def build_server(config_path: str | None = None) -> FileSystemMCPServer:
    return FileSystemMCPServer(ServerConfig.load(config_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resume filesystem MCP server (JSON-RPC 2.0)")
    parser.add_argument("--config", help="Path to a JSON config file (or set MCP_FS_CONFIG)")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default=os.environ.get("MCP_FS_LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s [fs-mcp] %(message)s")
    try:
        server = build_server(args.config)
    except (OSError, ValueError, TypeError) as exc:
        log.error("invalid configuration: %s", exc)
        return 2
    log.info("roots: %s", {n: r.path for n, r in server.config.roots.items()})
    if args.transport == "http":
        server.serve_http(args.host, args.port)
    else:
        server.serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
