"""Scenario group 3 - MCP resources, watch_directory() and batch_process()."""
import json
import shutil
import time

from conftest import call, rpc
from filesystem_mcp_server import ErrorCode


# --------------------------------------------------------------------- resources
def test_resource_discovery(server):
    res = rpc(server, "resources/list")["result"]["resources"]
    uris = {r["uri"] for r in res}
    assert {"config://server", "tools://catalog", "watch://active"} <= uris
    assert "fs://resumes/priya_raman.pdf" in uris and "fs://jobs/senior_python_ai_engineer.md" in uris
    pdf = next(r for r in res if r["uri"] == "fs://resumes/priya_raman.pdf")
    assert pdf["mimeType"] == "application/pdf" and pdf["size"] > 0
    templates = {t["uriTemplate"] for t in rpc(server, "resources/templates/list")["result"]["resourceTemplates"]}
    assert templates == {"fs://{root}/{path}", "fs-meta://{root}/{path}", "tools://{name}", "watch://{watch_id}/events"}


def test_read_file_resources(server):
    pdf = rpc(server, "resources/read", {"uri": "fs://resumes/priya_raman.pdf"})["result"]["contents"][0]
    assert pdf["mimeType"] == "text/plain" and "Priya Raman" in pdf["text"]
    md = rpc(server, "resources/read", {"uri": "fs://jobs/senior_python_ai_engineer.md"})["result"]["contents"][0]
    assert md["mimeType"] == "text/markdown"
    meta = json.loads(rpc(server, "resources/read", {"uri": "fs-meta://resumes/sarah_chen.txt"})
                      ["result"]["contents"][0]["text"])
    assert len(meta["sha256"]) == 64


def test_tools_exposed_as_resources(server):
    catalog = json.loads(rpc(server, "resources/read", {"uri": "tools://catalog"})["result"]["contents"][0]["text"])
    assert {t["name"] for t in catalog["tools"]} >= {"list_files", "read_file", "extract_text", "search_files"}
    one = json.loads(rpc(server, "resources/read", {"uri": "tools://batch_process"})["result"]["contents"][0]["text"])
    assert one["name"] == "batch_process" and "inputSchema" in one


def test_resource_errors(server):
    for uri in ("fs://resumes/ghost.pdf", "fs://../etc/passwd", "tools://nope", "nosuch://x"):
        assert rpc(server, "resources/read", {"uri": uri})["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND
    assert rpc(server, "resources/read", {})["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_subscribe_and_updated_notification(server, project):
    assert rpc(server, "resources/subscribe", {"uri": "fs://resumes/sarah_chen.txt"})["result"] == {}
    wid = call(server, "watch_directory", action="start", directory="resumes")["structuredContent"]["watch_id"]
    time.sleep(0.05)
    with open(project / "data/resumes/sarah_chen.txt", "a") as fh:
        fh.write("\nCERTIFICATIONS\nAWS Certified ML Specialty\n")
    events = call(server, "watch_directory", action="poll", watch_id=wid, timeout_s=3)["structuredContent"]["events"]
    assert any(e["type"] == "modified" for e in events)
    assert {"jsonrpc": "2.0", "method": "notifications/resources/updated",
            "params": {"uri": "fs://resumes/sarah_chen.txt"}} in server.sent


# ---------------------------------------------------------------- watch_directory
def test_watch_detects_new_resume_with_preview(server, project):
    start = call(server, "watch_directory", action="start", directory="resumes", patterns="*.txt,*.pdf")
    wid = start["structuredContent"]["watch_id"]
    assert start["structuredContent"]["tracked_files"] == 5  # existing files are baseline, not events
    shutil.copy(project / "data/incoming_samples/kavya_nair.txt", project / "data/resumes/kavya_nair.txt")
    res = call(server, "watch_directory", action="poll", watch_id=wid, timeout_s=5)["structuredContent"]
    created = [e for e in res["events"] if e["type"] == "created"]
    assert created and created[0]["path"] == "resumes/kavya_nair.txt"
    assert created[0]["extraction"]["status"] == "ok" and "Kavya Nair" in created[0]["extraction"]["preview"]
    # Server pushed MCP notifications as well
    methods = [m.get("method") for m in server.sent]
    assert "notifications/resources/list_changed" in methods and "notifications/message" in methods
    # And the new file is now a discoverable resource
    uris = {r["uri"] for r in rpc(server, "resources/list")["result"]["resources"]}
    assert "fs://resumes/kavya_nair.txt" in uris


def test_watch_ignores_other_patterns_and_reports_deletes(server, project):
    wid = call(server, "watch_directory", action="start", directory="resumes", patterns="*.pdf",
               auto_extract=False)["structuredContent"]["watch_id"]
    (project / "data/resumes/notes.txt").write_text("ignore me")
    (project / "data/resumes/meera_iyer.txt").unlink()
    (project / "data/resumes/david_okafor.pdf").unlink()
    events = call(server, "watch_directory", action="poll", watch_id=wid, timeout_s=2)["structuredContent"]["events"]
    assert [(e["type"], e["path"]) for e in events] == [("deleted", "resumes/david_okafor.pdf")]


def test_watch_lifecycle_and_errors(server):
    wid = call(server, "watch_directory", action="start", directory="resumes")["structuredContent"]["watch_id"]
    listed = call(server, "watch_directory", action="list")["structuredContent"]["watches"]
    assert [w["watch_id"] for w in listed] == [wid]
    active = json.loads(rpc(server, "resources/read", {"uri": "watch://active"})["result"]["contents"][0]["text"])
    assert active["watches"][0]["watch_id"] == wid
    empty = call(server, "watch_directory", action="poll", watch_id=wid, timeout_s=0)["structuredContent"]
    assert empty["count"] == 0
    assert call(server, "watch_directory", action="stop", watch_id=wid)["structuredContent"]["status"] == "stopped"
    gone = call(server, "watch_directory", action="poll", watch_id=wid)
    assert gone["isError"] and gone["structuredContent"]["error"]["type"] == "WATCH_NOT_FOUND"
    assert call(server, "watch_directory", action="poll")["isError"]  # missing watch_id
    assert call(server, "watch_directory", action="start", directory="jobs/none")["isError"]


def test_watch_limit(server):
    server.config.watch_max_active = 2
    for _ in range(2):
        call(server, "watch_directory", action="start", directory="resumes")
    third = call(server, "watch_directory", action="start", directory="resumes")
    assert third["structuredContent"]["error"]["type"] == "LIMIT_EXCEEDED"


# ----------------------------------------------------------------- batch_process
def test_batch_extract_directory_with_partial_failure(server):
    r = call(server, "batch_process", operation="extract_text", directory="resumes", max_chars=100)
    assert r["isError"] is False  # one bad file does not fail the batch
    b = r["structuredContent"]
    assert b["total"] == 7 and b["succeeded"] == 6 and b["failed"] == 1
    bad = next(x for x in b["results"] if x["status"] == "error")
    assert bad["path"] == "resumes/corrupted_upload.pdf" and bad["error"]["type"] == "EXTRACTION_FAILED"
    assert all(len(x["data"]["text"]) <= 100 for x in b["results"] if x["status"] == "ok")


def test_batch_paths_dedupe_order_and_operations(server):
    paths = ["resumes/sarah_chen.txt", "resumes/priya_raman.pdf", "resumes/sarah_chen.txt", "resumes/ghost.txt"]
    b = call(server, "batch_process", operation="hash", paths=paths)["structuredContent"]
    assert [x["path"] for x in b["results"]] == paths[:2] + paths[3:]
    assert b["results"][2]["error"]["type"] == "RESOURCE_NOT_FOUND"
    meta = call(server, "batch_process", operation="metadata", directory="jobs")["structuredContent"]
    assert meta["succeeded"] == 1


def test_batch_stop_on_first_error(server):
    paths = ["resumes/ghost.txt"] + [f"resumes/{n}" for n in ("sarah_chen.txt", "meera_iyer.txt")]
    b = call(server, "batch_process", operation="extract_text", paths=paths, max_workers=1,
             continue_on_error=False)["structuredContent"]
    assert b["stopped_early"] and b["failed"] == 1 and b["succeeded"] + b["skipped"] == 2


def test_batch_progress_notifications(server):
    rpc(server, "tools/call", {"name": "batch_process", "arguments": {"operation": "metadata", "directory": "resumes"},
                               "_meta": {"progressToken": "tok-1"}})
    progress = [m["params"] for m in server.sent if m.get("method") == "notifications/progress"]
    assert progress[0]["progress"] == 0 and progress[-1]["progress"] == progress[-1]["total"] == 7
    assert all(p["progressToken"] == "tok-1" for p in progress)


def test_batch_limits(server):
    assert call(server, "batch_process", operation="read")["isError"]  # neither paths nor directory
    server.config.batch_max_files = 2
    r = call(server, "batch_process", operation="metadata", directory="resumes")
    assert r["structuredContent"]["error"]["type"] == "LIMIT_EXCEEDED"
