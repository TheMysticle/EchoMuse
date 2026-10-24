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
// built, so 64ms plus the 43ms period needs ~107ms.
//
// Measured on a Radar, 2026-10-24, by the kernel's own DL1 underflow count
// (the hardware ring running dry, a click each): 50ms gave 181 in four
// minutes, 58ms about one every 20s, 64ms none over two four-minute runs.
// Lower is not available on this loop; a stall of more than the target
// empties the ring. Do not go below 64 without that count.
const shallowRingFrames = 64 * 48

// maxRingWait bounds one wait. It has to exceed a period (43ms) or the ring
// never drains: the first cap, 40ms, left a Radar holding 125ms.
const maxRingWait = 120 * time.Millisecond

// ringWait is how long to hold off building the next period so the ring
// drains to target from d queued frames.
func ringWait(d, target int) time.Duration {
	if d <= target {
		return 0
	}
	return min(time.Duration(d-target)*time.Second/48000, maxRingWait)
}
