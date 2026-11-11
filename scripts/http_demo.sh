#!/usr/bin/env bash
# Exercise the HTTP transport. Start the server first:
#   python filesystem_mcp_server.py --config config/filesystem_server.json --transport http --port 8765
set -u
URL=${URL:-http://127.0.0.1:8765}
post() { echo; echo "→ $1"; curl -s -w '\n  HTTP %{http_code}\n' -H 'Content-Type: application/json' -d "$1" "$URL/mcp"; }

echo "GET /health";                curl -s "$URL/health"; echo
post '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
post '{"jsonrpc":"2.0","id":2,"method":"resources/templates/list"}'
post '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"batch_process","arguments":{"operation":"hash","directory":"resumes"}}}'
post '[{"jsonrpc":"2.0","id":4,"method":"ping"},{"jsonrpc":"2.0","id":5,"method":"no/such/method"}]'
post '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"../../etc/passwd"}}}'
post '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"list_files","arguments":{"directory":42}}}'
post '{"jsonrpc":"2.0","method":"notifications/initialized"}'
post '{broken json'
