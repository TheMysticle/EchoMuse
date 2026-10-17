#!/usr/bin/env python3
"""Package a pinned upstream Pipecat add-on with the EchoMuse session adapter.

Usage: python tools/pipecat_echomuse/package.py --source /path/to/upstream
The adapter is kept here; upstream source is not vendored into the repository.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess

PIN = "a538d1d1bacc8609061719f0b05c1622147ccb4b"
ROOT = Path(__file__).resolve().parents[2]


def adapt(text):
    def replace(old, new):
        nonlocal text
        if text.count(old) != 1:
            raise ValueError("Upstream adapter anchor changed: " + old[:70])
        text = text.replace(old, new)
    replace('from __future__ import annotations', 'from __future__ import annotations\n\nfrom app import echomuse')
    replace('    await websocket.send_text(_va_pipecat_protocol(config).hello())', '''    hello = json.loads(_va_pipecat_protocol(config).hello())
    hello["echomuse"] = {"scheduler": bool(session_options.get("scheduler", {}).get("schedules")), "stop": bool(session_options.get("scheduler")), "barge_in": bool(session_options.get("barge_in"))}
    await websocket.send_text(json.dumps(hello))''')
    replace('    session_id = str(uuid.uuid4())\n    await websocket.accept()', '''    session_options = echomuse.options(websocket.headers, authenticated=bool(secret))
    session_id = str(uuid.uuid4())
    await websocket.accept()''')
    replace('            "transport": "va-pipecat",', '            "transport": "va-pipecat",\n            "echomuse": session_options,')
    replace('    mcp_servers = config.enabled_mcp_servers(mcp_token) if flow.mcp_enabled else []\n    if mcp_servers:\n        bridge = CombinedMCPBridge(mcp_servers, flow.mcp_tool_allowlist)', '''    mcp_servers = config.enabled_mcp_servers(mcp_token) if flow.mcp_enabled else []
    mcp_servers, session_allowlist = echomuse.mcp_options(session_body.get("echomuse", {}), mcp_servers, flow.mcp_tool_allowlist)
    if mcp_servers:
        bridge = CombinedMCPBridge(mcp_servers, session_allowlist)''')
    replace('    session_body = _runner_body(runner_args)\n    is_esphome_satellite', '''    session_body = _runner_body(runner_args)
    if session_body.get("echomuse"):
        flow = flow.model_copy(deep=True)
        area = session_body["echomuse"].get("area")
        area_hint = f" The speaker is in the {area} area; treat that as the room for bare media stop requests." if area else " Use the speaker's current Home Assistant area for bare media stop requests."
        lang_hint = f" Always reply in the language with code {flow.language or 'en'}, the satellite's configured language, unless the user clearly speaks a different language to you. Accented or very short speech is not a reason to switch, and names or titles in another language in tool results or the home setup do not change your reply language."
        flow.instructions += "\\nFor EchoMuse: end normal replies without a follow-up question. If a required detail is missing, call EchoMuseRequestContinuation before asking one short clarification; never use it for optional follow-up. Use EchoMuse scheduler tools for timers, alarms and reminders; never claim a save without device acknowledgment. For a bare stop, call EchoMuseStop once. It stops one thing: what is ringing first, otherwise this speaker's music, otherwise a running timer, and reports `stopped` and `left_running`. Report only what `stopped` lists, explain any reported errors, and never stop, pause or cancel anything listed in `left_running`: that is deliberate, and a second stop does the next thing. Only when `stopped` is empty and there are no errors, use homeassistant__GetLiveContext or equivalent to find a playing media_player in the speaker's area and call intent__HassMediaPause for only those active room players; if a Home Assistant timer in that area is active and a timer-cancel tool is available, cancel it. Never cancel future alarms or reminders. For a bare snooze, inspect EchoMuseList and snooze only the active ringing alert. Explicit room or device targets take precedence over this bare-stop behavior. For a bare play, resume or continue with no title, resume this speaker's own media player (the Home Assistant media unpause tool) and never call play_media. When asked for specific music, pass the title or artist as plain text to play_media; never write a Spotify URI or playlist ID yourself, only reuse one that a search returned. play_media with a title starts playback at once and its success means the music is playing, so never search first, verify afterwards or call it again for the same request. Never search for a generic word such as music to find something to resume; a bare play or resume is the media unpause tool. Ask about an unknown timezone before creating a calendar alarm or reminder." + area_hint + lang_hint
    is_esphome_satellite''')
    replace('    local_tool_schemas = [schema for schema in [_web_search_tool_schema(config, flow)] if schema]\n    tools_schema = _merge_tools_schema(mcp_tools_schema, local_tool_schemas)\n    context_for_memory = None', '''    local_tool_schemas = [schema for schema in [_web_search_tool_schema(config, flow)] if schema]
    if session_body.get("echomuse") and session_body["transport"] == "va-pipecat":
        params = getattr(transport.input(), "_params", None)
        serializer = getattr(params, "serializer", None)
        protocol = getattr(serializer, "protocol", None)
        if protocol is not None:
            local_tool_schemas.append(echomuse.continuation_schema(protocol))
    tools_schema = _merge_tools_schema(mcp_tools_schema, local_tool_schemas)
    context_for_memory = None''')
    replace('local_turn_detection=bool(_enabled_step(flow, "vad")),', 'local_turn_detection=bool(_enabled_step(flow, "vad") or session_body.get("echomuse", {}).get("barge_in")),')
    replace('if provider_kind == "gemini" and _enabled_step(flow, "vad"):', 'if provider_kind == "gemini" and (_enabled_step(flow, "vad") or session_body.get("echomuse", {}).get("barge_in")):')
    replace('vad_analyzer=_composed_vad_analyzer(flow),', 'vad_analyzer=echomuse.vad_analyzer(session_body.get("echomuse", {}), lambda: _composed_vad_analyzer(flow)),')
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, default=ROOT/'dist/pipecat-assist-echomuse-dev')
    args = parser.parse_args()
    sha = subprocess.check_output(['git', '-C', str(args.source), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != PIN:
        parser.error('Expected pinned upstream commit ' + PIN)
    source = args.source/'addons/pipecat_assist'
    # This command only replaces its own generated local add-on folder.
    if args.out.exists():
        shutil.rmtree(args.out)
    shutil.copytree(source, args.out, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.env'))
    shutil.copyfile(args.source/'LICENSE', args.out/'LICENSE')
    path = args.out/'app/main.py'
    path.write_text(adapt(path.read_text()))
    path = args.out/'app/va_pipecat_protocol.py'
    protocol = path.read_text()
    old = 'phase_extra = {"terminal": True} if terminal else {"follow_up": True}'
    assert protocol.count(old) == 1
    protocol = protocol.replace('    tool_start_pending: bool = False', '    tool_start_pending: bool = False\n    continuation_requested: bool = False')
    protocol = protocol.replace('            self.tool_start_pending = False\n            return "wake"', '            self.tool_start_pending = False\n            self.continuation_requested = False\n            return "wake"')
    protocol = protocol.replace('            self.tool_start_pending = False\n            return "stop"', '            self.tool_start_pending = False\n            self.continuation_requested = False\n            return "stop"')
    protocol = protocol.replace(old, 'phase_extra = {"terminal": True} if terminal else {"follow_up": True, "continuation_question": self.continuation_requested}')
    protocol = protocol.replace('            self.active = not terminal\n            self.stop_requested = False', '            continuation_question = self.continuation_requested\n            self.continuation_requested = False\n            self.active = not terminal\n            self.stop_requested = False')
    protocol = protocol.replace('phase_extra = {"terminal": True} if terminal else {"follow_up": True, "continuation_question": self.continuation_requested}', 'phase_extra = {"terminal": True} if terminal else {"follow_up": True, "continuation_question": continuation_question}')
    protocol = protocol.replace('self.phase_message("listening", follow_up=True)', 'self.phase_message("listening", follow_up=True, continuation_question=continuation_question)')
    path.write_text(protocol)
    shutil.copyfile(Path(__file__).with_name('echomuse.py'), args.out/'app/echomuse.py')
    version = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], text=True).strip()
    path = args.out/'config.yaml'
    config = path.read_text()
    config = re.sub(r'^image:.*\n', '', config, flags=re.M)
    config = re.sub(r'^name:.*$', 'name: Pipecat Assist (EchoMuse dev)', config, flags=re.M)
    config = re.sub(r'^slug:.*$', 'slug: pipecat_assist_echomuse', config, flags=re.M)
    config = re.sub(r'^version:.*$', f'version: "0.1.80-em-{version}"', config, flags=re.M)
    path.write_text(config)
    print(args.out)


if __name__ == '__main__':
    main()
