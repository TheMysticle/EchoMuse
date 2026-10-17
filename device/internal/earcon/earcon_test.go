package earcon

import (
	"bytes"
	"encoding/binary"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func makeTestWAV(sampleRate int, samples []int16) []byte {
	buf := new(bytes.Buffer)
	buf.WriteString("RIFF")
	totalSize := uint32(36 + len(samples)*2)
	binary.Write(buf, binary.LittleEndian, totalSize)
	buf.WriteString("WAVE")
	buf.WriteString("fmt ")
	binary.Write(buf, binary.LittleEndian, uint32(16))
	binary.Write(buf, binary.LittleEndian, uint16(1)) // PCM
	binary.Write(buf, binary.LittleEndian, uint16(1)) // 1 channel
	binary.Write(buf, binary.LittleEndian, uint32(sampleRate))
	binary.Write(buf, binary.LittleEndian, uint32(sampleRate*2))
	binary.Write(buf, binary.LittleEndian, uint16(2))  // block align
	binary.Write(buf, binary.LittleEndian, uint16(16)) // 16 bits
	buf.WriteString("data")
	binary.Write(buf, binary.LittleEndian, uint32(len(samples)*2))
	for _, s := range samples {
		binary.Write(buf, binary.LittleEndian, s)
	}
	return buf.Bytes()
}

func TestDecodeWAV(t *testing.T) {
	samples := []int16{0, 1000, 2000, 3000, 2000, 1000, 0, -1000}
	wavData := makeTestWAV(48000, samples)
	tmp := filepath.Join(t.TempDir(), "test.wav")
	if err := os.WriteFile(tmp, wavData, 0o644); err != nil {
		t.Fatal(err)
	}
	loaded, err := Load(tmp)
	if err != nil {
		t.Fatalf("Load WAV failed: %v", err)
	}
	if len(loaded) != len(samples) {
		t.Fatalf("got %d samples, want %d", len(loaded), len(samples))
	}
	for i, s := range samples {
		if math.Abs(loaded[i]-float64(s)) > 0.001 {
			t.Errorf("sample %d: got %f, want %d", i, loaded[i], s)
		}
	}
}

func TestLoadOfflineMP3IfAvailable(t *testing.T) {
	firmwarePath := os.Getenv("EARCON_TEST_MP3")
	if firmwarePath == "" {
		firmwarePath = "/mnt/Development/alexa-firm/out/system/system/local/share/earcon/en/system_prompt_error_offline_not_connected_to_service.mp3"
	}
	if _, err := os.Stat(firmwarePath); err != nil {
		t.Skip("firmware MP3 not available on host")
	}
	loaded, err := Load(firmwarePath)
	if err != nil {
		t.Fatalf("Load MP3 failed: %v", err)
	}
	if len(loaded) < 48000*3 {
		t.Fatalf("MP3 duration too short: got %d samples", len(loaded))
	}
	// Test volume scaling
	scaled, err := LoadWithVolume(firmwarePath, 0.5)
	if err != nil {
		t.Fatalf("LoadWithVolume failed: %v", err)
	}
	if len(scaled) != len(loaded) {
		t.Fatalf("scaled len mismatch: got %d, want %d", len(scaled), len(loaded))
	}
	for i := 1000; i < 2000; i++ {
		if math.Abs(scaled[i]-loaded[i]*0.5) > 0.0001 {
			t.Fatalf("sample %d not scaled properly: loaded=%f scaled=%f", i, loaded[i], scaled[i])
		}
	}
}
