# Demo video script (5–6 minutes)

**Setup before recording:** two terminals side by side, editor open on the project, font size up.
Reset state:
```powershell
# Windows PowerShell
Remove-Item data\candidates.db, output\*, data\resumes\kavya_nair.txt -ErrorAction SilentlyContinue
python scripts/generate_sample_data.py
```
```bash
# macOS / Linux
rm -f data/candidates.db output/* data/resumes/kavya_nair.txt && python scripts/generate_sample_data.py
```

---

### 0:00 – 0:30 · Intro
*Show `docs/workflow_diagram.png`.*

> "In Milestone 1 the agent called file functions directly. In Milestone 2 those tools live in an MCP
> server, and the LangGraph agent reaches files only through a standard JSON-RPC 2.0 interface. I also
> added a second MCP server — a candidate database — to show one agent using several MCP servers."

### 0:30 – 1:45 · The MCP server on its own
*Terminal 1:*
```bash
python filesystem_mcp_server.py --config config/filesystem_server.json --transport http --port 8765
```
*Terminal 2:*
```bash
python scripts/http_demo.py
```
Point out, in order:
- `initialize` → negotiated protocol version and capabilities (tools, resources with subscribe/listChanged, logging)
- `resources/templates/list` → `fs://{root}/{path}`, `tools://{name}` — resource discovery
- `batch_process` with `hash` over the whole folder → one call, 7 files
- batch request → a result and a -32601 error in one reply
- `../../etc/passwd` → `isError: true`, `ACCESS_DENIED` (sandbox)
- wrong argument type → -32602 with the schema problem
- notification → **HTTP 202**, broken JSON → **-32700 / HTTP 400**

Stop the HTTP server. *(Mention: stdio is the primary transport, used by the agent and any MCP host.)*

### 1:45 – 2:30 · Code walkthrough (quick)
- `filesystem_mcp_server.py`: `MCPServer.handle_message` (validation, notifications vs requests, error mapping),
  `ErrorCode`, the `@self.tool(...)` registrations, `ServerConfig.load` (file < env < `configure_server`).
- `watch_directory` / `DirectoryWatcher.check` — settle detection; `batch_process` — thread pool + progress.
- `matching_agent.py`: `_build_graph` (nodes, conditional edges); `self.call("filesystem.batch_process", ...)`.
  "There is no `open()` or `os` import in the agent — a test enforces that."

### 2:30 – 3:45 · End-to-end batch run
```bash
python matching_agent.py
```
Narrate as it prints:
- step 1: both servers discovered (11 + 8 tools)
- step 4: progress notifications streaming from `batch_process`; the corrupt PDF is skipped, not fatal
- steps 6 & 8: the database server — history lookup, then persistence
- the ranking table; open `output/match_report_senior_python_ai_engineer.md`

Run it **again** and show step 6: "returning candidate, screened 1x before" — data from the second MCP server.

### 3:45 – 5:00 · Watch mode
*Terminal 1:*
```bash
python matching_agent.py --watch --watch-seconds 45
```
*Terminal 2, once it says "wait_for_resumes":*
```powershell
Copy-Item data\incoming_samples\kavya_nair.txt data\resumes\     # macOS/Linux: cp data/incoming_samples/kavya_nair.txt data/resumes/
```
Show: the 🔔 push notification, the poll returning a `created` event, the graph looping back to
`extract_resumes` for just the new file, and the updated ranking with Kavya at #2. Let the window close
("watch window closed - stopping").

### 5:00 – 5:40 · Tests and interoperability
```bash
python -m pytest -q
python -m pytest -q tests/test_transports_and_interop.py -k official -v
```
> "60 scenarios: JSON-RPC compliance, sandbox, watch, batch, both transports, and the agent workflow.
> This one connects with the *official* MCP Python SDK client, so the server works with any MCP host —
> for example Claude Desktop, using the snippet in the README."

### 5:40 – 6:00 · Wrap-up
Back to the diagram: custom tools → standardized MCP interface, cleaner agent, safer file access,
new capabilities (watching, batching), and a second server plugged in through the same client.
