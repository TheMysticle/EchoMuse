# Tool selector preview

From the repository root, run:

```powershell
python tools/tool_selector_preview/serve.py
```

Open **http://127.0.0.1:8765**. No Python packages, keys, HA connection or model
are required. `--port 8766` selects another local port.

The HTML sends sentences to a loopback-only Python server, which imports
`tools/pipecat_echomuse/tool_selector.py` directly. There is no JavaScript
reimplementation of the matching rules. The packaged add-on imports this same
module before composed voice LLM inference, after local command handling.

The editable sample catalogue is illustrative, not a snapshot of a real HA
installation. Paste JSON tool names, MCP tool definitions/list_tools results,
or OpenAI function tool definitions to test a different catalogue. Names are
matched exactly after removing MCP namespaces, without guessing from tool
descriptions. Unknown custom tools remain exposed rather than disappearing.
Generic HA tools can belong to multiple categories. Context/continuation
tools stay available. Selection only removes tools from the supplied list;
it never discovers new tools or changes configured allowlists.

English whole-clause rules cover lighting, weather, music, climate, covers,
speaker schedules, and clear conversation. Multiple recognized clauses produce the union of
categories. Unknown clauses, conditional dependencies, incomplete commands,
and ambiguous follow-ups keep the full catalogue. This is intentionally
conservative. It selects capabilities, not rooms, arguments or actions.

Clear self-contained chat, such as greetings, jokes and general knowledge
questions, omits HA and custom tools. Only continuation (if configured) and
the runtime expansion hook remain. Indirect device questions, personal state,
time queries and uncertain follow-ups keep home capabilities available. This
is a conservative heuristic, not a guarantee that every chat phrase is detected.

The result flags the need for an `EchoMuseExpandTools` handler when narrowing;
the preview does not execute that production conversation lifecycle. The
add-on retains its existing expansion handler and turn isolation, without
rerunning selection halfway through tool results. Custom provider schemas,
direct callables and explicit tool choices retain their full original tools.
Gemini Live, flow graphs and HA Assist's separate text HTTP route are unchanged.
Reported timing covers selection only, not HTTP, cloud inference or speech.

Nothing is sent to HA or a cloud service, executed, persisted, or logged by
the server. Requests are processed in memory. Opening index.html directly
does not run Python; use the local server.

Tests:

```powershell
python -m pytest tools/tool_selector_preview/test_preview.py -q
```
