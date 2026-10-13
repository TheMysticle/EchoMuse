package pryon

/*
#cgo LDFLAGS: -ldl
#include "shim.h"
*/
import "C"

import (
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"time"
	"unsafe"

	"github.com/wilbowes/EchoMuse/internal/wakeword/shadow"
)

// DefaultLib and DefaultModelDir are where Radar's firmware keeps them, and
// where emOS finds them too, since it mounts the same userspace. Overridable
// for a scratch directory the way the open engine's EM_OWW_DIR is.
const (
	DefaultLib      = "libpryon.so" // resolved by the Android linker: /system/lib
	DefaultModelDir = "/system/local/models/keyword/en-US/ALEXA"
	libPath         = "/system/lib/libpryon.so"
)

func libName() string {
	if p := os.Getenv("EM_PRYON_LIB"); p != "" {
		return p
	}
	return DefaultLib
}

func modelDir() string {
	if d := os.Getenv("EM_PRYON_MODEL_DIR"); d != "" {
		return d
	}
	return DefaultModelDir
}

// Available says whether this device has what the engine needs, without loading
// it: the library file and the model manifest. It is what the capability the
// controller reads is decided on, so it must be cheap and must never touch the
// library itself (a bad load would take the registration with it).
func Available() bool {
	lib := libPath
	if p := os.Getenv("EM_PRYON_LIB"); p != "" {
		lib = p
	}
	if filepath.Base(lib) == lib { // a bare name is the linker's to find
		lib = "/system/lib/" + lib
	}
	for _, p := range []string{lib, filepath.Join(modelDir(), "pryon.manifest")} {
		if st, err := os.Stat(p); err != nil || st.IsDir() {
			return false
		}
	}
	return true
}

// ── the process-wide pieces ───────────────────────────────────────────────────
//
// The decoder's callbacks are registered once for the whole library and are
// plain C function pointers, so they cannot carry a Go closure: they reach the
// running Scorer through these. One Scorer exists at a time.

var (
	active atomic.Pointer[Scorer]
	// lastScore is the classifier_score of the most recent decision the decoder
	// logged, as float bits. It is written and read on the decoder's thread,
	// log line first and result callback second, which is the order it emits
	// them in.
	lastScore atomic.Uint32
)

//export emPryonLog
func emPryonLog(level C.int, msg *C.char) {
	s := C.GoString(msg)
	if v, ok := ParseScore(s); ok {
		lastScore.Store(floatBits(v))
	}
	if int(level) <= 2 { // error and fatal only: the rest is a firehose
		log.Printf("[pryon] %s", s)
	}
}

//export emPryonResult
func emPryonResult(typ C.uint, begin, end C.ulonglong, keyword *C.char) {
	sc := active.Load()
	if sc == nil {
		return
	}
	kw := ""
	if keyword != nil {
		kw = C.GoString(keyword)
	}
	sc.onResult(uint32(typ), kw, uint64(begin), uint64(end), floatFromBits(lastScore.Load()))
}

var (
	libMu sync.Mutex
	lib   *C.em_pryon // dlopen'd once per process; Scorers are built and torn down on it
)

func openLib() (*C.em_pryon, error) {
	libMu.Lock()
	defer libMu.Unlock()
	if lib != nil {
		return lib, nil
	}
	p := (*C.em_pryon)(C.calloc(1, C.size_t(unsafe.Sizeof(C.em_pryon{}))))
	name := C.CString(libName())
	defer C.free(unsafe.Pointer(name))
	if msg := C.em_pryon_open(name, p); msg != nil {
		defer C.free(unsafe.Pointer(msg))
		C.free(unsafe.Pointer(p))
		return nil, errors.New(C.GoString(msg))
	}
	lib = p
	return lib, nil
}

// cBackend is the real decoder behind the Scorer.
type cBackend struct {
	p     *C.em_pryon
	msid  *C.char
	decid *C.char
}

func (b *cBackend) push(ts uint64, pcm []int16) error {
	if len(pcm) == 0 {
		return nil
	}
	if rc := C.em_pryon_push(b.p, b.decid, C.uint64_t(ts), (*C.int16_t)(unsafe.Pointer(&pcm[0])), C.uint32_t(len(pcm))); rc != 0 {
		return fmt.Errorf("pryon: push returned %d", int(rc))
	}
	return nil
}

func (b *cBackend) backlogMs() int { return int(C.em_pryon_backlog_ms(b.p, b.decid)) }

func (b *cBackend) close() {
	C.em_pryon_stop(b.p, b.msid, b.decid)
	C.free(unsafe.Pointer(b.msid))
	C.free(unsafe.Pointer(b.decid))
}

// Open loads the library, the ALEXA model set and a decoder, and starts a
// Scorer on it. A device without them returns an error and is otherwise
// unaffected: the caller logs it once and keeps the open engine.
func Open(onCross func(score, threshold float32, at time.Time)) (*Scorer, error) {
	if !Available() {
		return nil, fmt.Errorf("pryon: not available (needs %s and %s/pryon.manifest on this device)", libPath, modelDir())
	}
	p, err := openLib()
	if err != nil {
		return nil, fmt.Errorf("pryon: %w", err)
	}
	b := &cBackend{p: p, msid: C.CString("echomuse-ms"), decid: C.CString("echomuse")}
	manifest := C.CString(filepath.Join(modelDir(), "pryon.manifest"))
	defer C.free(unsafe.Pointer(manifest))
	if active.Load() != nil {
		C.free(unsafe.Pointer(b.msid))
		C.free(unsafe.Pointer(b.decid))
		return nil, errors.New("pryon: an engine is already running")
	}
	if msg := C.em_pryon_start(p, b.msid, manifest, b.decid); msg != nil {
		defer C.free(unsafe.Pointer(msg))
		C.free(unsafe.Pointer(b.msid))
		C.free(unsafe.Pointer(b.decid))
		return nil, errors.New(C.GoString(msg))
	}
	s := newScorer(b, onCross)
	s.info = "pryon (the Echo's own decoder), model " + Keyword + ", accept " + fmt.Sprintf("%.3f/%.3f", AcceptBar, PlaybackBar)
	return s, nil
}

var _ shadow.Engine = (*Scorer)(nil)
