"""Scenario group 4 - real transports: stdio subprocess, HTTP, and interop with the
official MCP Python SDK client (proves the server is spec-compliant, not just
compatible with our own client)."""
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT
from mcp_client import HttpMCPClient, MCPClientError, MCPToolError, StdioMCPClient


def fs_client(project):
    return StdioMCPClient("filesystem", sys.executable,
                          [str(ROOT / "filesystem_mcp_server.py"), "--config", "config/filesystem_server.json",
                           "--log-level", "WARNING"], cwd=str(project), timeout=20)


def test_stdio_handshake_and_tools(project):
    with fs_client(project) as c:
        assert c.server_info["name"] == "resume-filesystem-mcp" and c.protocol_version == "2025-06-18"
        assert "watch_directory" in {t["name"] for t in c.list_tools()}
        assert c.ping() >= 0
        assert c.call_tool("extract_text", {"path": "resumes/arjun_mehta.docx"})["text"].startswith("Arjun Mehta")
        with pytest.raises(MCPToolError) as tool_err:
            c.call_tool("extract_text", {"path": "../secret"})
        assert tool_err.value.type == "ACCESS_DENIED"
        with pytest.raises(MCPClientError) as proto_err:
            c.request("bogus/method")
        assert proto_err.value.code == -32601


def test_stdio_push_notifications_for_new_resume(project):
    seen = []
    with fs_client(project) as c:
        c.on_notification(lambda m, p: seen.append((m, p)))
        wid = c.call_tool("watch_directory", {"action": "start", "directory": "resumes"})["watch_id"]
        shutil.copy(project / "data/incoming_samples/kavya_nair.txt", project / "data/resumes/")
        events = c.call_tool("watch_directory", {"action": "poll", "watch_id": wid, "timeout_s": 5})["events"]
        assert events[0]["type"] == "created"
        time.sleep(0.2)
    methods = [m for m, _ in seen]
    assert "notifications/resources/list_changed" in methods
    msg = next(p for m, p in seen if m == "notifications/message")
    assert msg["logger"] == "watch_directory" and msg["data"]["path"] == "resumes/kavya_nair.txt"


def test_stdio_progress_callback(project):
    ticks = []
    with fs_client(project) as c:
        r = c.call_tool("batch_process", {"operation": "extract_text", "directory": "resumes"},
                        on_progress=ticks.append)
    assert r["total"] == 7 and ticks[-1]["progress"] == 7


def test_requests_run_concurrently(project):
    """A long poll must not block other requests on the same connection."""
    import threading
    with fs_client(project) as c:
        wid = c.call_tool("watch_directory", {"action": "start", "directory": "resumes"})["watch_id"]
        t = threading.Thread(target=c.call_tool, args=("watch_directory",
                                                       {"action": "poll", "watch_id": wid, "timeout_s": 3}))
        t.start()
        time.sleep(0.2)
        t0 = time.perf_counter()
        c.call_tool("list_roots")
        assert time.perf_counter() - t0 < 1.0
        t.join()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(project):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(ROOT / "filesystem_mcp_server.py"), "--config",
                             "config/filesystem_server.json", "--transport", "http", "--port", str(port),
                             "--log-level", "WARNING"], cwd=project, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    yield base
    proc.terminate()
    proc.wait(5)


def _post(url, body: bytes, ctype="application/json"):
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_transport_and_status_codes(http_server):
    health = json.loads(urllib.request.urlopen(http_server + "/health").read())
    assert health["status"] == "ok"
    disc = json.loads(urllib.request.urlopen(http_server + "/.well-known/mcp.json").read())
    assert "batch_process" in {t["name"] for t in disc["tools"]}
    url = http_server + "/mcp"
    assert _post(url, b'{"jsonrpc":"2.0","id":1,"method":"ping"}')[0] == 200
    assert _post(url, b'{"jsonrpc":"2.0","method":"notifications/initialized"}')[0] == 202
    status, body = _post(url, b"{oops")
    assert status == 400 and json.loads(body)["error"]["code"] == -32700
    assert _post(url, b"{}", "text/plain")[0] == 415
    with HttpMCPClient("fs-http", url) as c:
        assert c.call_tool("list_files", {"directory": "jobs"})["count"] == 1


def test_official_mcp_sdk_client_interop(project):
    mcp = pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    def field(obj, snake, camel):
        return getattr(obj, snake, None) if hasattr(obj, snake) else getattr(obj, camel)

    async def run():
        params = StdioServerParameters(command=sys.executable, cwd=str(project),
                                       args=[str(ROOT / "filesystem_mcp_server.py"), "--config",
                                             "config/filesystem_server.json", "--log-level", "WARNING"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("list_files", {"directory": "resumes", "pattern": "*.pdf"})
                resources = await session.list_resources()
                content = await session.read_resource("fs://resumes/sarah_chen.txt")
                return tools, result, resources, content

    tools, result, resources, content = asyncio.run(asyncio.wait_for(run(), 30))
    assert "batch_process" in {t.name for t in tools.tools}
    assert field(result, "is_error", "isError") is False
    assert field(result, "structured_content", "structuredContent")["count"] == 3
    assert any(str(r.uri) == "fs://resumes/priya_raman.pdf" for r in resources.resources)
    assert content.contents[0].text.startswith("Sarah Chen")
