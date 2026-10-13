/* probe.c - drive Radar's own wake word decoder (libpryon.so) from C, on a PC.
 *
 * Reads raw 16kHz mono int16 from a file, pushes it to a decoder built from the
 * firmware's ALEXA model package, and prints one line per detection:
 *
 *     DET begin=<sample> end=<sample> classifier_score=<s>,active_threshold=<t>,detection_type=Accept
 *
 * Nothing of Amazon's is in this file: the library and the model are loaded at
 * run time from a copy of a device's own /system (see README.md). The calling
 * convention was read off the way libWakeWordManager calls the same functions:
 *   PryonModelSet_NewSimple(id, manifestPath)                       -> 0 on success
 *   PryonDecoder_NewPryonMultichannelAudioFormat_Default(&fmt)      fmt is 144 bytes
 *   PryonDecoder_NewSpotterAudioDecoder(id, modelSetId, "pryon", fmt /by value/, "{}")
 *   PryonDecoder_PushAudioEventSamples(id, uint64 timestamp, samples, count)
 *   PryonDecoder_BacklogMsec(id)                                     {err, msec}
 *   PryonApi_SetEnumeratedResultCallback(cb(id, result*))            result[2], result[4] = begin, end sample
 * The decoder drops audio when its backlog grows, so the push loop waits on
 * BacklogMsec; flooding it (as a first version did) silently loses most of a file.
 */
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct { uint32_t w[36]; } PMCFormat;   /* 144 bytes, passed by value */

static char lastscore[200];
static void logcb(int level, const char *tag, const char *msg) {
    if (msg && strstr(msg, "classifier_score=")) { const char *p = strstr(msg, "classifier_score="); snprintf(lastscore, sizeof lastscore, "%s", p); }
    if (level <= 2) { printf("[pryon %d] %s: %s\n", level, tag ? tag : "", msg ? msg : ""); fflush(stdout); }
}
static int ndet = 0;
static void resultcb(const char *id, const void *res) {
    ndet++;
    const uint32_t *w = (const uint32_t *)res;
    printf("DET begin=%u end=%u %s\n", w[2], w[4], lastscore); fflush(stdout);
}

typedef void (*setlog_t)(void (*)(int, const char *, const char *));
typedef int (*setres_t)(void (*)(const char *, const void *));
typedef int (*msnew_t)(const char *, const char *);
typedef void (*fmtdef_t)(PMCFormat *);
typedef int (*newdec_t)(const char *, const char *, const char *, PMCFormat, const char *);
typedef int (*push_t)(const char *, uint64_t, const int16_t *, uint32_t);
typedef int (*del_t)(const char *);
typedef struct { uint32_t err; uint32_t v; } BL;
typedef BL (*backlog_t)(const char *);

#define SYM(T, n) T n = (T)dlsym(h, #n); if (!n) { printf("missing " #n "\n"); return 2; }

int main(int argc, char **argv) {
    void *h = dlopen("libpryon.so", RTLD_NOW | RTLD_LOCAL);
    if (!h) { printf("dlopen: %s\n", dlerror()); return 1; }
    SYM(setlog_t, PryonApi_SetLoggingCallback)
    SYM(setres_t, PryonApi_SetEnumeratedResultCallback)
    SYM(msnew_t, PryonModelSet_NewSimple)
    SYM(fmtdef_t, PryonDecoder_NewPryonMultichannelAudioFormat_Default)
    SYM(newdec_t, PryonDecoder_NewSpotterAudioDecoder)
    SYM(push_t, PryonDecoder_PushAudioEventSamples)
    PryonApi_SetLoggingCallback(logcb);
    printf("setres rc=%d\n", PryonApi_SetEnumeratedResultCallback(resultcb));
    int rc = PryonModelSet_NewSimple("ms", "/system/local/models/keyword/en-US/ALEXA/pryon.manifest");
    printf("modelset rc=%d\n", rc); if (rc) return 3;
    PMCFormat fmt; memset(&fmt, 0xAA, sizeof fmt);
    PryonDecoder_NewPryonMultichannelAudioFormat_Default(&fmt);
    printf("format words:"); for (int i = 0; i < 36; i++) printf(" %u", fmt.w[i]); printf("\n");
    rc = PryonDecoder_NewSpotterAudioDecoder("dec", "ms", "pryon", fmt, argc > 2 ? argv[2] : "{}");
    printf("decoder rc=%d\n", rc); fflush(stdout);
    if (argc < 2) return 0;
    FILE *f = fopen(argv[1], "rb"); if (!f) { printf("no input\n"); return 4; }
    backlog_t BacklogMsec = (backlog_t)dlsym(h, "PryonDecoder_BacklogMsec");
    int16_t buf[160]; uint64_t pos = 0; size_t n;
    while ((n = fread(buf, 2, 160, f)) == 160) {
        /* Real-time-ish: the decoder drops audio when its backlog gets too long, and
           emulation is slower than the Echo, so wait for it instead of flooding it. */
        while (BacklogMsec) { BL b = BacklogMsec("dec"); if (b.err || b.v < 400) break; usleep(20000); }
        int p = PryonDecoder_PushAudioEventSamples("dec", pos, buf, 160);
        if (p) { printf("push rc=%d at %llu\n", p, (unsigned long long)pos); break; }
        pos += 160;
    }
    int16_t z[160] = {0};                    /* a second of silence to flush the decoder */
    for (int i = 0; i < 100; i++) { while (BacklogMsec) { BL b = BacklogMsec("dec"); if (b.err || b.v < 400) break; usleep(20000); } PryonDecoder_PushAudioEventSamples("dec", pos, z, 160); pos += 160; }
    /* The decoder works on its own thread, far slower than real time under emulation:
       wait until no new result has arrived for a good while. */
    int last = -1, still = 0;
    for (int t = 0; t < 3000 && still < 40; t++) { usleep(100000); if (ndet == last) still++; else { still = 0; last = ndet; } }
    sleep(1);
    printf("pushed %llu samples, %d results\n", (unsigned long long)pos, ndet);
    del_t dd = (del_t)dlsym(h, "PryonDecoder_Delete"), dm = (del_t)dlsym(h, "PryonModelSet_Delete");
    if (dd) printf("delete decoder rc=%d\n", dd("dec"));
    if (dm) printf("delete modelset rc=%d\n", dm("ms"));
    return 0;
}
