package sendspin

import (
	"bytes"
	"encoding/base64"
	"errors"

	"github.com/mewkiz/flac"
	"github.com/mewkiz/flac/frame"
	"github.com/mewkiz/flac/meta"
)

// flacEncoder cuts mono S16 at outRate into the chunks a Sendspin player
// takes: one self-contained FLAC frame each, preceded on the wire by the
// stream header sent once in stream/start.
//
// Fixed predictors with a Rice residual (the library's own analysis picks the
// order per frame), which is what the Echo's decoder sees from Music
// Assistant too. A whole-number block size keeps every frame the same length,
// so a chunk is exactly leaderChunk of audio and its timestamp is its index.
type flacEncoder struct {
	buf    bytes.Buffer
	enc    *flac.Encoder
	header string // base64, for stream/start's codec_header
	i32    []int32
}

func newFlacEncoder() (*flacEncoder, error) {
	e := &flacEncoder{i32: make([]int32, leaderChunkFrames)}
	info := &meta.StreamInfo{
		BlockSizeMin:  leaderChunkFrames,
		BlockSizeMax:  leaderChunkFrames,
		SampleRate:    outRate,
		NChannels:     outChannels,
		BitsPerSample: outBits,
	}
	enc, err := flac.NewEncoder(&e.buf, info)
	if err != nil {
		return nil, err
	}
	e.enc = enc
	e.header = base64.StdEncoding.EncodeToString(e.buf.Bytes())
	e.buf.Reset()
	return e, nil
}

// encode returns one frame holding exactly leaderChunkFrames samples. The
// slice is valid until the next call.
func (e *flacEncoder) encode(pcm []int16) ([]byte, error) {
	if len(pcm) != leaderChunkFrames {
		return nil, errors.New("sendspin: flac chunk is not a whole block")
	}
	for i, s := range pcm {
		e.i32[i] = int32(s)
	}
	f := &frame.Frame{
		Header: frame.Header{
			HasFixedBlockSize: true,
			BlockSize:         leaderChunkFrames,
			SampleRate:        outRate,
			Channels:          frame.ChannelsMono,
			BitsPerSample:     outBits,
		},
		Subframes: []*frame.Subframe{{
			SubHeader: frame.SubHeader{Pred: frame.PredVerbatim},
			Samples:   e.i32,
			NSamples:  leaderChunkFrames,
		}},
	}
	e.buf.Reset()
	if err := e.enc.WriteFrame(f); err != nil {
		return nil, err
	}
	return e.buf.Bytes(), nil
}
