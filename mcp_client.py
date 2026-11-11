"""
mcp_client.py
=============

A small, dependency-free MCP client used by the matching agent.

* ``StdioMCPClient``  launches an MCP server as a subprocess and speaks
  newline-delimited JSON-RPC 2.0 over its stdin/stdout (the standard MCP
  transport). Works with this project's servers and with any other
  spec-compliant MCP server (Node, Python SDK, ...).
* ``HttpMCPClient``   talks to a server started with ``--transport http``.
* ``MultiMCPClient``  connects to several servers from one config file and
  routes calls by server name; ``to_langchain_tools()`` turns every MCP tool
  into a LangChain ``StructuredTool`` so an LLM agent can bind them directly.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("mcp-client")

CLIENT_INFO = {"name": "resume-matching-agent", "version": "2.0.0"}
PROTOCOL_VERSION = "2025-06-18"


class MCPClientError(Exception):
    """A JSON-RPC error returned by the server (or a transport failure)."""

    def __init__(self, code: int, message: str, data: Any = None, server: str = "?"):
        super().__init__(f"[{server}] {code}: {message}")
        self.code, self.message, self.data, self.server = code, message, data, server


class MCPToolError(Exception):
    """A tool ran but reported failure (result.isError == true)."""

    def __init__(self, tool: str, payload: Any, server: str = "?"):
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        self.tool, self.payload, self.server = tool, payload, server
        self.code = err.get("code")
        self.type = err.get("type", "TOOL_ERROR")
        self.message = err.get("message", str(payload))
        super().__init__(f"[{server}.{tool}] {self.type}: {self.message}")


# =============================================================================
# Base client: everything above the transport
# =============================================================================
class BaseMCPClient:
    def __init__(self, name: str, timeout: float = 60.0):
        self.name = name
        self.timeout = timeout
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions: str = ""
        self.protocol_version: str | None = None
        self.notifications: deque[dict] = deque(maxlen=500)
        self._listeners: list[Callable[[str, dict], None]] = []
        self._tools_cache: list[dict] | None = None

    # transport hooks --------------------------------------------------------
    def _request(self, method: str, params: dict | None, timeout: float | None,
                 on_progress: Callable[[dict], None] | None = None) -> Any:
        raise NotImplementedError

    def _notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    def start(self) -> "BaseMCPClient":
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # lifecycle ---------------------------------------------------------------
    def _handshake(self) -> None:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        }, timeout=self.timeout)
        self.protocol_version = result.get("protocolVersion")
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {})
        self.instructions = result.get("instructions", "")
        self._notify("notifications/initialized")
        log.info("[%s] connected to %s %s (protocol %s)", self.name, self.server_info.get("name"),
                 self.server_info.get("version"), self.protocol_version)

    def on_notification(self, callback: Callable[[str, dict], None]) -> None:
        self._listeners.append(callback)

    def _dispatch_notification(self, method: str, params: dict) -> None:
        self.notifications.append({"method": method, "params": params, "received": time.time()})
        if method == "notifications/tools/list_changed":
            self._tools_cache = None
        for cb in list(self._listeners):
            try:
                cb(method, params)
            except Exception:  # noqa: BLE001
                log.exception("notification listener failed")

    # MCP API -----------------------------------------------------------------
    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        return self._request(method, params, timeout)

    def _paged(self, method: str, key: str) -> list[dict]:
        items, cursor = [], None
        while True:
            result = self._request(method, {"cursor": cursor} if cursor else {}, None)
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            if not cursor:
                return items

    def ping(self) -> float:
        t0 = time.perf_counter()
        self._request("ping", {}, 10)
        return round((time.perf_counter() - t0) * 1000, 2)

    def list_tools(self, refresh: bool = False) -> list[dict]:
        if self._tools_cache is None or refresh:
            self._tools_cache = self._paged("tools/list", "tools")
        return self._tools_cache

    def list_resources(self) -> list[dict]:
        return self._paged("resources/list", "resources")

    def list_resource_templates(self) -> list[dict]:
        return self._paged("resources/templates/list", "resourceTemplates")

    def list_prompts(self) -> list[dict]:
        if "prompts" not in self.capabilities:
            return []
        return self._paged("prompts/list", "prompts")

    def read_resource(self, uri: str) -> list[dict]:
        return self._request("resources/read", {"uri": uri}, None)["contents"]

    def read_resource_text(self, uri: str) -> str:
        return "".join(c.get("text", "") for c in self.read_resource(uri))

    def read_resource_json(self, uri: str) -> Any:
        return json.loads(self.read_resource_text(uri))

    def subscribe(self, uri: str) -> None:
        self._request("resources/subscribe", {"uri": uri}, None)

    def set_log_level(self, level: str) -> None:
        if "logging" in self.capabilities:
            self._request("logging/setLevel", {"level": level}, None)

    def call_tool_raw(self, name: str, arguments: dict | None = None, timeout: float | None = None,
                      on_progress: Callable[[dict], None] | None = None) -> dict:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        return self._request("tools/call", params, timeout, on_progress)

    def call_tool(self, name: str, arguments: dict | None = None, *, timeout: float | None = None,
                  on_progress: Callable[[dict], None] | None = None, raise_on_error: bool = True) -> Any:
        """Call a tool and return its structured result (parsed JSON).

        Raises ``MCPToolError`` when the tool reports ``isError`` (unless
        ``raise_on_error=False``) and ``MCPClientError`` for protocol errors.
        """
        result = self.call_tool_raw(name, arguments, timeout, on_progress)
        payload = result.get("structuredContent")
        if payload is None:
            text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = text
        if result.get("isError") and raise_on_error:
            raise MCPToolError(name, payload, self.name)
        return payload

    def discover(self) -> dict:
        """Full inventory. Uses the one-shot ``server/discover`` extension when the
        server has it, otherwise falls back to the standard list methods."""
        try:
            return self._request("server/discover", {}, None)
        except MCPClientError as exc:
            if exc.code != -32601:
                raise
        return {
            "serverInfo": self.server_info, "capabilities": self.capabilities,
            "instructions": self.instructions,
            "tools": self.list_tools(refresh=True),
            "resources": self.list_resources() if "resources" in self.capabilities else [],
            "resourceTemplates": self.list_resource_templates() if "resources" in self.capabilities else [],
            "prompts": self.list_prompts(),
        }


# =============================================================================
# stdio transport
# =============================================================================
class StdioMCPClient(BaseMCPClient):
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None,
                 cwd: str | None = None, timeout: float = 60.0, stderr_log: str | None = None):
        super().__init__(name, timeout)
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.stderr_log = stderr_log
        self.proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._progress: dict[str, Callable[[dict], None]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_fh = None

    def start(self) -> "StdioMCPClient":
        cmd = [sys.executable if self.command in ("python", "python3") else self.command, *self.args]
        env = {**os.environ, **{k: str(v) for k, v in self.env.items()}}
        if self.stderr_log:
            Path(self.stderr_log).parent.mkdir(parents=True, exist_ok=True)
            self._stderr_fh = open(self.stderr_log, "a", encoding="utf-8")  # noqa: SIM115
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr_fh or subprocess.DEVNULL, cwd=self.cwd, env=env)
        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-{self.name}-reader", daemon=True)
        self._reader.start()
        try:
            self._handshake()
        except Exception:
            self.close()
            raise
        return self

    def _send(self, message: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise MCPClientError(-32000, "Server process is not running", server=self.name)
        data = (json.dumps(message) + "\n").encode("utf-8")
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("[%s] non-JSON output ignored: %s", self.name, line[:200])
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                self._handle_incoming(m)
        # Process ended: fail everything still waiting.
        with self._lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(MCPClientError(-32000, "Server process exited", server=self.name))

    def _handle_incoming(self, msg: dict) -> None:
        if "method" in msg and "id" not in msg:  # notification
            params = msg.get("params", {}) or {}
            if msg["method"] == "notifications/progress":
                cb = self._progress.get(str(params.get("progressToken")))
                if cb:
                    cb(params)
            self._dispatch_notification(msg["method"], params)
        elif "method" in msg:  # server -> client request (e.g. roots/list, ping)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
            if msg["method"] == "ping":
                reply["result"] = {}
            elif msg["method"] == "roots/list":
                reply["result"] = {"roots": []}
            else:
                reply["error"] = {"code": -32601, "message": f"Method not found: {msg['method']}"}
            self._send(reply)
        else:  # response
            with self._lock:
                fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    def _request(self, method, params, timeout, on_progress=None):
        req_id = next(self._ids)
        params = dict(params or {})
        token = None
        if on_progress:
            token = f"{self.name}-{req_id}"
            params["_meta"] = {"progressToken": token}
            self._progress[token] = on_progress
        fut: Future = Future()
        with self._lock:
            self._pending[req_id] = fut
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            try:
                msg = fut.result(timeout=timeout or self.timeout)
            except FutureTimeout:
                with self._lock:
                    self._pending.pop(req_id, None)
                self._notify("notifications/cancelled", {"requestId": req_id, "reason": "client timeout"})
                raise MCPClientError(-32001, f"Timed out after {timeout or self.timeout}s waiting for {method}",
                                     server=self.name) from None
        finally:
            if token:
                self._progress.pop(token, None)
        if "error" in msg:
            e = msg["error"]
            raise MCPClientError(e.get("code", -32603), e.get("message", "error"), e.get("data"), self.name)
        return msg.get("result")

    def _notify(self, method, params=None):
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def close(self) -> None:
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()  # EOF = graceful shutdown for stdio servers
                self.proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()
            self.proc = None
        if self._stderr_fh:
            self._stderr_fh.close()
            self._stderr_fh = None


# =============================================================================
# HTTP transport (for servers started with --transport http)
# =============================================================================
class HttpMCPClient(BaseMCPClient):
    def __init__(self, name: str, url: str, timeout: float = 60.0, headers: dict | None = None):
        super().__init__(name, timeout)
        self.url = url
        self.headers = headers or {}
        self._ids = itertools.count(1)

    def start(self) -> "HttpMCPClient":
        self._handshake()
        return self

    def _post(self, message: dict, timeout: float) -> dict | None:
        req = urllib.request.Request(self.url, data=json.dumps(message).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **self.headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise MCPClientError(-32000, f"HTTP {exc.code}", server=self.name) from None
        except urllib.error.URLError as exc:
            raise MCPClientError(-32000, f"Cannot reach {self.url}: {exc.reason}", server=self.name) from None

    def _request(self, method, params, timeout, on_progress=None):
        msg = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}},
                         timeout or self.timeout)
        if msg and "error" in msg:
            e = msg["error"]
            raise MCPClientError(e.get("code", -32603), e.get("message", "error"), e.get("data"), self.name)
        return (msg or {}).get("result")

    def _notify(self, method, params=None):
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._post(msg, self.timeout)


# =============================================================================
# Several servers at once
# =============================================================================
def client_from_spec(name: str, spec: dict, base_dir: Path) -> BaseMCPClient:
    transport = spec.get("transport", "stdio")
    if transport == "http":
        return HttpMCPClient(name, spec["url"], spec.get("timeout", 60), spec.get("headers"))
    # Relative paths in "args" resolve against the project root, which is the subprocess cwd.
    cwd = spec.get("cwd")
    cwd = str((base_dir / cwd).resolve()) if cwd else str(base_dir)
    return StdioMCPClient(name, spec["command"], spec.get("args", []), spec.get("env"), cwd,
                          spec.get("timeout", 60), spec.get("stderr_log") and str(base_dir / spec["stderr_log"]))


class MultiMCPClient:
    """Connects to every enabled server in an ``mcpServers`` config (the same
    shape Claude Desktop and other MCP hosts use)."""

    def __init__(self, specs: dict[str, dict], base_dir: Path, required: tuple[str, ...] = ()):
        self.specs = {n: s for n, s in specs.items() if s.get("enabled", True)}
        self.base_dir = base_dir
        self.required = required
        self.clients: dict[str, BaseMCPClient] = {}
        self.failed: dict[str, str] = {}

    @classmethod
    def from_config_file(cls, path: str | Path, only: list[str] | None = None,
                         required: tuple[str, ...] = ()) -> "MultiMCPClient":
        path = Path(path).resolve()
        config = json.loads(path.read_text(encoding="utf-8"))
        specs = config.get("mcpServers", config)
        if only is not None:
            specs = {k: v for k, v in specs.items() if k in only}
        return cls(specs, path.parent.parent if path.parent.name == "config" else path.parent, required)

    def start(self) -> "MultiMCPClient":
        for name, spec in self.specs.items():
            try:
                self.clients[name] = client_from_spec(name, spec, self.base_dir).start()
            except Exception as exc:  # noqa: BLE001 - optional servers may be absent
                self.failed[name] = str(exc)
                log.warning("could not connect to MCP server '%s': %s", name, exc)
                if name in self.required:
                    self.close()
                    raise
        return self

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def __getitem__(self, name: str) -> BaseMCPClient:
        return self.clients[name]

    def has(self, name: str) -> bool:
        return name in self.clients

    def call(self, qualified: str, arguments: dict | None = None, **kw) -> Any:
        """``call("filesystem.list_files", {...})``"""
        server, _, tool = qualified.partition(".")
        if server not in self.clients:
            raise MCPClientError(-32000, f"Server '{server}' is not connected", server=server)
        return self.clients[server].call_tool(tool, arguments, **kw)

    def all_tools(self) -> dict[str, dict]:
        return {f"{s}.{t['name']}": t for s, c in self.clients.items() for t in c.list_tools()}

    def inventory(self) -> dict[str, dict]:
        out = {}
        for name, c in self.clients.items():
            d = c.discover()
            out[name] = {
                "server": d.get("serverInfo", c.server_info), "protocol": c.protocol_version,
                "tools": [t["name"] for t in d.get("tools", [])],
                "resources": len(d.get("resources", [])),
                "resource_templates": [t["uriTemplate"] for t in d.get("resourceTemplates", [])],
                "prompts": [p["name"] for p in d.get("prompts", [])],
            }
        return out

    def to_langchain_tools(self) -> list:
        """Every MCP tool as a LangChain StructuredTool named ``server__tool``.

        Bind them to any tool-calling chat model (``llm.bind_tools(tools)``) or a
        LangGraph ``ToolNode`` to let an LLM drive the MCP servers directly.
        """
        from langchain_core.tools import StructuredTool

        tools = []
        for qualified, spec in self.all_tools().items():
            server, _, tool_name = qualified.partition(".")

            def make(server=server, tool_name=tool_name):
                def _run(**kwargs):
                    try:
                        return json.dumps(self.clients[server].call_tool(tool_name, kwargs), default=str)
                    except (MCPToolError, MCPClientError) as exc:
                        return json.dumps({"error": str(exc)})
                return _run

            tools.append(StructuredTool.from_function(
                func=make(), name=f"{server}__{tool_name}", description=spec.get("description", "")[:1000],
                args_schema=spec.get("inputSchema", {"type": "object", "properties": {}})))
        return tools
