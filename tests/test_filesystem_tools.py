"""Scenario group 2 - the Milestone 1 tools behind MCP, the sandbox and error results."""
import hashlib
import os

import pytest

from conftest import call, rpc
from filesystem_mcp_server import ErrorCode


def err(result):
    assert result["isError"] is True, result
    return result["structuredContent"]["error"]


def test_list_roots_and_files(server):
    roots = call(server, "list_roots")["structuredContent"]["roots"]
    assert {r["name"]: r["writable"] for r in roots} == {"resumes": False, "jobs": False, "output": True}
    listing = call(server, "list_files", directory="resumes")["structuredContent"]
    names = {f["name"] for f in listing["files"]}
    assert {"priya_raman.pdf", "arjun_mehta.docx", "sarah_chen.txt", "rahul_verma.md"} <= names
    pdfs = call(server, "list_files", directory="resumes", pattern="*.pdf")["structuredContent"]
    assert all(f["extension"] == ".pdf" for f in pdfs["files"]) and pdfs["count"] == 3


@pytest.mark.parametrize("path,needle", [
    ("resumes/priya_raman.pdf", "LangGraph"),
    ("resumes/arjun_mehta.docx", "Flask"),
    ("resumes/sarah_chen.txt", "Chroma"),
    ("resumes/rahul_verma.md", "Spring Boot"),
])
def test_extract_text_all_formats(server, path, needle):
    r = call(server, "extract_text", path=path)
    assert r["isError"] is False
    assert needle in r["structuredContent"]["text"]
    # Content is also delivered as a text block for clients that ignore structuredContent.
    assert r["content"][0]["type"] == "text"


def test_extract_text_truncation(server):
    r = call(server, "extract_text", path="resumes/priya_raman.pdf", max_chars=50)["structuredContent"]
    assert r["truncated"] and len(r["text"]) == 50 and r["chars"] > 50


def test_metadata_hash_matches_file(server, project):
    meta = call(server, "get_file_metadata", path="resumes/sarah_chen.txt")["structuredContent"]
    expected = hashlib.sha256((project / "data/resumes/sarah_chen.txt").read_bytes()).hexdigest()
    assert meta["sha256"] == expected and meta["mime_type"] == "text/plain"


def test_read_file_rejects_binary_formats_with_hint(server):
    e = err(call(server, "read_file", path="resumes/priya_raman.pdf"))
    assert e["type"] == "UNSUPPORTED_FILE_TYPE" and e["details"]["hint"] == "extract_text"


def test_search_files_across_pdf_and_docx(server):
    r = call(server, "search_files", query="kubernetes")["structuredContent"]
    paths = {m["path"] for m in r["matches"]}
    assert {"resumes/priya_raman.pdf", "resumes/david_okafor.pdf"} <= paths
    assert any(e["path"].endswith("corrupted_upload.pdf") for e in r["errors"])


def test_corrupted_pdf_is_a_tool_error_not_a_crash(server):
    e = err(call(server, "extract_text", path="resumes/corrupted_upload.pdf"))
    assert e["code"] == ErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/passwd", "resumes/../../config/x.json", "secrets/a.txt"])
def test_sandbox_blocks_escapes(server, path):
    assert err(call(server, "extract_text", path=path))["code"] == ErrorCode.ACCESS_DENIED


def test_symlink_escape_blocked(server, project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("top secret")
    try:
        os.symlink(outside, project / "data/resumes/link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
    assert err(call(server, "read_file", path="resumes/link.txt"))["code"] == ErrorCode.ACCESS_DENIED


def test_missing_file(server):
    assert err(call(server, "extract_text", path="resumes/nobody.pdf"))["code"] == ErrorCode.RESOURCE_NOT_FOUND


def test_write_file_rules(server, project):
    assert err(call(server, "write_file", path="resumes/x.txt", content="hi"))["code"] == ErrorCode.READ_ONLY
    ok = call(server, "write_file", path="output/reports/r.json", content='{"a":1}')["structuredContent"]
    assert ok["bytes_written"] == 7 and (project / "output/reports/r.json").read_text(encoding="utf-8") == '{"a":1}'
    assert err(call(server, "write_file", path="output/reports/r.json", content="x"))["code"] == ErrorCode.ALREADY_EXISTS
    call(server, "write_file", path="output/reports/r.json", content="x", overwrite=True)
    assert (project / "output/reports/r.json").read_text(encoding="utf-8") == "x"
    assert err(call(server, "write_file", path="output/run.exe", content="x"))["code"] == ErrorCode.UNSUPPORTED_FILE_TYPE
    assert any(m.get("method") == "notifications/resources/list_changed" for m in server.sent)


def test_configuration_management(server):
    cfg = call(server, "get_server_config")["structuredContent"]
    assert cfg["batch_max_workers"] == 4 and "roots" in cfg
    r = call(server, "configure_server", settings={"max_file_size_mb": 0.0001, "roots": {}})["structuredContent"]
    assert "max_file_size_mb" in r["changed"] and r["rejected"]["roots"] == "not runtime-configurable"
    # The new limit is enforced immediately.
    assert err(call(server, "extract_text", path="resumes/priya_raman.pdf"))["code"] == ErrorCode.FILE_TOO_LARGE
    assert err(call(server, "configure_server", settings={"batch_max_workers": 999}))["code"] == ErrorCode.INVALID_PARAMS
    # Changing config notifies subscribers of config://server
    assert any(m.get("params", {}).get("uri") == "config://server" for m in server.sent)


def test_tool_annotations_present(server):
    tools = {t["name"]: t for t in rpc(server, "tools/list")["result"]["tools"]}
    assert tools["extract_text"]["annotations"]["readOnlyHint"] is True
    assert tools["write_file"]["annotations"]["destructiveHint"] is True
    assert tools["batch_process"]["inputSchema"]["properties"]["operation"]["enum"] == \
        ["extract_text", "metadata", "read", "hash"]
