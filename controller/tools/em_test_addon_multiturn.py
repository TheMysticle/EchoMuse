"""Direct test against the addon's own /api/assist/esphome endpoint (bypasses
the controller entirely) -- sends two utterances' raw PCM back-to-back on ONE
connection, exactly what triggers the sherpa-onnx reconnect-per-utterance bug,
without needing to replicate the controller's follow-up-arming timing.
"""
import argparse
import asyncio
import json
import os
import time
import wave

import websockets

FRAME_SAMPLES = 1280  # 80ms at 16kHz mono s16


def load_pcm16(path):
    with wave.open(path, "rb") as w:
        return w.readframes(w.getnframes())


async def stream_utterance(ws, path, label, t0):
    pcm = load_pcm16(path)
    frame_bytes = FRAME_SAMPLES * 2
    print(f"[{label}] streaming at +{time.perf_counter()-t0:.2f}s")
    for i in range(0, len(pcm), frame_bytes):
        await ws.send(pcm[i:i + frame_bytes])
        await asyncio.sleep(0.08)
    print(f"[{label}] done streaming at +{time.perf_counter()-t0:.2f}s (no explicit end marker -- VAD decides)")


async def wait_for_reply(ws, label, t0, timeout=20):
    got_first = False
    try:
        async with asyncio.timeout(timeout):
            async for raw in ws:
                if isinstance(raw, bytes) and raw:
                    if not got_first:
                        got_first = True
                        print(f"[{label}] first reply audio at +{time.perf_counter()-t0:.2f}s")
                elif isinstance(raw, str):
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    phase = msg.get("phase")
                    if phase:
                        print(f"  [{label} +{time.perf_counter()-t0:.2f}s] phase={phase} {msg}")
                        if phase == "idle" and got_first:
                            return True
                        if phase == "listening" and got_first:
                            # Reply finished, back to listening -- good enough signal.
                            return True
    except TimeoutError:
        print(f"[{label}] TIMEOUT ({'got first byte, no terminal phase' if got_first else 'got NOTHING'})")
        return got_first
    return got_first


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ADDON_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ADDON_PORT", "7860")))
    parser.add_argument("--token", default=os.environ.get("ADDON_TOKEN", ""),
                         help="the addon's satellite_shared_secret (Runtime tab)")
    parser.add_argument("--flow-id", default=os.environ.get("ADDON_FLOW_ID", ""))
    parser.add_argument("utterance1")
    parser.add_argument("utterance2")
    args = parser.parse_args()

    query = f"token={args.token}&client_id=direct-multiturn"
    if args.flow_id:
        query += f"&flow_id={args.flow_id}"
    url = f"ws://{args.host}:{args.port}/api/assist/esphome?{query}"
    ws = await websockets.connect(url, max_size=2**22)
    hello = json.loads(await ws.recv())
    print("hello:", hello.get("type"), hello.get("protocol"))

    t0 = time.perf_counter()
    await stream_utterance(ws, args.utterance1, "turn1", t0)
    ok1 = await wait_for_reply(ws, "turn1", t0)

    await asyncio.sleep(0.5)
    await stream_utterance(ws, args.utterance2, "turn2", t0)
    ok2 = await wait_for_reply(ws, "turn2", t0)

    await ws.close()
    print(f"\nRESULT: turn1={'OK' if ok1 else 'FAILED'} turn2={'OK' if ok2 else 'FAILED'}")


if __name__ == "__main__":
    asyncio.run(main())
