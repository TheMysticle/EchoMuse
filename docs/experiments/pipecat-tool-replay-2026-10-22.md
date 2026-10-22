# Gemini Live tool-result replay review, 2026-10-22

## Evidence and cause

Reviewed the supplied device log (`what the hell is happening here.txt`) and
Pipecat export ending `2026-10-22T13-45-56.820Z.log`. Times below refer to log
entries, not the UTC export filename.

- 15:42:48: Gemini disconnects and reconnects.
- 15:44:33: device session 35 opens on a wake word.
- 15:44:34.646: the user transcript is `Stop.`.
- 15:44:35.301: Gemini calls EchoMuseStop. Its result confirms one stopped timer.
- 15:44:35.364–371: Pipecat sends 13 historical tool results plus the new stop
  result. Some old results contain timestamps from the previous night. These
  are outbound `_tool_result` messages, not merely debug dumps of history.
- 15:44:37: the device session closes after the response.
- 15:44:38.159 onward: new vacuum, timer and other tool calls continue with no
  further wake until 15:45:58. The export ends before that wake. Between
  15:44:35 and 15:45:51 it records 69 function calls: the intended stop and
  68 subsequent calls, including 35 timer-creation attempts. Some timer
  results explicitly acknowledge persistent device changes. These are timers
  in this burst; the log does not justify calling every creation an alarm.

An earlier instance occurs at 15:21:45: a floor-lamp operation resends five
historical results, followed at 15:21:47 by a ten-minute timer creation.

Pipecat 1.4.0's `_disconnect` clears `_completed_tool_calls` and tool-name
mapping but retains `_context`. `_process_completed_function_calls` scans
the entire context on updates and sends every finished result whose ID is
absent from the cleared set. Consequently the first ordinary tool completion
after reconnect replays old results as new live responses. The source and
outbound log directly establish replay; its timing strongly implicates it
as the trigger for the ensuing model/tool loop. The model's internal reason
for selecting particular unrelated tools cannot be established from logs.

Reference: [Pipecat Gemini Live implementation](https://github.com/pipecat-ai/pipecat/blob/v1.4.0/src/pipecat/services/google/gemini_live/llm.py).

## Review of the proposed fix

The proposed fix is correct for this replay path. Before the existing
connection-task wrapper starts receiving messages, it marks completed regular
tool results already in retained context as sent. Conversation history is
preserved for restored or newly seeded sessions. IN_PROGRESS results remain
eligible to be sent after completion, and new calls added after reconnect
are not suppressed. The existing wrapper is already installed by the packager;
no additional generated-main patch or controller change is necessary.

The original added test only checked the set at connection startup. Added
runtime tests use Pipecat's actual `_disconnect` and completed-result scan
with real LLMContext objects and mocked network/tool delivery. They reproduce
the old-result replay without the fix and show that the fix sends only the
new result, permits a pending call to finish once, and prevents replay after
another reconnect. No cloud requests or physical device actions are made.
The incident comment was corrected from approximately 170 calls to the
observed count and no longer claims each stale result provably caused one call.

Validation: all 58 adapter/reconnect tests pass against Pipecat 1.4.0;
`git diff --check` passes.

This targets regular synchronous tool results used by this integration; it
is not a general authorization filter for model-generated tool calls or an
async-tool transport redesign. It also does not erase an already contaminated
live conversation or cancel schedules created earlier.

## Rollout

Only the Pipecat Assist EchoMuse add-on requires rebuilding. Reinstall/restart
it to start a fresh service and conversation with the fix. The controller and
device firmware are unchanged. Review unwanted persistent timers separately;
this patch intentionally does not delete the user's schedules.

After rollout, verify that a reconnect followed by a normal command sends only
that command's fresh tool results, not old call IDs. Unit/runtime verification
does not substitute for observing the deployed service through a reconnect.
