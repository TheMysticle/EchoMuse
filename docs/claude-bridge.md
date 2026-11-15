# Claude Code by voice

Ask a Pipecat Echo to build something on your PC, for example "ask Claude to design a 3D-printable tamper for my Dolce Gusto capsules". Claude Code works on it in its own folder. When it needs something from you, the Echo asks you out loud. When it finishes, the Echo tells you.

It runs your own `claude` CLI and its login, so a Pro or Max plan is enough. No API key is involved.

## Set it up

1. **On the PC**, with Claude Code installed and signed in (`claude`, then `/login`):

   ```bash
   python3 ~/EchoMuse/tools/claude_bridge/bridge.py
   ```

   On first start it writes `~/.config/echomuse-claude-bridge/config.json` with a token. Print the token with `--show-token`. To keep it running, install `tools/claude_bridge/echomuse-claude-bridge.service` as a systemd user unit (instructions are in the file).

2. **In the controller add-on's options**, set *Claude Code bridge URL* (for example `http://<your PC address>:8790`) and *Claude Code bridge token*, then restart the add-on. If you run the container instead, set `CLAUDE_BRIDGE_URL` and `CLAUDE_BRIDGE_TOKEN`.

3. **Rebuild the Pipecat add-on** from this branch. It needs the `prompt` message, which is what lets an Echo speak first. Pipecat Echoes then have four new tools: `ClaudeStart`, `ClaudeStatus`, `ClaudeReply` and `ClaudeCancel`.

Speaking first works with a Gemini Live flow. A composed flow has the tools but cannot start a conversation, so ask "how is my PC task going" instead.

## Using it

- **Start a task:** "Ask Claude to build …". The Echo repeats back what it understood and waits for your yes before starting.
- **Questions:** when Claude needs you, the Echo that started the task says the question and listens for your answer. If you miss it or say "not now", ask "what does my PC task need?" later.
- **Done:** the Echo gives a two- or three-sentence summary. Each task's files are in `~/voice-projects/<date>-<name>/`, with a `README.md`.
- **More changes:** "tell the coffee tamper task to make the handle longer". This works after it has finished. While it is running, the instruction is passed on when the current run ends.
- **Stop it:** "cancel my PC task".

If the Echo is busy, muted or has its wake word off, the update waits, retried every 15 seconds for 30 minutes. After that the question stays in the task's status.

## Settings (`config.json`)

| Key | Default | |
|---|---|---|
| `model` | `claude-sonnet-5-5` | Any model name `claude --model` accepts |
| `effort` | `medium` | `claude --effort`: `low`, `medium`, `high`, `xhigh`, `max`, or `""` for the CLI's own default |
| `projects_dir` | `~/voice-projects` | One subfolder per task |
| `port`, `host` | `8790`, `0.0.0.0` | |
| `max_running` | `2` | Tasks running at once |
| `sandbox` | `auto` | Claude Code's shell sandbox, used when `bwrap` and `socat` are installed |
| `sandbox_domains` | PyPI | Hosts that sandboxed shell commands may reach; web search and fetch are not limited by it |
| `allowed_tools` | web search/fetch, `openscad` | Tools allowed beyond reading and editing inside the task folder |
| `max_turn_seconds` | `3600` | A single run is stopped after this long |
| `printer` | none | `{"host": "<printer IP>"}` turns printing on; see below |
| `printer_poll_s` | `30` | How often a running print is checked |

## What it can and cannot do

Nobody is at the PC to approve anything. Claude Code runs with `acceptEdits`, so it can:

- read and write files inside the task's folder;
- run simple file commands there;
- use the tools in `allowed_tools`.

Anything else is refused rather than asked.

- **With the sandbox** (`sudo pacman -S socat` alongside bubblewrap), it can also run any shell command, confined to the folder, with network access only to `sandbox_domains` (PyPI by default). That is what lets it render and check a model (`openscad`, Python).
- **Without the sandbox**, it writes the files and the README tells you what to run.

It is not given your other MCP servers, and an `ANTHROPIC_API_KEY` in the environment is removed, so the plan is used rather than API billing.

The bridge is reachable by anything on your LAN that has its token. Keep the token private and don't expose the port to the internet. The controller connects out to the bridge, so nothing new can reach the controller.

## Printing (Elegoo Centauri Carbon)

Claude can put a print on the printer. It never starts one: you do, by voice.

1. When the model is ready, Claude calls `send_to_printer`. The bridge slices it with ElegooSlicer's own Centauri Carbon profiles and uploads the G-code. Claude asks which material first, offering your usual one, and picks the quality.
2. The Echo tells you it is on the printer, with the time and material.
3. Clear the bed and load the filament, then say "start the print". The bridge starts it only if the printer reports idle, stopped or finished. Otherwise it says what the printer is doing.
4. "How is the print going", "pause the print", "resume the print" and "stop the print" work while it runs.
5. The Echo says when the print has finished, failed, or was stopped at the printer. A stop you asked for is not announced back to you.

Setup on the PC:

```bash
python3 -m venv ~/.local/share/echomuse-claude-bridge/venv
~/.local/share/echomuse-claude-bridge/venv/bin/pip install pycentauri
# ElegooSlicer (AUR: elegoo-slicer-bin) provides `elegoo-slicer` and the profiles
```

Add `"printer": {"host": "<printer IP>"}` to `config.json` and restart the bridge. The log says `Printer <IP>: ready` or names what is missing. Optional `printer` keys: `material` (what you usually have loaded, default `PLA`; Claude asks which material before slicing and offers this one), `python`, `slicer`, `profiles` (default `/opt/elegoo-slicer/resources/profiles/Elegoo`).

The slicer runs with its own data folder under `<projects_dir>/.bridge-cache/slicer`, so your ElegooSlicer settings are not touched. The printer's port takes few connections, so the bridge talks to it one command at a time. Print files are named after the task (`<task id>.gcode`) and the sliced copy stays in the task's `print/` folder.
