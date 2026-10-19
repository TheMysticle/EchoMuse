# Tool selector preview

From the repository root, run:

```powershell
python tools/tool_selector_preview/serve.py
```

Open **http://127.0.0.1:8765**. No Python packages, keys, HA connection or model
are required. `--port 8766` selects another local port.

The HTML sends sentences to a loopback-only Python server, which imports
`tools/pipecat_echomuse/tool_selector.py` directly. There is no JavaScript
reimplementation of the matching rules. This pure module is intended for the
future add-on integration; it is **not enabled in the existing pipeline**.

The editable sample catalogue is illustrative, not a snapshot of a real HA
installation. Paste JSON tool names, MCP tool definitions/list_tools results,
or OpenAI function tool definitions to test a different catalogue. Names are
matched exactly after removing MCP namespaces, without guessing from tool
descriptions. Unknown custom tools remain exposed rather than disappearing.
Generic HA tools can belong to multiple categories. Context/continuation
tools stay available. Selection only removes tools from the supplied list;
it never discovers new tools or changes configured allowlists.

English whole-clause rules cover lighting, weather, music, climate, covers,
and speaker schedules. Multiple recognized clauses produce the union of
categories. Unknown clauses, conditional dependencies, incomplete commands,
and ambiguous follow-ups keep the full catalogue. This is intentionally
conservative. It selects capabilities, not rooms, arguments or actions.

The result flags the need for an `EchoMuseExpandTools` handler when narrowing;
the preview cannot implement that production conversation lifecycle. Future
integration must retain the existing runtime expansion handler and turn
isolation. It must not run the selector again halfway through tool results.
Reported timing covers selection only, not HTTP, cloud inference or speech.

Nothing is sent to HA or a cloud service, executed, persisted, or logged by
the server. Requests are processed in memory. Opening index.html directly
does not run Python; use the local server.

Tests:

```powershell
python -m pytest tools/tool_selector_preview/test_preview.py -q
```
