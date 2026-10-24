package speaker

import "time"

// ringFrames and loopPeriodFrames mirror alsaBufferFrames and periodSize, which sit
// behind the device build tag; pcm_speaker.go asserts they agree.
const (
	ringFrames       = 8192
	loopPeriodFrames = 2048
)

// shallowRingFrames is how much the ring may hold ahead of the DAC when the
// source reports a low-latency stream. The blocking write otherwise keeps the
// ring full (171ms), and everything in it is lead the stream must have
// arrived with: lead must exceed the ring's occupancy plus the period being
// built. With 512-frame pulls this is 48ms + 10.67ms = 58.67ms,
// before network jitter and scheduling overhead. Voice still uses 2048 frames.
//
// The earlier 64ms floor was inferred from Auddrv_DL1_Interrupt_Handler
// messages. The vendor handler treats equal DMA pointers as a full ring wrap,
// so status reads near an IRQ can produce false underflow messages. Validate
// with mtk_pcm_I2S0dl1_pointer underflow / ALSA XRUN, player underruns and audio,
// not the IRQ message count alone. See docs/experiments/sendspin-latency-2026-10-24.md.
const shallowRingFrames = 48 * 48

// maxRingWait bounds one wait. It has to exceed a period (43ms) or the ring
// never drains: the first cap, 40ms, left a Radar holding 125ms.
const maxRingWait = 140 * time.Millisecond

// ringWait is how long to hold off building the next period so the ring
// drains to target from d queued frames.
func ringWait(d, target int) time.Duration {
	if d <= target {
		return 0
	}
	return min(time.Duration(d-target)*time.Second/48000, maxRingWait)
}

const lowLatencyPeriodFrames = 512

// sourcePeriodBytes is the length of the period the pull source is asked to
// build, in stereo S16 bytes. A quarter of the loop's when a low-latency stream has
// the speaker to itself; a voice period is a whole one and so is everything
// mixed against it. Everything but this choice is length-agnostic.
func sourcePeriodBytes(voiceReady, lowLatency bool) int {
	if lowLatency && !voiceReady {
		return lowLatencyPeriodFrames * 4
	}
	return loopPeriodFrames * 4
}
