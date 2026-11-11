#!/usr/bin/env python3
"""
database_mcp_server.py  (Part B bonus: a second MCP server)
============================================================

A SQLite-backed "candidate database" MCP server. The matching agent connects to
it *and* to the filesystem server, and uses both in one workflow:

* before scoring: ``candidate_history`` - has this person applied before?
* after scoring:  ``upsert_candidate`` + ``record_matches`` - persist results
* any time:       ``search_candidates`` / ``top_matches`` / read-only ``run_sql``

It reuses the JSON-RPC 2.0 / MCP protocol core from ``filesystem_mcp_server``,
so both servers share identical framing, lifecycle and error semantics.

    python database_mcp_server.py --db data/candidates.db
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
import uuid
from pathlib import Path

from filesystem_mcp_server import ErrorCode, MCPError, MCPServer, utc_now

log = logging.getLogger("candidate-db-mcp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    email            TEXT PRIMARY KEY,
    name             TEXT,
    phone            TEXT,
    years_experience REAL,
    skills           TEXT,          -- JSON array
    source_path      TEXT,
    content_hash     TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS match_runs (
    run_id       TEXT PRIMARY KEY,
    job_title    TEXT NOT NULL,
    job_path     TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    total        INTEGER,
    shortlisted  INTEGER,
    notes        TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL REFERENCES match_runs(run_id),
    candidate_email  TEXT NOT NULL REFERENCES candidates(email),
    job_title        TEXT NOT NULL,
    score            REAL NOT NULL,
    decision         TEXT NOT NULL,
    matched_required TEXT,
    missing_required TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_email ON matches(candidate_email);
CREATE INDEX IF NOT EXISTS idx_matches_job ON matches(job_title, score DESC);
"""


class CandidateDatabase:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self, readonly: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if readonly:
            def authorizer(action, *args):
                allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
                return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
            conn.set_authorizer(authorizer)
        return conn

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("skills", "matched_required", "missing_required"):
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    pass
        return d

    # --------------------------------------------------------------- writes
    def upsert_candidate(self, c: dict) -> dict:
        email = (c.get("email") or "").strip().lower()
        if not email:
            raise MCPError(ErrorCode.INVALID_PARAMS, "candidate.email is required")
        now = utc_now()
        with self._lock, self._conn() as conn:
            exists = conn.execute("SELECT 1 FROM candidates WHERE email=?", (email,)).fetchone()
            conn.execute("""
                INSERT INTO candidates(email,name,phone,years_experience,skills,source_path,content_hash,first_seen,last_seen)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(email) DO UPDATE SET name=excluded.name, phone=excluded.phone,
                    years_experience=excluded.years_experience, skills=excluded.skills,
                    source_path=excluded.source_path, content_hash=excluded.content_hash, last_seen=excluded.last_seen
            """, (email, c.get("name"), c.get("phone"), c.get("years_experience"), json.dumps(c.get("skills", [])),
                  c.get("source_path"), c.get("content_hash"), now, now))
            row = conn.execute("SELECT * FROM candidates WHERE email=?", (email,)).fetchone()
        return {"status": "updated" if exists else "inserted", "candidate": self._row(row)}

    def start_run(self, job_title: str, job_path: str | None) -> dict:
        run_id = "run_" + uuid.uuid4().hex[:10]
        with self._lock, self._conn() as conn:
            conn.execute("INSERT INTO match_runs(run_id,job_title,job_path,started_at) VALUES(?,?,?,?)",
                         (run_id, job_title, job_path, utc_now()))
        return {"run_id": run_id, "job_title": job_title}

    def record_matches(self, run_id: str, matches: list[dict]) -> dict:
        now = utc_now()
        with self._lock, self._conn() as conn:
            run = conn.execute("SELECT job_title FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                raise MCPError(ErrorCode.RESOURCE_NOT_FOUND, f"Unknown run_id {run_id}")
            rows = []
            for m in matches:
                email = (m.get("email") or "").lower()
                if not conn.execute("SELECT 1 FROM candidates WHERE email=?", (email,)).fetchone():
                    raise MCPError(ErrorCode.INVALID_PARAMS, f"Candidate {email!r} not found; upsert it first")
                rows.append((run_id, email, run["job_title"], float(m["score"]), m.get("decision", "review"),
                             json.dumps(m.get("matched_required", [])), json.dumps(m.get("missing_required", [])),
                             now))
            conn.executemany("""INSERT INTO matches(run_id,candidate_email,job_title,score,decision,
                                matched_required,missing_required,created_at) VALUES(?,?,?,?,?,?,?,?)""", rows)
        return {"run_id": run_id, "recorded": len(rows)}

    def finish_run(self, run_id: str, total: int, shortlisted: int, notes: str = "") -> dict:
        with self._lock, self._conn() as conn:
            cur = conn.execute("UPDATE match_runs SET finished_at=?, total=?, shortlisted=?, notes=? WHERE run_id=?",
                               (utc_now(), total, shortlisted, notes, run_id))
            if cur.rowcount == 0:
                raise MCPError(ErrorCode.RESOURCE_NOT_FOUND, f"Unknown run_id {run_id}")
            row = conn.execute("SELECT * FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row(row)

    # ---------------------------------------------------------------- reads
    def candidate_history(self, emails: list[str], job_title: str | None = None) -> dict:
        out = {}
        with self._conn() as conn:
            for e in emails:
                email = e.strip().lower()
                cand = conn.execute("SELECT * FROM candidates WHERE email=?", (email,)).fetchone()
                q = "SELECT run_id, job_title, score, decision, created_at FROM matches WHERE candidate_email=?"
                args: list = [email]
                if job_title:
                    q += " AND job_title=?"
                    args.append(job_title)
                hist = conn.execute(q + " ORDER BY created_at DESC LIMIT 20", args).fetchall()
                out[email] = {"known": cand is not None, "candidate": self._row(cand),
                              "previous_matches": [dict(h) for h in hist]}
        return {"candidates": out, "known_count": sum(1 for v in out.values() if v["known"])}

    def search_candidates(self, skill: str | None, min_years: float | None, limit: int) -> dict:
        q, args = "SELECT * FROM candidates WHERE 1=1", []
        if skill:
            q += " AND lower(skills) LIKE ?"
            args.append(f"%{skill.lower()}%")
        if min_years is not None:
            q += " AND years_experience >= ?"
            args.append(min_years)
        with self._conn() as conn:
            rows = conn.execute(q + " ORDER BY years_experience DESC LIMIT ?", (*args, limit)).fetchall()
        return {"count": len(rows), "candidates": [self._row(r) for r in rows]}

    def top_matches(self, job_title: str | None, limit: int) -> dict:
        q = """SELECT m.candidate_email AS email, c.name, m.job_title, MAX(m.score) AS best_score,
                      COUNT(*) AS times_screened, MAX(m.created_at) AS last_screened
               FROM matches m JOIN candidates c ON c.email = m.candidate_email"""
        args: list = []
        if job_title:
            q += " WHERE m.job_title = ?"
            args.append(job_title)
        q += " GROUP BY m.candidate_email, m.job_title ORDER BY best_score DESC LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(q, (*args, limit)).fetchall()
        return {"count": len(rows), "matches": [dict(r) for r in rows]}

    def run_sql(self, query: str, params: list, limit: int) -> dict:
        if not query.lstrip().lower().startswith(("select", "with")):
            raise MCPError(ErrorCode.ACCESS_DENIED, "Only read-only SELECT queries are allowed")
        try:
            with self._conn(readonly=True) as conn:
                cur = conn.execute(query, params)
                rows = cur.fetchmany(limit)
                cols = [d[0] for d in cur.description or []]
        except sqlite3.DatabaseError as exc:
            raise MCPError(ErrorCode.INVALID_PARAMS, f"SQL error: {exc}") from None
        return {"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows), "limit": limit}

    def stats(self) -> dict:
        with self._conn() as conn:
            return {
                "candidates": conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
                "runs": conn.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0],
                "matches": conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0],
                "database": self.path,
            }

    def runs(self, run_id: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if run_id:
                run = conn.execute("SELECT * FROM match_runs WHERE run_id=?", (run_id,)).fetchone()
                if not run:
                    return []
                matches = conn.execute("SELECT * FROM matches WHERE run_id=? ORDER BY score DESC",
                                       (run_id,)).fetchall()
                return [{**dict(run), "matches": [self._row(m) for m in matches]}]
            return [dict(r) for r in conn.execute("SELECT * FROM match_runs ORDER BY started_at DESC LIMIT 50")]


class CandidateDBServer(MCPServer):
    def __init__(self, db_path: str):
        super().__init__("candidate-db-mcp", "1.0.0",
                         "Candidate database for the resume matcher. Look up history with candidate_history, "
                         "persist results with upsert_candidate + start_run/record_matches/finish_run.")
        self.db = CandidateDatabase(db_path)
        self._register()

    def capabilities(self) -> dict:
        return {"tools": {"listChanged": False}, "resources": {"subscribe": False, "listChanged": False},
                "logging": {}}

    def _register(self) -> None:
        db = self.db
        ro = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
        rw = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
        candidate_schema = {"type": "object", "properties": {
            "email": {"type": "string", "minLength": 3}, "name": {"type": "string"}, "phone": {"type": "string"},
            "years_experience": {"type": "number"}, "skills": {"type": "array", "items": {"type": "string"}},
            "source_path": {"type": "string"}, "content_hash": {"type": "string"}}, "required": ["email"]}

        @self.tool("upsert_candidate", "Insert or update a candidate profile (keyed by email).",
                   {"type": "object", "properties": {"candidate": candidate_schema},
                    "required": ["candidate"], "additionalProperties": False}, annotations=rw)
        def upsert_candidate(ctx, candidate):
            return db.upsert_candidate(candidate)

        @self.tool("candidate_history", "For a list of emails: is the candidate known, and their previous match results.",
                   {"type": "object", "properties": {
                       "emails": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                       "job_title": {"type": "string"}},
                    "required": ["emails"], "additionalProperties": False}, annotations=ro)
        def candidate_history(ctx, emails, job_title=None):
            return db.candidate_history(emails, job_title)

        @self.tool("start_run", "Open a new screening run for a job; returns run_id.",
                   {"type": "object", "properties": {"job_title": {"type": "string", "minLength": 1},
                                                     "job_path": {"type": "string"}},
                    "required": ["job_title"], "additionalProperties": False}, annotations=rw)
        def start_run(ctx, job_title, job_path=None):
            return db.start_run(job_title, job_path)

        @self.tool("record_matches", "Store scored matches for a run.",
                   {"type": "object", "properties": {
                       "run_id": {"type": "string"},
                       "matches": {"type": "array", "items": {"type": "object", "properties": {
                           "email": {"type": "string"}, "score": {"type": "number"}, "decision": {"type": "string"},
                           "matched_required": {"type": "array"}, "missing_required": {"type": "array"}},
                           "required": ["email", "score"]}}},
                    "required": ["run_id", "matches"], "additionalProperties": False}, annotations=rw)
        def record_matches(ctx, run_id, matches):
            return db.record_matches(run_id, matches)

        @self.tool("finish_run", "Close a run with its totals.",
                   {"type": "object", "properties": {
                       "run_id": {"type": "string"}, "total": {"type": "integer", "minimum": 0},
                       "shortlisted": {"type": "integer", "minimum": 0}, "notes": {"type": "string"}},
                    "required": ["run_id", "total", "shortlisted"], "additionalProperties": False}, annotations=rw)
        def finish_run(ctx, run_id, total, shortlisted, notes=""):
            return db.finish_run(run_id, total, shortlisted, notes)

        @self.tool("search_candidates", "Find stored candidates by skill and/or minimum years of experience.",
                   {"type": "object", "properties": {
                       "skill": {"type": "string"}, "min_years": {"type": "number", "minimum": 0},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 20}},
                    "additionalProperties": False}, annotations=ro)
        def search_candidates(ctx, skill=None, min_years=None, limit=20):
            return db.search_candidates(skill, min_years, limit)

        @self.tool("top_matches", "Best score per candidate, optionally for one job title.",
                   {"type": "object", "properties": {
                       "job_title": {"type": "string"},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 10}},
                    "additionalProperties": False}, annotations=ro)
        def top_matches(ctx, job_title=None, limit=10):
            return db.top_matches(job_title, limit)

        @self.tool("run_sql", "Run a read-only SELECT against the candidate database.",
                   {"type": "object", "properties": {
                       "query": {"type": "string", "minLength": 6},
                       "params": {"type": "array", "default": []},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}},
                    "required": ["query"], "additionalProperties": False}, annotations=ro)
        def run_sql(ctx, query, params=None, limit=100):
            return db.run_sql(query, params or [], limit)

    def resource_list(self) -> list[dict]:
        return [
            {"uri": "db://schema", "name": "schema", "mimeType": "text/x-sql", "description": "Database schema"},
            {"uri": "db://stats", "name": "stats", "mimeType": "application/json", "description": "Row counts"},
            {"uri": "db://runs", "name": "runs", "mimeType": "application/json", "description": "Recent runs"},
        ]

    def resource_templates(self) -> list[dict]:
        return [{"uriTemplate": "db://runs/{run_id}", "name": "run", "mimeType": "application/json",
                 "description": "One run with all its matches"}]

    def resource_read(self, uri: str) -> list[dict]:
        def js(obj):
            return [{"uri": uri, "mimeType": "application/json", "text": json.dumps(obj, indent=2, default=str)}]
        if uri == "db://schema":
            return [{"uri": uri, "mimeType": "text/x-sql", "text": SCHEMA.strip()}]
        if uri == "db://stats":
            return js(self.db.stats())
        if uri == "db://runs":
            return js(self.db.runs())
        if uri.startswith("db://runs/"):
            found = self.db.runs(uri[len("db://runs/"):])
            if found:
                return js(found[0])
        raise MCPError(ErrorCode.RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Candidate database MCP server")
    p.add_argument("--db", default=os.environ.get("MCP_DB_PATH", "data/candidates.db"))
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=a.log_level.upper(),
                        format="%(asctime)s %(levelname)s [db-mcp] %(message)s")
    server = CandidateDBServer(a.db)
    log.info("database: %s", Path(a.db).resolve())
    if a.transport == "http":
        server.serve_http(port=a.port)
    else:
        server.serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
