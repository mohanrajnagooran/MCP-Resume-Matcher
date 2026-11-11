#!/usr/bin/env python3
"""
matching_agent.py  (refactored for MCP)
=======================================

LangGraph resume-matching agent. It has **no direct file-system access**: every
file it reads or writes goes through the filesystem MCP server, and candidate
history / persistence goes through a second MCP server (the candidate database).

Workflow (LangGraph state machine)::

    discover_capabilities -> load_job -> discover_resumes
        -> extract_resumes   (filesystem.batch_process)
        -> parse_candidates
        -> lookup_history    (candidate_db.candidate_history)   [if DB connected]
        -> score_candidates
        -> persist_results   (candidate_db.upsert/record)       [if DB connected]
        -> write_report      (filesystem.write_file)
        -> wait_for_resumes  (filesystem.watch_directory)       [--watch only]
               └── new resumes? -> extract_resumes (loop)  /  none -> END

Run::

    python matching_agent.py                                   # batch run
    python matching_agent.py --watch --watch-seconds 60        # keep screening new arrivals
    python matching_agent.py --no-db                           # filesystem server only
    python matching_agent.py --mermaid > docs/agent_graph.mmd  # export the graph
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import operator
import re
import sys
import time
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from mcp_client import MCPClientError, MCPToolError, MultiMCPClient

log = logging.getLogger("matching-agent")

FS = "filesystem"
DB = "candidate_db"
REQUIRED_FS_TOOLS = {"list_files", "extract_text", "batch_process", "write_file", "watch_directory"}

# =============================================================================
# Domain logic: job & resume parsing, scoring (pure functions, no I/O)
# =============================================================================
SKILL_VOCAB: dict[str, list[str]] = {
    "python": ["python"], "langchain": ["langchain"], "langgraph": ["langgraph"],
    "llm": ["llms?", "large language models?"], "rag": ["rag", r"retrieval[- ]augmented"],
    "fastapi": ["fastapi"], "flask": ["flask"], "django": ["django"], "rest api": ["rest apis?", "restful"],
    "docker": ["docker", "dockeri[sz]ed"], "kubernetes": ["kubernetes", "k8s", "eks", "gke", "aks"],
    "sql": ["sql"], "postgresql": ["postgresql", "postgres", "pgvector"], "mysql": ["mysql"],
    "git": ["git", "github", "gitlab"], "ci/cd": ["ci/cd", "ci-cd", "github actions", "jenkins", "gitlab ci"],
    "mcp": ["mcp", "model context protocol"], "aws": ["aws", "amazon web services"],
    "gcp": ["gcp", "google cloud", "bigquery"], "azure": ["azure"],
    "vector database": ["vector databases?", "faiss", "pinecone", "chroma", "weaviate", "milvus", "pgvector"],
    "faiss": ["faiss"], "pinecone": ["pinecone"], "chroma": ["chroma"],
    "prompt engineering": ["prompt engineering"], "machine learning": ["machine learning", "scikit-learn"],
    "pytorch": ["pytorch"], "tensorflow": ["tensorflow"], "nlp": ["nlp", "natural language processing"],
    "pandas": ["pandas"], "java": ["java"], "spring boot": ["spring boot"], "javascript": ["javascript"],
    "typescript": ["typescript"], "react": ["react"], "terraform": ["terraform"], "linux": ["linux"],
}
_SKILL_RX = {k: re.compile(r"(?<![a-z0-9])(?:" + "|".join(v) + r")(?![a-z0-9])", re.I) for k, v in SKILL_VOCAB.items()}
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RX = re.compile(r"\+?\d[\d\s().-]{8,}\d")
YEARS_RX = re.compile(r"(\d{1,2})\+?\s*(?:years?|yrs?)\b", re.I)
RANGE_RX = re.compile(r"\b(19[89]\d|20[0-4]\d)\s*[-–]\s*(present|current|now|19[89]\d|20[0-4]\d)\b", re.I)
DEGREE_RX = re.compile(r"\b(b\.?\s?tech|b\.?\s?e\.|b\.?\s?eng|b\.?\s?sc|bachelor|m\.?\s?s\.|m\.?\s?tech|master|ph\.?\s?d)",
                       re.I)
CS_FIELD_RX = re.compile(r"computer|information technology|data science|software|computing", re.I)


def skills_in(text: str) -> set[str]:
    return {k for k, rx in _SKILL_RX.items() if rx.search(text)}


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def parse_requirements(lines: list[str]) -> list[dict]:
    """Bullets -> requirements. Commas and 'and' separate requirements; '/',
    'or' and parenthesised lists are alternatives (any one satisfies it)."""
    reqs = []
    for line in lines:
        for chunk in _split_top_level(line):
            for part in re.split(r"\s+and\s+", chunk):
                part = part.strip(" .")
                if not part:
                    continue
                hits = sorted(skills_in(part))
                reqs.append({"label": part, "any_of": hits, "literal": None if hits else part.lower()})
    return reqs


def parse_job(text: str, path: str) -> dict:
    lines = text.splitlines()
    title = next((ln.lstrip("# ").strip() for ln in lines if ln.strip()), path)
    sections: dict[str, list[str]] = {}
    current = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("#") or (s.endswith(":") and len(s) < 40):
            current = s.strip("#: ").lower()
            sections.setdefault(current, [])
        elif current and re.match(r"^[-*•]\s+", s):
            sections[current].append(re.sub(r"^[-*•]\s+", "", s))
    req_lines = next((v for k, v in sections.items() if "required" in k or "must" in k or "requirement" in k), [])
    nice_lines = next((v for k, v in sections.items()
                       if any(w in k for w in ("nice", "preferred", "bonus", "good to have"))), [])
    years = [int(m.group(1)) for m in YEARS_RX.finditer(text)]
    return {
        "title": title, "path": path,
        "min_years": min(years) if years else 0,
        "required": parse_requirements(req_lines),
        "nice_to_have": parse_requirements(nice_lines),
        "education_required": bool(re.search(r"bachelor|degree", text, re.I)),
    }


def parse_resume(text: str, path: str) -> dict:
    lines = [ln.strip().lstrip("# ").strip() for ln in text.splitlines() if ln.strip()]
    name = lines[0] if lines else path.rsplit("/", 1)[-1]
    email_m = EMAIL_RX.search(text)
    phone_m = PHONE_RX.search(text)
    explicit = [int(m.group(1)) for m in YEARS_RX.finditer(text)]
    if explicit:
        years = float(max(explicit))
    else:
        now = datetime.now().year
        starts = [int(m.group(1)) for m in RANGE_RX.finditer(text)]
        years = float(now - min(starts)) if starts else 0.0
    degree = DEGREE_RX.search(text)
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return {
        "path": path, "name": name,
        "email": (email_m.group(0).lower() if email_m else f"unknown+{stem}@local"),
        "phone": phone_m.group(0).strip() if phone_m else None,
        "years_experience": years,
        "skills": sorted(skills_in(text)),
        "education": {"degree": degree.group(0) if degree else None,
                      "cs_related": bool(degree and CS_FIELD_RX.search(text))},
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "_text_lower": text.lower(),
    }


def _met(req: dict, cand: dict) -> bool:
    if req["any_of"]:
        return bool(set(req["any_of"]) & set(cand["skills"]))
    return req["literal"] in cand["_text_lower"]


WEIGHTS = {"required": 55, "nice_to_have": 20, "experience": 20, "education": 5}
THRESHOLDS = {"shortlist": 75, "review": 55}


def score_candidate(cand: dict, job: dict) -> dict:
    req_met = [r["label"] for r in job["required"] if _met(r, cand)]
    req_missing = [r["label"] for r in job["required"] if not _met(r, cand)]
    nice_met = [r["label"] for r in job["nice_to_have"] if _met(r, cand)]
    req_cov = len(req_met) / len(job["required"]) if job["required"] else 1.0
    nice_cov = len(nice_met) / len(job["nice_to_have"]) if job["nice_to_have"] else 1.0
    exp = min(cand["years_experience"] / job["min_years"], 1.0) if job["min_years"] else 1.0
    edu = 1.0 if cand["education"]["cs_related"] else 0.6 if cand["education"]["degree"] else 0.0
    if not job["education_required"]:
        edu = 1.0
    breakdown = {
        "required": round(WEIGHTS["required"] * req_cov, 1),
        "nice_to_have": round(WEIGHTS["nice_to_have"] * nice_cov, 1),
        "experience": round(WEIGHTS["experience"] * exp, 1),
        "education": round(WEIGHTS["education"] * edu, 1),
    }
    score = round(sum(breakdown.values()), 1)
    if req_cov < 0.5:
        decision = "reject"
    elif score >= THRESHOLDS["shortlist"]:
        decision = "shortlist"
    elif score >= THRESHOLDS["review"]:
        decision = "review"
    else:
        decision = "reject"
    rationale = (f"Meets {len(req_met)}/{len(job['required'])} required and {len(nice_met)}/"
                 f"{len(job['nice_to_have'])} nice-to-have skills; {cand['years_experience']:g} yrs experience "
                 f"(needs {job['min_years']}+); degree: {cand['education']['degree'] or 'not found'}.")
    return {"score": score, "decision": decision, "breakdown": breakdown, "matched_required": req_met,
            "missing_required": req_missing, "matched_nice_to_have": nice_met, "rationale": rationale}


# =============================================================================
# LangGraph state
# =============================================================================
class AgentState(TypedDict, total=False):
    job_path: str
    resume_dir: str
    pattern: str
    watch: bool
    watch_deadline: float
    poll_timeout_s: float
    max_rounds: int
    # filled in by nodes
    inventory: dict
    db_enabled: bool
    job: dict
    run_id: str | None
    watch_id: str | None
    round: int
    to_process: list[str]
    extracted: dict            # path -> text (current round)
    candidates: list[dict]     # parsed in current round
    results: dict              # path -> full scored result (all rounds)
    errors: list[dict]
    report_paths: list[str]
    trace: Annotated[list[dict], operator.add]


def _t(node: str, **info) -> list[dict]:
    return [{"node": node, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **info}]


class ResumeMatchingAgent:
    """Builds and runs the LangGraph workflow on top of connected MCP clients."""

    def __init__(self, mcp: MultiMCPClient, verbose: bool = True):
        self.mcp = mcp
        self.verbose = verbose
        self.calls: list[dict] = []
        self.graph = self._build_graph()

    # ------------------------------------------------------------ helpers
    def say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def call(self, qualified: str, args: dict | None = None, **kw) -> Any:
        """Every MCP tool call goes through here so it is timed and logged."""
        t0 = time.perf_counter()
        status = "ok"
        try:
            return self.mcp.call(qualified, args or {}, **kw)
        except (MCPToolError, MCPClientError) as exc:
            status = f"error: {exc}"
            raise
        finally:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            self.calls.append({"tool": qualified, "ms": ms, "status": status})
            self.say(f"      ↳ MCP {qualified}  ({ms} ms){'' if status == 'ok' else '  ' + status}")

    # -------------------------------------------------------------- nodes
    def discover_capabilities(self, state: AgentState) -> dict:
        self.say("\n[1] discover_capabilities - asking every MCP server what it offers")
        inventory = self.mcp.inventory()
        for name, inv in inventory.items():
            self.say(f"    • {name}: {inv['server'].get('name')} v{inv['server'].get('version')} "
                     f"| {len(inv['tools'])} tools, {inv['resources']} resources, protocol {inv['protocol']}")
        missing = REQUIRED_FS_TOOLS - set(inventory.get(FS, {}).get("tools", []))
        if missing:
            raise RuntimeError(f"filesystem MCP server is missing tools: {sorted(missing)}")
        db_enabled = DB in inventory
        if not db_enabled:
            self.say("    • candidate_db not connected - history and persistence steps will be skipped")
        return {"inventory": inventory, "db_enabled": db_enabled, "round": 0, "results": {}, "errors": [],
                "report_paths": [], "trace": _t("discover_capabilities", servers=list(inventory))}

    def load_job(self, state: AgentState) -> dict:
        self.say(f"\n[2] load_job - {state['job_path']}")
        doc = self.call(f"{FS}.extract_text", {"path": state["job_path"]})
        job = parse_job(doc["text"], state["job_path"])
        self.say(f"    title: {job['title']} | {len(job['required'])} required, "
                 f"{len(job['nice_to_have'])} nice-to-have, {job['min_years']}+ years")
        run_id = None
        if state.get("db_enabled"):
            run_id = self.call(f"{DB}.start_run", {"job_title": job["title"], "job_path": state["job_path"]})["run_id"]
            self.say(f"    run_id: {run_id}")
        return {"job": job, "run_id": run_id, "trace": _t("load_job", title=job["title"], run_id=run_id)}

    def discover_resumes(self, state: AgentState) -> dict:
        self.say(f"\n[3] discover_resumes - {state['resume_dir']}/{state['pattern']}")
        watch_id = None
        if state.get("watch"):
            # Start watching *before* listing so nothing arriving in between is missed.
            w = self.call(f"{FS}.watch_directory", {"action": "start", "directory": state["resume_dir"],
                                                    "patterns": "*.pdf,*.docx,*.txt,*.md", "auto_extract": False})
            watch_id = w["watch_id"]
            self.say(f"    watching {w['directory']} as {watch_id}")
        listing = self.call(f"{FS}.list_files", {"directory": state["resume_dir"], "pattern": state["pattern"]})
        paths = [f["path"] for f in listing["files"]]
        self.say(f"    found {len(paths)} resume file(s)")
        return {"to_process": paths, "watch_id": watch_id, "trace": _t("discover_resumes", found=len(paths))}

    def extract_resumes(self, state: AgentState) -> dict:
        paths = state["to_process"]
        self.say(f"\n[4] extract_resumes - batch_process over {len(paths)} file(s)")

        def progress(p: dict) -> None:
            if p.get("progress"):
                self.say(f"      progress {int(p['progress'])}/{int(p.get('total', 0))}  {p.get('message', '')}")

        batch = self.call(f"{FS}.batch_process", {"operation": "extract_text", "paths": paths},
                          on_progress=progress)
        extracted, errors = {}, list(state.get("errors", []))
        for r in batch["results"]:
            if r["status"] == "ok":
                extracted[r["path"]] = r["data"]["text"]
            else:
                err = r.get("error", {"type": "SKIPPED", "message": "not processed"})
                errors.append({"path": r["path"], "stage": "extract", "type": err["type"], "message": err["message"]})
                self.say(f"    ! skipped {r['path']}: {err['type']} - {err['message']}")
        self.say(f"    {batch['succeeded']} ok, {batch['failed']} failed in {batch['elapsed_ms']} ms "
                 f"using {batch['workers']} workers")
        return {"extracted": extracted, "errors": errors,
                "trace": _t("extract_resumes", ok=batch["succeeded"], failed=batch["failed"])}

    def parse_candidates(self, state: AgentState) -> dict:
        self.say("\n[5] parse_candidates")
        cands = [parse_resume(text, path) for path, text in state["extracted"].items()]
        for c in cands:
            self.say(f"    • {c['name']:<16} {c['email']:<28} {c['years_experience']:>4g} yrs  "
                     f"{len(c['skills'])} skills")
        return {"candidates": cands, "trace": _t("parse_candidates", parsed=len(cands))}

    def lookup_history(self, state: AgentState) -> dict:
        self.say("\n[6] lookup_history - candidate_db.candidate_history")
        cands = state["candidates"]
        if not cands:
            return {"trace": _t("lookup_history", known=0)}
        hist = self.call(f"{DB}.candidate_history", {"emails": [c["email"] for c in cands],
                                                     "job_title": state["job"]["title"]})["candidates"]
        known = 0
        for c in cands:
            h = hist.get(c["email"], {})
            prev = h.get("previous_matches", [])
            c["history"] = {"known": h.get("known", False), "previous_matches": prev[:5]}
            if h.get("known"):
                known += 1
                best = max((p["score"] for p in prev), default=None)
                self.say(f"    • {c['name']}: returning candidate, screened {len(prev)}x before"
                         + (f", best {best}" if best is not None else ""))
        if not known:
            self.say("    no returning candidates")
        return {"candidates": cands, "trace": _t("lookup_history", known=known)}

    def score_candidates(self, state: AgentState) -> dict:
        self.say("\n[7] score_candidates")
        results = dict(state.get("results", {}))
        for c in state["candidates"]:
            s = score_candidate(c, state["job"])
            public = {k: v for k, v in c.items() if not k.startswith("_")}
            results[c["path"]] = {**public, **s, "round": state.get("round", 0)}
            self.say(f"    • {c['name']:<16} {s['score']:>5}  {s['decision']}")
        return {"results": results, "trace": _t("score_candidates", scored=len(state["candidates"]))}

    def persist_results(self, state: AgentState) -> dict:
        self.say("\n[8] persist_results - candidate_db.upsert_candidate / record_matches")
        batch = []
        for c in state["candidates"]:
            r = state["results"][c["path"]]
            self.call(f"{DB}.upsert_candidate", {"candidate": {
                "email": c["email"], "name": c["name"], "phone": c["phone"] or "",
                "years_experience": c["years_experience"], "skills": c["skills"], "source_path": c["path"],
                "content_hash": c["content_hash"]}})
            batch.append({"email": c["email"], "score": r["score"], "decision": r["decision"],
                          "matched_required": r["matched_required"], "missing_required": r["missing_required"]})
        if batch:
            self.call(f"{DB}.record_matches", {"run_id": state["run_id"], "matches": batch})
        return {"trace": _t("persist_results", stored=len(batch))}

    def write_report(self, state: AgentState) -> dict:
        self.say("\n[9] write_report - filesystem.write_file")
        job = state["job"]
        ranked = sorted(state.get("results", {}).values(), key=lambda r: -r["score"])
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        counts = {d: sum(1 for r in ranked if r["decision"] == d) for d in ("shortlist", "review", "reject")}
        slug = re.sub(r"[^a-z0-9]+", "_", job["title"].lower()).strip("_")
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job": {k: job[k] for k in ("title", "path", "min_years")},
            "requirements": {"required": [r["label"] for r in job["required"]],
                             "nice_to_have": [r["label"] for r in job["nice_to_have"]]},
            "run_id": state.get("run_id"), "rounds": state.get("round", 0) + 1,
            "summary": {"screened": len(ranked), "errors": len(state.get("errors", [])), **counts},
            "scoring": {"weights": WEIGHTS, "thresholds": THRESHOLDS},
            "rankings": ranked, "errors": state.get("errors", []),
            "mcp": {"servers": {n: i["server"] for n, i in state["inventory"].items()},
                    "calls": len(self.calls), "call_log": self.calls[-200:]},
        }
        json_path = f"output/match_report_{slug}.json"
        md_path = f"output/match_report_{slug}.md"
        self.call(f"{FS}.write_file", {"path": json_path, "content": json.dumps(report, indent=2), "overwrite": True})
        self.call(f"{FS}.write_file", {"path": md_path, "content": self.render_markdown(report), "overwrite": True})
        if state.get("db_enabled") and state.get("run_id"):
            self.call(f"{DB}.finish_run", {"run_id": state["run_id"], "total": len(ranked),
                                           "shortlisted": counts["shortlist"],
                                           "notes": f"round {state.get('round', 0) + 1}"})
        self.print_ranking(ranked)
        return {"report_paths": [json_path, md_path], "trace": _t("write_report", path=json_path)}

    def wait_for_resumes(self, state: AgentState) -> dict:
        remaining = state["watch_deadline"] - time.time()
        self.say(f"\n[10] wait_for_resumes - polling {state['watch_id']} ({max(0, int(remaining))}s left; "
                 f"drop a resume into data/resumes/)")
        new: list[str] = []
        while time.time() < state["watch_deadline"] and not new:
            timeout = max(0.0, min(state["poll_timeout_s"], state["watch_deadline"] - time.time()))
            events = self.call(f"{FS}.watch_directory", {"action": "poll", "watch_id": state["watch_id"],
                                                         "timeout_s": round(timeout, 1)})["events"]
            for e in events:
                self.say(f"    event: {e['type']} {e['path']}")
                if e["type"] in ("created", "modified") and e["path"] not in new:
                    new.append(e["path"])
        if not new:
            self.call(f"{FS}.watch_directory", {"action": "stop", "watch_id": state["watch_id"]})
            self.say("    watch window closed - stopping")
        return {"to_process": new, "round": state.get("round", 0) + (1 if new else 0),
                "trace": _t("wait_for_resumes", new=len(new))}

    # --------------------------------------------------------- routing
    @staticmethod
    def route_after_discover(state: AgentState) -> str:
        if state["to_process"]:
            return "extract_resumes"
        return "wait_for_resumes" if state.get("watch") else "write_report"

    @staticmethod
    def route_after_parse(state: AgentState) -> str:
        return "lookup_history" if state.get("db_enabled") else "score_candidates"

    @staticmethod
    def route_after_score(state: AgentState) -> str:
        return "persist_results" if state.get("db_enabled") else "write_report"

    @staticmethod
    def route_after_report(state: AgentState) -> str:
        if state.get("watch") and state.get("round", 0) < state.get("max_rounds", 10) \
                and time.time() < state.get("watch_deadline", 0):
            return "wait_for_resumes"
        return END

    @staticmethod
    def route_after_wait(state: AgentState) -> str:
        return "extract_resumes" if state["to_process"] else END

    def _build_graph(self):
        g = StateGraph(AgentState)
        for name in ("discover_capabilities", "load_job", "discover_resumes", "extract_resumes",
                     "parse_candidates", "lookup_history", "score_candidates", "persist_results",
                     "write_report", "wait_for_resumes"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "discover_capabilities")
        g.add_edge("discover_capabilities", "load_job")
        g.add_edge("load_job", "discover_resumes")
        g.add_conditional_edges("discover_resumes", self.route_after_discover,
                                ["extract_resumes", "wait_for_resumes", "write_report"])
        g.add_edge("extract_resumes", "parse_candidates")
        g.add_conditional_edges("parse_candidates", self.route_after_parse, ["lookup_history", "score_candidates"])
        g.add_edge("lookup_history", "score_candidates")
        g.add_conditional_edges("score_candidates", self.route_after_score, ["persist_results", "write_report"])
        g.add_edge("persist_results", "write_report")
        g.add_conditional_edges("write_report", self.route_after_report, ["wait_for_resumes", END])
        g.add_conditional_edges("wait_for_resumes", self.route_after_wait, ["extract_resumes", END])
        return g.compile()

    # ------------------------------------------------------------ output
    @staticmethod
    def render_markdown(report: dict) -> str:
        s = report["summary"]
        out = [f"# Match report: {report['job']['title']}", "",
               f"Generated {report['generated_at']} · run `{report['run_id']}` · "
               f"{s['screened']} screened · {s['shortlist']} shortlisted · {s['review']} to review · "
               f"{s['reject']} rejected · {s['errors']} file errors", "",
               "| # | Candidate | File | Score | Decision | Missing required | History |",
               "|---|---|---|---|---|---|---|"]
        for r in report["rankings"]:
            prev = r.get("history", {}).get("previous_matches", [])
            hist = f"{len(prev)} previous" if prev else "new"
            out.append(f"| {r['rank']} | {r['name']} | `{r['path']}` | **{r['score']}** | {r['decision']} | "
                       f"{', '.join(r['missing_required']) or '-'} | {hist} |")
        out += ["", "## Rationale", ""]
        out += [f"- **{r['name']}** - {r['rationale']}" for r in report["rankings"]]
        if report["errors"]:
            out += ["", "## Files that could not be processed", ""]
            out += [f"- `{e['path']}` - {e['type']}: {e['message']}" for e in report["errors"]]
        out += ["", "## MCP servers used", ""]
        out += [f"- `{n}`: {i.get('name')} {i.get('version')}" for n, i in report["mcp"]["servers"].items()]
        out += [f"- {report['mcp']['calls']} MCP tool calls in total", ""]
        return "\n".join(out)

    def print_ranking(self, ranked: list[dict]) -> None:
        if not self.verbose:
            return
        print("\n    ┌──────┬──────────────────┬────────┬───────────┐")
        print("    │ rank │ candidate        │ score  │ decision  │")
        print("    ├──────┼──────────────────┼────────┼───────────┤")
        for r in ranked:
            print(f"    │ {r['rank']:>4} │ {r['name'][:16]:<16} │ {r['score']:>6} │ {r['decision']:<9} │")
        print("    └──────┴──────────────────┴────────┴───────────┘")

    # --------------------------------------------------------------- run
    def run(self, job_path: str, resume_dir: str = "resumes", pattern: str = "*", watch: bool = False,
            watch_seconds: float = 60, poll_timeout_s: float = 10, max_rounds: int = 10) -> dict:
        initial: AgentState = {
            "job_path": job_path, "resume_dir": resume_dir, "pattern": pattern, "watch": watch,
            "watch_deadline": time.time() + watch_seconds, "poll_timeout_s": poll_timeout_s,
            "max_rounds": max_rounds, "trace": [],
        }
        return self.graph.invoke(initial, {"recursion_limit": 25 + 8 * max_rounds})


# =============================================================================
# CLI
# =============================================================================
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="LangGraph resume matcher that uses MCP servers for all I/O")
    p.add_argument("--job", default="jobs/senior_python_ai_engineer.md", help="Job description (sandbox path)")
    p.add_argument("--resumes", default="resumes", help="Resume directory (sandbox path)")
    p.add_argument("--pattern", default="*", help="Glob(s), e.g. '*.pdf,*.docx'")
    p.add_argument("--servers", default="config/mcp_servers.json", help="MCP servers config")
    p.add_argument("--no-db", action="store_true", help="Connect to the filesystem server only")
    p.add_argument("--watch", action="store_true", help="Keep watching for new resumes after the first pass")
    p.add_argument("--watch-seconds", type=float, default=60)
    p.add_argument("--max-rounds", type=int, default=10)
    p.add_argument("--mermaid", action="store_true", help="Print the LangGraph diagram as Mermaid and exit")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--log-level", default="WARNING")
    a = p.parse_args(argv)
    # The progress output uses box-drawing characters; keep it printable on Windows consoles and redirects.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=a.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    only = ["filesystem"] if a.no_db else None
    with MultiMCPClient.from_config_file(a.servers, only=only, required=(FS,)) as mcp:
        agent = ResumeMatchingAgent(mcp, verbose=not a.quiet)
        if a.mermaid:
            print(agent.graph.get_graph().draw_mermaid())
            return 0
        fs = mcp[FS]
        fs.on_notification(lambda m, prm: agent.say(
            f"    🔔 {m}: {prm.get('data', {}).get('event', '')} {prm.get('data', {}).get('path', '')}")
            if m == "notifications/message" and prm.get("logger") == "watch_directory" else None)
        try:
            final = agent.run(a.job, a.resumes, a.pattern, a.watch, a.watch_seconds, max_rounds=a.max_rounds)
        except (MCPToolError, MCPClientError) as exc:
            print(f"\nMCP error: {exc}", file=sys.stderr)
            return 1
        print(f"\nDone. {len(final.get('results', {}))} candidates ranked, "
              f"{len(final.get('errors', []))} file error(s), {len(agent.calls)} MCP calls.")
        print("Reports:", ", ".join(final.get("report_paths", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
