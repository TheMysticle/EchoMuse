// Package earcon plays stock system earcon files through the speaker cue mixer.
//
// The files live on the Echo under /system/local/share/earcon/. We decode them to
// mono float samples at the speaker rate and hand them to PcmSpeaker.PlayCue — the
// same path as the synthesised wake/volume tones.
package earcon

import (
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"

	"github.com/hajimehoshi/go-mp3"
)

const speakerRate = 48000

// Well-known paths on the device (FireOS / emOS system partition).
const (
	OfflineNotConnected = "/system/local/share/earcon/en/system_prompt_error_offline_not_connected_to_service.mp3"
	PrivacyModeOn       = "/system/local/share/earcon/base/state_privacy_mode_on.wav"
	PrivacyModeOff      = "/system/local/share/earcon/base/state_privacy_mode_off.wav"
)

var cache sync.Map // path → []float64

// Load decodes path into mono float64 samples at speakerRate, caching the
// result. Supported: PCM WAV (16-bit) and MP3.
func Load(path string) ([]float64, error) {
	if v, ok := cache.Load(path); ok {
		return v.([]float64), nil
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("earcon %s: %w", path, err)
	}
	defer f.Close()

	var samples []float64
	if strings.HasSuffix(strings.ToLower(path), ".mp3") {
		samples, err = decodeMP3(f)
	} else {
		samples, err = decodeWAV(f)
	}
	if err != nil {
		return nil, fmt.Errorf("earcon %s: %w", path, err)
	}
	cache.Store(path, samples)
	return samples, nil
}

// LoadWithVolume loads the earcon and scales its samples by linear volume gain.
func LoadWithVolume(path string, gain float64) ([]float64, error) {
	raw, err := Load(path)
	if err != nil {
		return nil, err
	}
	if gain >= 0.999 && gain <= 1.001 {
		return raw, nil
	}
	scaled := make([]float64, len(raw))
	for i, v := range raw {
		scaled[i] = v * gain
	}
	return scaled, nil
}

func decodeMP3(r io.Reader) ([]float64, error) {
	d, err := mp3.NewDecoder(r)
	if err != nil {
		return nil, err
	}
	data, err := io.ReadAll(d)
	if err != nil {
		return nil, err
	}
	// go-mp3 produces 16-bit little-endian 2-channel interleaved samples (4 bytes/frame).
	nFrames := len(data) / 4
	mono := make([]float64, nFrames)
	for i := 0; i < nFrames; i++ {
		l := int16(binary.LittleEndian.Uint16(data[i*4:]))
		r := int16(binary.LittleEndian.Uint16(data[i*4+2:]))
		mono[i] = (float64(l) + float64(r)) / 2.0
	}
	return resample(mono, d.SampleRate(), speakerRate), nil
}

func decodeWAV(r io.ReadSeeker) ([]float64, error) {
	var hdr [12]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, err
	}
	if string(hdr[0:4]) != "RIFF" || string(hdr[8:12]) != "WAVE" {
		return nil, fmt.Errorf("not a RIFF/WAVE file")
	}
	var (
		audioFormat   uint16
		numChannels   uint16
		sampleRate    uint32
		bitsPerSample uint16
		data          []byte
	)
	for {
		var chunk [8]byte
		if _, err := io.ReadFull(r, chunk[:]); err != nil {
			return nil, err
		}
		id := string(chunk[0:4])
		size := binary.LittleEndian.Uint32(chunk[4:8])
		body := make([]byte, size)
		if _, err := io.ReadFull(r, body); err != nil {
			return nil, err
		}
		if size%2 == 1 {
			var pad [1]byte
			_, _ = r.Read(pad[:])
		}
		switch id {
		case "fmt ":
			if len(body) < 16 {
				return nil, fmt.Errorf("short fmt chunk")
			}
			audioFormat = binary.LittleEndian.Uint16(body[0:2])
			numChannels = binary.LittleEndian.Uint16(body[2:4])
			sampleRate = binary.LittleEndian.Uint32(body[4:8])
			bitsPerSample = binary.LittleEndian.Uint16(body[14:16])
		case "data":
			data = body
			goto done
		}
	}
done:
	if audioFormat != 1 {
		return nil, fmt.Errorf("unsupported WAV format %d (need PCM)", audioFormat)
	}
	if bitsPerSample != 16 {
		return nil, fmt.Errorf("unsupported bits %d (need 16)", bitsPerSample)
	}
	if numChannels < 1 {
		return nil, fmt.Errorf("no channels")
	}
	frameSize := int(numChannels) * 2
	nFrames := len(data) / frameSize
	mono := make([]float64, nFrames)
	for i := 0; i < nFrames; i++ {
		var sum float64
		for ch := 0; ch < int(numChannels); ch++ {
			off := i*frameSize + ch*2
			v := int16(binary.LittleEndian.Uint16(data[off:]))
			sum += float64(v)
		}
		mono[i] = sum / float64(numChannels)
	}
	return resample(mono, int(sampleRate), speakerRate), nil
}

func resample(in []float64, srcRate, dstRate int) []float64 {
	if srcRate == dstRate || len(in) == 0 {
		return in
	}
	outLen := int(float64(len(in)) * float64(dstRate) / float64(srcRate))
	if outLen < 1 {
		outLen = 1
	}
	out := make([]float64, outLen)
	for i := range out {
		srcPos := float64(i) * float64(srcRate) / float64(dstRate)
		i0 := int(srcPos)
		if i0 >= len(in)-1 {
			out[i] = in[len(in)-1]
			continue
		}
		frac := srcPos - float64(i0)
		out[i] = in[i0]*(1-frac) + in[i0+1]*frac
	}
	return out
}
