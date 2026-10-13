/*
 * shim.h - the C half of the binding to the Echo's own wake word decoder.
 *
 * The library (Radar's libpryon.so) is dlopen'd at run time and nothing here
 * references one of its symbols at link time, so the firmware links, boots and
 * runs on a board that does not have it: em_pryon_open just fails and the caller
 * carries on with the open model. That is the same reason internal/wakeword/ort
 * does it, and it is what makes this an opt-in for one platform rather than a
 * dependency.
 *
 * The calling convention was read off the way Amazon's libWakeWordManager calls
 * the same functions, and is documented in tools/pryon_probe/probe.c:
 *
 *   PryonModelSet_NewSimple(id, manifestPath)                          0 = ok
 *   PryonDecoder_NewPryonMultichannelAudioFormat_Default(&fmt)         fmt: 144 bytes
 *   PryonDecoder_NewSpotterAudioDecoder(id, modelSetId, "pryon", fmt, "{}")  0 = ok
 *   PryonDecoder_PushAudioEventSamples(id, uint64 timestamp, samples, count) 0 = ok
 *   PryonApi_SetEnumeratedResultCallback(cb(id, result*))
 *   PryonApi_SetLoggingCallback(cb(level, tag, message))
 *
 * The result struct is read as 32-bit words and only on a 32-bit build: the
 * layout is the Echo's (ARM32), and a 64-bit host build has nothing to read it
 * from. word 2/3 = begin sample, 4/5 = end sample, 7 = keyword char*, 9 = type.
 *
 * Error convention matches the ort shim: char* NULL on success, else a malloc'd
 * message the Go side frees.
 */
#ifndef EM_PRYON_SHIM_H
#define EM_PRYON_SHIM_H

#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { uint32_t w[36]; } em_pryon_fmt;
typedef struct { uint32_t err; uint32_t v; } em_pryon_bl;

typedef void (*em_logcb_t)(int, const char *, const char *);
typedef void (*em_rescb_t)(const char *, const void *);

typedef struct {
	void *dl;
	void (*set_log)(em_logcb_t);
	int (*set_result)(em_rescb_t);
	int (*modelset_new)(const char *, const char *);
	int (*modelset_delete)(const char *);
	void (*fmt_default)(em_pryon_fmt *);
	int (*decoder_new)(const char *, const char *, const char *, em_pryon_fmt, const char *);
	int (*decoder_delete)(const char *);
	int (*push)(const char *, uint64_t, const int16_t *, uint32_t);
	em_pryon_bl (*backlog)(const char *);
} em_pryon;

/* Implemented in Go (//export). */
extern void emPryonLog(int level, char *msg);
extern void emPryonResult(unsigned int typ, unsigned long long begin, unsigned long long end, char *keyword);

static void em_log_tramp(int level, const char *tag, const char *msg) {
	(void)tag;
	if (msg) emPryonLog(level, (char *)msg);
}

static void em_result_tramp(const char *id, const void *res) {
	(void)id;
#if UINTPTR_MAX == 0xffffffffu
	const uint32_t *w = (const uint32_t *)res;
	unsigned long long b = ((unsigned long long)w[3] << 32) | w[2];
	unsigned long long e = ((unsigned long long)w[5] << 32) | w[4];
	emPryonResult(w[9], b, e, (char *)(uintptr_t)w[7]);
#else
	(void)res;
#endif
}

static char *em_dup(const char *s) {
	size_t n = strlen(s) + 1;
	char *p = (char *)malloc(n);
	if (p) memcpy(p, s, n);
	return p;
}

#define EM_SYM(field, name)                                              \
	do {                                                                 \
		*(void **)(&out->field) = dlsym(out->dl, name);                  \
		if (!out->field) {                                               \
			char msg[160];                                               \
			snprintf(msg, sizeof msg, "libpryon: %s not found", name);   \
			dlclose(out->dl);                                            \
			out->dl = NULL;                                              \
			return em_dup(msg);                                          \
		}                                                                \
	} while (0)

/* em_pryon_open dlopens the library and resolves every function used. */
static char *em_pryon_open(const char *path, em_pryon *out) {
	memset(out, 0, sizeof *out);
	out->dl = dlopen(path, RTLD_NOW | RTLD_LOCAL);
	if (!out->dl) {
		const char *e = dlerror();
		return em_dup(e ? e : "dlopen failed");
	}
	EM_SYM(set_log, "PryonApi_SetLoggingCallback");
	EM_SYM(set_result, "PryonApi_SetEnumeratedResultCallback");
	EM_SYM(modelset_new, "PryonModelSet_NewSimple");
	EM_SYM(modelset_delete, "PryonModelSet_Delete");
	EM_SYM(fmt_default, "PryonDecoder_NewPryonMultichannelAudioFormat_Default");
	EM_SYM(decoder_new, "PryonDecoder_NewSpotterAudioDecoder");
	EM_SYM(decoder_delete, "PryonDecoder_Delete");
	EM_SYM(push, "PryonDecoder_PushAudioEventSamples");
	EM_SYM(backlog, "PryonDecoder_BacklogMsec");
	return NULL;
}

/* em_pryon_start registers the callbacks, loads the model set and builds the decoder.
 * Everything it creates is released by em_pryon_stop, in the reverse order. */
static char *em_pryon_start(em_pryon *p, const char *msid, const char *manifest, const char *decid) {
	char msg[256];
	p->set_log(em_log_tramp);
	if (p->set_result(em_result_tramp)) return em_dup("libpryon: could not register the result callback");
	int rc = p->modelset_new(msid, manifest);
	if (rc) {
		snprintf(msg, sizeof msg, "libpryon: loading the model set from %s failed (%d)", manifest, rc);
		return em_dup(msg);
	}
	em_pryon_fmt fmt;
	memset(&fmt, 0, sizeof fmt);
	p->fmt_default(&fmt);
	rc = p->decoder_new(decid, msid, "pryon", fmt, "{}");
	if (rc) {
		p->modelset_delete(msid);
		snprintf(msg, sizeof msg, "libpryon: creating the decoder failed (%d)", rc);
		return em_dup(msg);
	}
	return NULL;
}

/* em_pryon_push hands 16kHz mono int16 to the decoder, stamped in samples. */
static int em_pryon_push(em_pryon *p, const char *decid, uint64_t ts, const int16_t *s, uint32_t n) {
	return p->push(decid, ts, s, n);
}

/* em_pryon_backlog_ms is how much audio the decoder has queued and not yet scored.
 * The decoder DROPS audio when this grows, so the caller must not outrun it. */
static int em_pryon_backlog_ms(em_pryon *p, const char *decid) {
	em_pryon_bl b = p->backlog(decid);
	return b.err ? -1 : (int)b.v;
}

/* em_pryon_stop deletes the decoder and then the model set. The library
 * complains and leaves a worker thread behind if the process exits without it. */
static void em_pryon_stop(em_pryon *p, const char *msid, const char *decid) {
	p->decoder_delete(decid);
	p->modelset_delete(msid);
}

#endif
