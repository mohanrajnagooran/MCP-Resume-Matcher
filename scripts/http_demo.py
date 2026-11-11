#!/usr/bin/env python3
"""Exercise the HTTP transport (works on Windows, macOS and Linux).

Start the server first, in another terminal:
    python filesystem_mcp_server.py --config config/filesystem_server.json --transport http --port 8765
Then:
    python scripts/http_demo.py
"""
import json
import sys
import urllib.error
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def show(body: bytes) -> None:
    text = body.decode("utf-8", errors="replace")
    try:
        text = json.dumps(json.loads(text), indent=2) if text else "(no body)"
    except json.JSONDecodeError:
        pass
    lines = text.splitlines()
    print("\n".join(lines[:25]) + ("\n  ..." if len(lines) > 25 else ""))


def post(title: str, payload: str) -> None:
    print(f"\n=== {title}\n--> {payload}")
    req = urllib.request.Request(URL + "/mcp", data=payload.encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, body = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read()
    print(f"<-- HTTP {status}")
    show(body)


def main() -> int:
    try:
        with urllib.request.urlopen(URL + "/health", timeout=5) as r:
            print("=== GET /health")
            show(r.read())
    except OSError as exc:
        print(f"Cannot reach {URL}: {exc}\nStart the server with --transport http first.")
        return 1
    post("initialize (version negotiation + capabilities)",
         '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",'
         '"capabilities":{},"clientInfo":{"name":"demo","version":"1"}}}')
    post("resource discovery: templates", '{"jsonrpc":"2.0","id":2,"method":"resources/templates/list"}')
    post("batch_process: hash every resume in one call",
         '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"batch_process",'
         '"arguments":{"operation":"hash","directory":"resumes"}}}')
    post("JSON-RPC batch: one success + one unknown method (-32601)",
         '[{"jsonrpc":"2.0","id":4,"method":"ping"},{"jsonrpc":"2.0","id":5,"method":"no/such/method"}]')
    post("sandbox: path escape -> isError + ACCESS_DENIED",
         '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"read_file",'
         '"arguments":{"path":"../../etc/passwd"}}}')
    post("schema validation: wrong argument type -> -32602",
         '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"list_files","arguments":{"directory":42}}}')
    post("notification -> HTTP 202, no body", '{"jsonrpc":"2.0","method":"notifications/initialized"}')
    post("malformed JSON -> -32700, HTTP 400", "{broken json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
