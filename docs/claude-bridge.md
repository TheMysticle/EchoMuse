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
| `projects_dir` | `~/voice-projects` | One subfolder per task |
| `port`, `host` | `8790`, `0.0.0.0` | |
| `max_running` | `2` | Tasks running at once |
| `sandbox` | `auto` | Claude Code's shell sandbox, used when `bwrap` and `socat` are installed |
| `allowed_tools` | web search/fetch, `openscad` | Tools allowed beyond reading and editing inside the task folder |
| `max_turn_seconds` | `3600` | A single run is stopped after this long |

## What it can and cannot do

Nobody is at the PC to approve anything. Claude Code runs with `acceptEdits`, so it can:

- read and write files inside the task's folder;
- run simple file commands there;
- use the tools in `allowed_tools`.

Anything else is refused rather than asked.

- **With the sandbox** (`sudo pacman -S socat` alongside bubblewrap), it can also run any shell command, confined to the folder with no network. That is what lets it render and check a model (`openscad`, Python).
- **Without the sandbox**, it writes the files and the README tells you what to run.

It is not given your other MCP servers, and an `ANTHROPIC_API_KEY` in the environment is removed, so the plan is used rather than API billing.

The bridge is reachable by anything on your LAN that has its token. Keep the token private and don't expose the port to the internet. The controller connects out to the bridge, so nothing new can reach the controller.
