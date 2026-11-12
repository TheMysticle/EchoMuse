"""The wake word's tail alone must not end a turn; a command said straight on must end as before.

Replays the shape of the 2026-11-12 field turns against Pipecat's own stop
strategy: the held pre-roll (wake word tail, then a pause) arriving at once,
and a command run on from the wake word.
"""
import asyncio
import sys
from pathlib import Path

from pipecat.frames.frames import InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import echomuse  # noqa: E402

RATE = 16000
START, STOP = 0.12, 0.3


async def _strategy():
    manager = TaskManager()
    manager.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
    strategy = echomuse.wake_tail_stop_strategy(SpeechTimeoutUserTurnStopStrategy)(
        user_speech_timeout=0.2, wait_for_transcript=False)
    await strategy.setup(manager)
    stops = []

    async def stopped(_strategy, *args, **kwargs):
        stops.append(asyncio.get_running_loop().time())

    strategy.add_event_handler("on_user_turn_stopped", stopped)
    return strategy, stops


async def _audio(strategy, seconds):
    frame_s = 0.08
    for _ in range(round(seconds / frame_s)):
        await strategy.process_frame(InputAudioRawFrame(
            audio=bytes(int(RATE * frame_s) * 2), sample_rate=RATE, num_channels=1))


async def _start(strategy):
    await strategy.process_frame(VADUserStartedSpeakingFrame(start_secs=START))


async def _stop(strategy):
    await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP))


def test_a_pause_after_the_wake_word_waits_for_the_command():
    async def go():
        strategy, stops = await _strategy()
        loop = asyncio.get_running_loop()
        # Burst: 0.26s lead, the tail's start, ~0.2s of tail, the 0.3s pause.
        await _audio(strategy, 0.24)
        await _start(strategy)
        await _audio(strategy, 0.16)
        await _audio(strategy, STOP)
        await _stop(strategy)
        t0 = loop.time()
        await asyncio.sleep(0.6)
        assert stops == [], "the tail ended the turn before the command"
        # The command, then its own end.
        await _start(strategy)
        await _audio(strategy, 1.0)
        await _stop(strategy)
        await asyncio.sleep(0.4)
        assert len(stops) == 1 and stops[0] - t0 > 0.6
        await strategy.cleanup()
    asyncio.run(go())


def test_a_command_run_on_from_the_wake_word_ends_as_before():
    async def go():
        strategy, stops = await _strategy()
        loop = asyncio.get_running_loop()
        await _audio(strategy, 0.24)
        await _start(strategy)
        await _audio(strategy, 1.1)
        await _stop(strategy)
        t0 = loop.time()
        await asyncio.sleep(0.4)
        assert len(stops) == 1 and stops[0] - t0 < 0.35
        await strategy.cleanup()
    asyncio.run(go())


def test_the_tail_alone_still_ends_the_turn_in_the_end():
    async def go():
        strategy, stops = await _strategy()
        await _audio(strategy, 0.24)
        await _start(strategy)
        await _audio(strategy, 0.16 + STOP)
        await _stop(strategy)
        await asyncio.sleep(echomuse.WAKE_PAUSE_S + 0.3)
        assert len(stops) == 1
        await strategy.cleanup()
    asyncio.run(go())


def test_only_the_first_segment_of_a_session_is_a_tail():
    async def go():
        strategy, stops = await _strategy()
        loop = asyncio.get_running_loop()
        await _audio(strategy, 0.24)
        await _start(strategy)
        await _audio(strategy, 0.16 + STOP)
        await _stop(strategy)            # the tail: waits
        await _start(strategy)
        await _audio(strategy, 0.2 + STOP)
        await _stop(strategy)            # a short second word ("yes") ends promptly
        t0 = loop.time()
        await asyncio.sleep(0.4)
        assert len(stops) == 1 and stops[0] - t0 < 0.35
        await strategy.cleanup()
    asyncio.run(go())
