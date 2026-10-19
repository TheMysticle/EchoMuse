# Composed audio tail reproduction

The user's log reports `TTSStoppedFrame`, bot stopped speaking, then
`InterruptionWorkerFrame` shortly afterwards. The controller sends `stop` when
the normal completed reply ends its session. The add-on deserializer maps that
message to an interruption worker frame. This explains those log entries
without attributing them to VAD or proving an audible cutoff.

A deterministic reproduction using the **unmodified Pipecat 1.4.0** WebSocket
output handler and real `BaseOutputTransport.MediaSender` demonstrated a
separate PCM loss before any interruption:

- Input: 2,880 bytes of 24 kHz mono PCM16 (60 ms).
- Output to the captured transport writer: 1,920 bytes (40 ms).
- Lost: the last 960 bytes (20 ms).
- Trigger: `TTSStoppedFrame` causes `_bot_stopped_speaking` to clear the partial
  packet remaining in `_audio_buffer`.

The reproduction captures output writes, not physical Echo playback. It does
not establish that this is the only cause of the user's perceived cutoff.

The composed ESPHome output adapter now completes the partial packet with
silence before queueing the TTS stop. Original PCM bytes remain unchanged and
are written before bot-stopped notification. It uses the sender's actual output
rate and chunk size, after resampling. The adapter accesses pinned Pipecat 1.4
media-sender fields and is covered by runtime tests. It is installed per
connection in the composed branch only; Gemini Live output is unchanged.

Regression tests cover exact-packet and partial-packet lengths, responses
shorter than one packet, retained PCM content, notification ordering, isolated
connection replacement, and real interruption discarding a pending sub-packet
instead of resurrecting cancelled speech. No acoustic replay was performed.

The independent chat improvement uses the shared selector's `chat` result to
skip HA conversation before LLM inference. Pending local schedule clarification
retains priority. No LLM classifier, sleep, or extra network request is added.

Reproduce and compare:

```powershell
python -m pytest tools/pipecat_echomuse/tests/test_audio_tail_runtime.py -q -s
```
