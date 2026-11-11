"""Scenario group 5 - the LangGraph agent driving two MCP servers end to end."""
import ast
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from conftest import ROOT
from database_mcp_server import CandidateDBServer
from matching_agent import ResumeMatchingAgent, parse_job, parse_resume, score_candidate
from mcp_client import MultiMCPClient

JOB_PATH = "jobs/senior_python_ai_engineer.md"


# ------------------------------------------------------------------ pure logic
def test_job_parsing():
    job = parse_job((ROOT / "data/jobs/senior_python_ai_engineer.md").read_text(encoding="utf-8"), JOB_PATH)
    assert job["title"] == "Senior Python AI Engineer" and job["min_years"] == 5
    labels = [r["label"] for r in job["required"]]
    assert labels[:2] == ["Python (5+ years)", "LangChain / LangGraph"]
    assert "Git" in labels and "CI/CD" in labels           # "Git and CI/CD" -> two requirements
    vector = next(r for r in job["nice_to_have"] if r["label"].startswith("Vector"))
    assert {"faiss", "pinecone", "chroma"} <= set(vector["any_of"])  # parenthesised list = alternatives


def test_scoring_orders_strong_above_weak():
    job = parse_job((ROOT / "data/jobs/senior_python_ai_engineer.md").read_text(encoding="utf-8"), JOB_PATH)
    strong = parse_resume((ROOT / "data/incoming_samples/kavya_nair.txt").read_text(encoding="utf-8"), "resumes/kavya.txt")
    weak = parse_resume((ROOT / "data/resumes/meera_iyer.txt").read_text(encoding="utf-8"), "resumes/meera.txt")
    s, w = score_candidate(strong, job), score_candidate(weak, job)
    assert s["decision"] == "shortlist" and w["decision"] == "reject" and s["score"] > w["score"] + 40
    assert sum(s["breakdown"].values()) == pytest.approx(s["score"], abs=0.2)
    assert strong["email"] == "kavya.nair@example.com" and strong["years_experience"] == 5


# --------------------------------------------------------------- architecture
def test_agent_has_no_direct_filesystem_access():
    """Part B requirement: the agent must not touch files itself."""
    tree = ast.parse((ROOT / "matching_agent.py").read_text(encoding="utf-8"))
    banned_calls = {"open", "listdir", "scandir", "walk", "glob", "read_text", "write_text", "read_bytes",
                    "write_bytes", "iterdir", "rglob", "PdfReader", "Document"}
    banned_imports = {"os", "pathlib", "glob", "shutil", "pypdf", "docx"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name not in banned_calls, f"direct file access via {name}()"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            assert not set(m.split(".")[0] for m in mods) & banned_imports, mods


# ----------------------------------------------------------------- end to end
def run_agent(project: Path, **kw):
    only = kw.pop("only", None)
    with MultiMCPClient.from_config_file(project / "config/mcp_servers.json", only=only) as mcp:
        agent = ResumeMatchingAgent(mcp, verbose=False)
        state = agent.run(JOB_PATH, **kw)
        return agent, state


def test_full_run_with_two_mcp_servers(project):
    agent, state = run_agent(project)
    ranked = sorted(state["results"].values(), key=lambda r: -r["score"])
    assert [r["name"] for r in ranked][:1] == ["Priya Raman"] and ranked[-1]["name"] == "Meera Iyer"
    assert len(ranked) == 6 and state["errors"][0]["type"] == "EXTRACTION_FAILED"
    # Both servers were used
    servers = {c["tool"].split(".")[0] for c in agent.calls}
    assert servers == {"filesystem", "candidate_db"}
    used = {c["tool"] for c in agent.calls}
    assert {"filesystem.batch_process", "filesystem.write_file", "candidate_db.candidate_history",
            "candidate_db.record_matches"} <= used
    # The report was written by the filesystem server into output/
    report = json.loads((project / "output/match_report_senior_python_ai_engineer.json").read_text(encoding="utf-8"))
    assert report["summary"]["screened"] == 6 and report["summary"]["shortlist"] >= 1
    assert (project / "output/match_report_senior_python_ai_engineer.md").read_text(encoding="utf-8").startswith("# Match report")
    # The node sequence matches the state machine
    nodes = [t["node"] for t in state["trace"]]
    assert nodes == ["discover_capabilities", "load_job", "discover_resumes", "extract_resumes", "parse_candidates",
                     "lookup_history", "score_candidates", "persist_results", "write_report"]


def test_second_run_recognises_returning_candidates(project):
    run_agent(project)
    _, state = run_agent(project)
    priya = next(r for r in state["results"].values() if r["name"] == "Priya Raman")
    assert priya["history"]["known"] and len(priya["history"]["previous_matches"]) == 1
    db = CandidateDBServer(str(project / "data/candidates.db")).db
    assert db.stats() == {"candidates": 6, "runs": 2, "matches": 12, "database": str(project / "data/candidates.db")}
    top = db.top_matches("Senior Python AI Engineer", 1)["matches"][0]
    assert top["name"] == "Priya Raman" and top["times_screened"] == 2


def test_filesystem_only_mode(project):
    agent, state = run_agent(project, only=["filesystem"])
    assert state["db_enabled"] is False and len(state["results"]) == 6
    assert "persist_results" not in [t["node"] for t in state["trace"]]
    assert not (project / "data/candidates.db").exists()


def test_watch_mode_screens_new_arrival(project):
    def drop():
        time.sleep(1.5)
        shutil.copy(project / "data/incoming_samples/kavya_nair.txt", project / "data/resumes/kavya_nair.txt")

    threading.Thread(target=drop, daemon=True).start()
    agent, state = run_agent(project, watch=True, watch_seconds=6, poll_timeout_s=2)
    assert "resumes/kavya_nair.txt" in state["results"]
    assert state["results"]["resumes/kavya_nair.txt"]["decision"] == "shortlist"
    nodes = [t["node"] for t in state["trace"]]
    assert nodes.count("extract_resumes") == 2 and nodes.count("wait_for_resumes") >= 2
    assert any(c["tool"] == "filesystem.watch_directory" for c in agent.calls)


def test_langchain_tool_adapters(project):
    pytest.importorskip("langchain_core")
    with MultiMCPClient.from_config_file(project / "config/mcp_servers.json") as mcp:
        tools = {t.name: t for t in mcp.to_langchain_tools()}
        assert {"filesystem__extract_text", "candidate_db__run_sql"} <= set(tools)
        out = json.loads(tools["filesystem__list_files"].invoke({"directory": "jobs"}))
        assert out["count"] == 1
        assert "error" in json.loads(tools["filesystem__read_file"].invoke({"path": "nope/x.txt"}))


# -------------------------------------------------------------- DB server
def test_database_server_read_only_sql(project):
    from conftest import rpc
    srv = CandidateDBServer(str(project / "data/test.db"))
    srv.strict_lifecycle = False
    r = rpc(srv, "tools/call", {"name": "run_sql", "arguments": {"query": "SELECT name FROM sqlite_master"}})
    assert r["result"]["isError"] is False
    for bad in ("DELETE FROM candidates", "SELECT 1; DROP TABLE candidates",
                "WITH x AS (SELECT 1) INSERT INTO candidates(email) VALUES('a')"):
        r = rpc(srv, "tools/call", {"name": "run_sql", "arguments": {"query": bad}})
        assert r["result"]["isError"] is True
    r = rpc(srv, "tools/call", {"name": "record_matches", "arguments": {"run_id": "nope", "matches": []}})
    assert r["result"]["structuredContent"]["error"]["type"] == "RESOURCE_NOT_FOUND"
