"""Scenario group 1 - JSON-RPC 2.0 compliance and the MCP lifecycle."""
import json

from conftest import rpc
from filesystem_mcp_server import SUPPORTED_PROTOCOL_VERSIONS, ErrorCode, FileSystemMCPServer, ServerConfig


def test_parse_error_returns_minus_32700_with_null_id(server):
    resp = json.loads(server.handle_payload("{not json"))
    assert resp["error"]["code"] == ErrorCode.PARSE_ERROR and resp["id"] is None


def test_invalid_requests(server):
    assert server.handle_message([1])["error"]["code"] == ErrorCode.INVALID_REQUEST           # not an object
    assert server.handle_message({"id": 1, "method": "ping"})["error"]["code"] == ErrorCode.INVALID_REQUEST  # no jsonrpc
    assert server.handle_message({"jsonrpc": "2.0", "id": 1.5, "method": "ping"})["error"]["code"] \
        == ErrorCode.INVALID_REQUEST                                                           # bad id type
    assert server.handle_message({"jsonrpc": "2.0", "id": 1})["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]})["error"]["code"] \
        == ErrorCode.INVALID_PARAMS                                                            # positional params


def test_method_not_found(server):
    resp = rpc(server, "files/teleport")
    assert resp["error"]["code"] == ErrorCode.METHOD_NOT_FOUND
    assert "tools/call" in resp["error"]["data"]["available"]


def test_response_echoes_id_and_version(server):
    resp = server.handle_message({"jsonrpc": "2.0", "id": "abc-123", "method": "ping"})
    assert resp == {"jsonrpc": "2.0", "id": "abc-123", "result": {}}


def test_notifications_get_no_response(server):
    assert server.handle_payload('{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
    assert server.handle_payload('{"jsonrpc":"2.0","method":"unknown/notification"}') is None


def test_batch_mixes_results_and_errors_and_skips_notifications(server):
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "nope"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_files", "arguments": {"directory": "resumes"}}},
    ]
    out = json.loads(server.handle_payload(json.dumps(batch)))
    assert [r["id"] for r in out] == [1, 2, 3]
    assert "result" in out[0] and out[1]["error"]["code"] == ErrorCode.METHOD_NOT_FOUND
    assert json.loads(server.handle_payload("[]"))["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_requests_before_initialize_are_rejected(project):
    srv = FileSystemMCPServer(ServerConfig.load(str(project / "config" / "filesystem_server.json")))
    assert rpc(srv, "tools/list")["error"]["code"] == ErrorCode.NOT_INITIALIZED
    assert rpc(srv, "ping")["result"] == {}  # ping is always allowed


def test_protocol_version_negotiation(project):
    srv = FileSystemMCPServer(ServerConfig.load(str(project / "config" / "filesystem_server.json")))
    ok = rpc(srv, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {}})
    assert ok["result"]["protocolVersion"] == "2025-03-26"
    unknown = rpc(srv, "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {}})
    assert unknown["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]
    caps = ok["result"]["capabilities"]
    assert caps["resources"]["subscribe"] and caps["resources"]["listChanged"] and "tools" in caps
    assert rpc(srv, "initialize", {})["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_tool_argument_validation_is_a_protocol_error(server):
    resp = rpc(server, "tools/call", {"name": "list_files", "arguments": {"directory": 5, "bogus": True}})
    assert resp["error"]["code"] == ErrorCode.INVALID_PARAMS
    problems = " ".join(resp["error"]["data"]["problems"])
    assert "expected string" in problems and "unknown property 'bogus'" in problems
    assert rpc(server, "tools/call", {"name": "rm_rf"})["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_pagination_with_cursor(server):
    server.page_size = 4
    first = rpc(server, "tools/list")["result"]
    assert len(first["tools"]) == 4 and "nextCursor" in first
    names = [t["name"] for t in first["tools"]]
    cursor = first["nextCursor"]
    while cursor:
        page = rpc(server, "tools/list", {"cursor": cursor})["result"]
        names += [t["name"] for t in page["tools"]]
        cursor = page.get("nextCursor")
    assert {"watch_directory", "batch_process"} <= set(names) and len(names) == len(set(names))
    assert rpc(server, "tools/list", {"cursor": "%%%"})["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_discovery_endpoint_lists_everything(server):
    d = rpc(server, "server/discover")["result"]
    assert d["serverInfo"]["name"] == "resume-filesystem-mcp"
    assert {t["name"] for t in d["tools"]} >= {"list_files", "extract_text", "watch_directory", "batch_process"}
    assert any(t["uriTemplate"] == "fs://{root}/{path}" for t in d["resourceTemplates"])
    assert d["errorCodes"]["ACCESS_DENIED"] == ErrorCode.ACCESS_DENIED


def test_logging_set_level(server):
    assert rpc(server, "logging/setLevel", {"level": "error"})["result"] == {}
    assert rpc(server, "logging/setLevel", {"level": "loud"})["error"]["code"] == ErrorCode.INVALID_PARAMS
