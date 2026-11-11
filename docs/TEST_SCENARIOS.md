# Test scenarios

Run everything with `python -m pytest -q` (60 tests, ~15 s). Each test gets a fresh copy of the sample
data in a temporary folder, so runs never touch your real `data/` or `output/`.

| # | Scenario | What it proves | Test |
|---|---|---|---|
| **JSON-RPC 2.0 & lifecycle** ||| `tests/test_jsonrpc_protocol.py` |
| 1 | Malformed JSON | -32700 with `id: null` | `test_parse_error_returns_minus_32700_with_null_id` |
| 2 | Non-object, missing `jsonrpc`, float id, no method, positional params | -32600 / -32602 | `test_invalid_requests` |
| 3 | Unknown method | -32601 lists available methods | `test_method_not_found` |
| 4 | String ids | id echoed exactly | `test_response_echoes_id_and_version` |
| 5 | Notifications | never answered, even unknown ones | `test_notifications_get_no_response` |
| 6 | Batch with requests, a notification, an error | ordered responses, notification skipped; empty batch rejected | `test_batch_mixes_results_and_errors_and_skips_notifications` |
| 7 | Call before `initialize` | -32010; `ping` still allowed | `test_requests_before_initialize_are_rejected` |
| 8 | Version negotiation | echoes supported version, falls back to newest | `test_protocol_version_negotiation` |
| 9 | Bad tool arguments | schema validation → -32602 with problem list | `test_tool_argument_validation_is_a_protocol_error` |
| 10 | Pagination | cursors walk all tools, bad cursor rejected | `test_pagination_with_cursor` |
| 11 | One-shot discovery | `server/discover` returns tools, templates, error codes | `test_discovery_endpoint_lists_everything` |
| **Milestone 1 tools over MCP** ||| `tests/test_filesystem_tools.py` |
| 12 | List roots & files, glob filters | | `test_list_roots_and_files` |
| 13 | Text from PDF, DOCX, TXT, MD | | `test_extract_text_all_formats` |
| 14 | Truncation, SHA-256 metadata, search inside PDFs/DOCX | | `test_extract_text_truncation`, `test_metadata_hash_matches_file`, `test_search_files_across_pdf_and_docx` |
| 15 | Corrupt PDF | tool error result, not a crash | `test_corrupted_pdf_is_a_tool_error_not_a_crash` |
| 16 | `../`, absolute paths, unknown roots, symlink out of sandbox | ACCESS_DENIED | `test_sandbox_blocks_escapes`, `test_symlink_escape_blocked` |
| 17 | Write rules | read-only roots, no silent overwrite, extension allow-list, list_changed notification | `test_write_file_rules` |
| 18 | Runtime configuration | limit changes apply immediately; roots can't be changed | `test_configuration_management` |
| **Resources, watch_directory, batch_process** ||| `tests/test_resources_watch_batch.py` |
| 19 | Resource discovery & templates | files appear as `fs://` resources with MIME types | `test_resource_discovery` |
| 20 | Read file / metadata / tool-schema resources | tools exposed as resources | `test_read_file_resources`, `test_tools_exposed_as_resources` |
| 21 | Subscribe → `notifications/resources/updated` on change | | `test_subscribe_and_updated_notification` |
| 22 | New resume dropped in | `created` event with text preview, push notifications, new resource listed | `test_watch_detects_new_resume_with_preview` |
| 23 | Pattern filtering, deletions | | `test_watch_ignores_other_patterns_and_reports_deletes` |
| 24 | Watch lifecycle, unknown id, limit | | `test_watch_lifecycle_and_errors`, `test_watch_limit` |
| 25 | Batch over a directory with one bad file | 6 ok / 1 error, batch still succeeds | `test_batch_extract_directory_with_partial_failure` |
| 26 | Explicit paths: dedupe, order kept, missing file | | `test_batch_paths_dedupe_order_and_operations` |
| 27 | `continue_on_error=false` | stops early, rest skipped | `test_batch_stop_on_first_error` |
| 28 | Progress notifications | 0 … N with the client's token | `test_batch_progress_notifications` |
| **Transports & interoperability** ||| `tests/test_transports_and_interop.py` |
| 29 | Real stdio subprocess | handshake, tool error vs protocol error | `test_stdio_handshake_and_tools` |
| 30 | Push notifications over stdio | watch events reach the client | `test_stdio_push_notifications_for_new_resume` |
| 31 | Progress callback & concurrency | long poll doesn't block other calls | `test_stdio_progress_callback`, `test_requests_run_concurrently` |
| 32 | HTTP transport | 200 / 202 / 400 / 415, health, well-known discovery | `test_http_transport_and_status_codes` |
| 33 | Official MCP Python SDK client | server is spec-compliant | `test_official_mcp_sdk_client_interop` |
| **Agent workflow (Part B)** ||| `tests/test_agent_workflow.py` |
| 34 | Job / resume parsing and scoring | | `test_job_parsing`, `test_scoring_orders_strong_above_weak` |
| 35 | No direct file access in the agent | AST check for `open`, `os`, `pathlib`, `pypdf`, ... | `test_agent_has_no_direct_filesystem_access` |
| 36 | Full run with two MCP servers | ranking, report written via MCP, node order | `test_full_run_with_two_mcp_servers` |
| 37 | Second run | returning candidates recognised from the DB | `test_second_run_recognises_returning_candidates` |
| 38 | Filesystem server only | DB nodes skipped by conditional edges | `test_filesystem_only_mode` |
| 39 | Watch mode | new arrival screened, graph loops | `test_watch_mode_screens_new_arrival` |
| 40 | LangChain adapters | MCP tools usable by an LLM agent | `test_langchain_tool_adapters` |
| 41 | DB server safety | read-only SQL, unknown run id | `test_database_server_read_only_sql` |

## Expected results on the sample data

| Rank | Candidate | Format | Score | Decision |
|---|---|---|---|---|
| 1 | Priya Raman | PDF | 97.1 | shortlist |
| – | Kavya Nair (arrives in watch mode) | TXT | 91.4 | shortlist |
| 2 | Arjun Mehta | DOCX | 73.9 | review |
| 3 | Sarah Chen | TXT | 65.9 | review |
| 4 | David Okafor | PDF | 61.3 | review |
| 5 | Rahul Verma | MD | 49.4 | reject |
| 6 | Meera Iyer | TXT | 23.1 | reject |
| – | corrupted_upload.pdf | PDF | – | listed under file errors (EXTRACTION_FAILED) |
