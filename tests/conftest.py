"""Shared fixtures: an isolated copy of the sample project per test, an in-process
server, and helpers to send JSON-RPC messages."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from filesystem_mcp_server import FileSystemMCPServer, ServerConfig  # noqa: E402


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throw-away copy of data/ plus configs that point at it."""
    for sub in ("resumes", "jobs", "incoming_samples"):
        shutil.copytree(ROOT / "data" / sub, tmp_path / "data" / sub)
    (tmp_path / "output").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "filesystem_server.json").write_text(json.dumps({
        "roots": {
            "resumes": {"path": "../data/resumes", "writable": False},
            "jobs": {"path": "../data/jobs", "writable": False},
            "output": {"path": "../output", "writable": True},
        },
        "watch_poll_interval_s": 0.2,
        "page_size": 50,
    }))
    (tmp_path / "config" / "mcp_servers.json").write_text(json.dumps({"mcpServers": {
        "filesystem": {"command": "python",
                       "args": [str(ROOT / "filesystem_mcp_server.py"), "--config", "config/filesystem_server.json",
                                "--log-level", "WARNING"],
                       "stderr_log": "logs/fs.log"},
        "candidate_db": {"command": "python",
                         "args": [str(ROOT / "database_mcp_server.py"), "--db", "data/candidates.db",
                                  "--log-level", "WARNING"],
                         "stderr_log": "logs/db.log"},
    }}))
    return tmp_path


@pytest.fixture
def server(project: Path) -> FileSystemMCPServer:
    srv = FileSystemMCPServer(ServerConfig.load(str(project / "config" / "filesystem_server.json")))
    sent: list[dict] = []
    srv._writer = lambda payload: sent.append(json.loads(payload))  # capture notifications
    srv.sent = sent
    srv.handle_message({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "pytest", "version": "1"}}})
    srv.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield srv
    srv.shutdown()


_ids = iter(range(1, 10**9))


def rpc(srv, method: str, params: dict | None = None) -> dict:
    msg = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        msg["params"] = params
    return srv.handle_message(msg)


def call(srv, tool: str, **arguments) -> dict:
    """tools/call -> the full result object (content, structuredContent, isError)."""
    resp = rpc(srv, "tools/call", {"name": tool, "arguments": arguments})
    assert "result" in resp, resp
    return resp["result"]
