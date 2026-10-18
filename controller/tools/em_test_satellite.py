"""Minimal device-controller-protocol satellite emulator for a one-off latency
test. Not part of the shipped tooling -- speaks just enough of /control and
/data (docs/device-controller-interface.md) to register, trigger a button
voice turn, stream one WAV as mic PCM at realtime pace, and capture when
speaker audio starts/stops.
"""
import asyncio
import json
import sys
import time
import wave

import websockets

CONTROLLER_HOST = "192.168.1.155"
CONTROL_URL = f"ws://{CONTROLLER_HOST}:8767/control"
DATA_URL = f"ws://{CONTROLLER_HOST}:8767/data"
DEVICE_ID = "EMTESTSAT01"
FRAME_BYTES = 2560  # 80ms of 16kHz mono s16, matching the real wire contract


def load_pcm16_mono16k(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000, f"{path} is not 16kHz"
        assert w.getnchannels() == 1, f"{path} is not mono"
        assert w.getsampwidth() == 2, f"{path} is not s16"
        return w.readframes(w.getnframes())


async def run_one(path, label):
    control = await websockets.connect(CONTROL_URL, max_size=2**22)
    await control.send(json.dumps({
        "type": "register",
        "device_id": DEVICE_ID,
        "version": "test-emulator",
        "capabilities": ["mic", "speaker"],
        "board": "emulator",
    }))

    ack = json.loads(await control.recv())
    assert ack.get("type") == "ack", f"expected ack, got {ack}"

    data = await websockets.connect(DATA_URL, max_size=2**22)
    # The data plane's FIRST message must be a text "identify" frame
    # (em_controller.handle_data): {"type":"identify","device_id":...}.
    # After that, admission is by address/scheme matching the live control
    # connection (em_linkauth.follows_control).
    await data.send(json.dumps({"type": "identify", "device_id": DEVICE_ID}))

    t = {}

    async def control_reader():
        async for raw in control:
            msg = json.loads(raw) if isinstance(raw, str) else None
            if not msg:
                continue
            mtype = msg.get("type")
            if mtype == "mic_start":
                t.setdefault("mic_start", time.perf_counter())
            elif mtype == "mic_stop":
                t.setdefault("mic_stop", time.perf_counter())
            elif mtype == "leds":
                pass
            else:
                print(f"  [control] {mtype} {msg}")

    async def data_reader():
        async for raw in data:
            if isinstance(raw, bytes) and raw:
                code = raw[0]
                print(f"  [data] frame type=0x{code:02x} len={len(raw)} at +{time.perf_counter()-t0:.3f}s")
                if code == 0x02 and "speaker_first" not in t:
                    t["speaker_first"] = time.perf_counter()
                # A bare 0x03 with no preceding 0x02 is the workflow resetting
                # the speaker plane on connect/apply, not a reply ending.
                if code == 0x03 and "speaker_first" in t:
                    t["speaker_eos"] = time.perf_counter()
                    return
            else:
                print(f"  [data] non-bytes message: {raw!r}")

    creader = asyncio.create_task(control_reader())
    dreader = asyncio.create_task(data_reader())

    pcm = load_pcm16_mono16k(path)

    t0 = time.perf_counter()
    print(f"[{label}] button press -> voice turn")
    await control.send(json.dumps({"type": "button", "clickType": 138, "down": True}))
    await control.send(json.dumps({"type": "button", "clickType": 138, "down": False}))

    # Stream mic PCM at realtime pace, 80ms frames: wire header is
    # [0x01][seq_hi][seq_lo][pcm...] (MIC_HEADER_LEN=3 in em_controller.py).
    seq = 0

    def mic_header():
        nonlocal seq
        seq = (seq + 1) & 0xFFFF
        return bytes([0x01, (seq >> 8) & 0xFF, seq & 0xFF])

    for i in range(0, len(pcm), FRAME_BYTES):
        chunk = pcm[i:i + FRAME_BYTES]
        await data.send(mic_header() + chunk)
        await asyncio.sleep(0.08)
    # VAD-end sentinel: a normal mic header followed by one 0x04 byte.
    await data.send(mic_header() + bytes([0x04]))
    t["mic_end_sent"] = time.perf_counter()
    print(f"[{label}] mic stream + VAD-end sent at +{t['mic_end_sent']-t0:.2f}s")

    try:
        await asyncio.wait_for(dreader, timeout=45)
    except asyncio.TimeoutError:
        print(f"[{label}] TIMEOUT waiting for speaker audio")

    creader.cancel()
    await control.close()
    await data.close()

    result = {"label": label, "t0": t0}
    for key in ("mic_start", "mic_stop", "speaker_first", "speaker_eos", "mic_end_sent"):
        if key in t:
            result[key] = round((t[key] - t0) * 1000)
    print(f"[{label}] result: {result}")
    return result


async def main():
    results = []
    for path, label in [
        (r"C:\Users\olikr\Music\test speech\turn off the bedroom lights.wav", "turn_off_bedroom_lights"),
        (r"C:\Users\olikr\Music\test speech\what's the indoor temperature.wav", "indoor_temperature"),
        (r"C:\Users\olikr\Music\test speech\Do I have any rmeinders.wav", "reminders"),
    ]:
        r = await run_one(path, label)
        results.append(r)
        await asyncio.sleep(2)
    print(json.dumps(results, indent=2))
    with open(r"C:\Users\olikr\dev\echomuse-latency-sim\results_addon_realcontroller.json", "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
