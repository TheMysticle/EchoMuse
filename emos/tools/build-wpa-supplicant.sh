#!/bin/bash
# Build wpa_supplicant and wpa_cli for emOS: static ARM32, nl80211, no external
# deps. Run inside the echomuse-compiler image; it fetches its own sources, or
# uses wpa_supplicant-2.10/ and libnl-tiny/ if they are already beside it.
# See emos-fireos6-system-as-root memory for why each workaround below is
# needed -- none of them are optional.
#
#   ./build-wpa-supplicant.sh [output dir]
#
# ARMV7A, and that is right for BOTH kernels, unlike the init beside it. These
# run as ordinary processes rather than as PID 1 under the kernel's own ABI, and
# a 64-bit kernel runs 32-bit binaries -- it has to, since the whole Android
# userspace on biscuit is 32-bit on top of FireOS 5's 64-bit kernel. Only the
# init needs two builds.
set -e
OUT=${1:-$(cd "$(dirname "$0")/.." && pwd)/prebuilt}
NDK=/opt/android/ndk/21.4.7075529/toolchains/llvm/prebuilt/linux-x86_64/bin
CC=$NDK/armv7a-linux-androideabi21-clang
AR=$NDK/llvm-ar

# 0. The sources, PINNED. They were fetched by hand from `master` when these
#    binaries were first built, which is not reproducible: nobody can say what
#    the shipped binary was built from, and that is the thing an auditor of a
#    published artifact most needs to know.
#
#    hostap ships release tarballs, so that one is pinned by sha256 -- the value
#    below is Arch's published sha256sum for the same file, cross-checked rather
#    than taken from one download of ours.
#
#    libnl-tiny has no releases, so it is pinned by COMMIT and cloned rather
#    than fetched as a GitHub archive: codeload tarballs are generated, and their
#    bytes are not guaranteed stable across git versions, so a sha256 over one is
#    a pin that can fail for no reason. A commit id is a content hash already.
WPA_VER=2.10
WPA_SHA256=20df7ae5154b3830355f8ab4269123a87affdea59fe74fe9292a91d0d7e17b2f
LIBNL_REPO=https://github.com/openwrt/libnl-tiny.git
LIBNL_COMMIT=40493a655d8caa2ccf5206dde1e733abe2920432

if [ ! -d "wpa_supplicant-$WPA_VER" ]; then
    echo "fetching wpa_supplicant $WPA_VER"
    curl -sfL -o wpa.tar.gz "https://w1.fi/releases/wpa_supplicant-$WPA_VER.tar.gz"
    echo "$WPA_SHA256  wpa.tar.gz" | sha256sum -c - || {
        echo "wpa_supplicant tarball does not match its pin -- refusing" >&2
        exit 1
    }
    tar xzf wpa.tar.gz
fi
if [ ! -d libnl-tiny ]; then
    echo "fetching libnl-tiny $LIBNL_COMMIT"
    git clone -q "$LIBNL_REPO" libnl-tiny
    # Verified rather than trusted: `git checkout <sha>` fails on a repository
    # that does not contain that object, so the clone cannot substitute another.
    git -C libnl-tiny checkout -q "$LIBNL_COMMIT"
fi

# 1. libnl-tiny (full libnl will not cross-compile against bionic: its bundled
#    kernel headers collide, in_addr_t undefined then struct in_addr redefined).
#    Its private netlink structs collide too, so defer to <linux/netlink.h>.
cd libnl-tiny
python3 - <<'PY'
import re
p='include/netlink/netlink-kernel.h'
s=open(p).read()
if '#include <linux/netlink.h>' not in s:
    for n in ('sockaddr_nl','nlmsghdr','nlmsgerr','nl_pktinfo'):
        s=re.sub(r'\nstruct %s\s*\n\{.*?\n\};\n' % n, '\n', s, flags=re.S)
    open(p,'w').write('#include <linux/netlink.h>\n'+s)
PY
for f in attr cache cache_mngt error genl genl_ctrl genl_family genl_mngt handlers msg nl object socket; do
    $CC -Os -w -fPIC -I include -c $f.c -o $f.o
done
$AR rcs libnl-tiny.a ./*.o
cd ..

# 2. bionic has no librt; hostap links -lrt unconditionally.
mkdir -p /tmp/stub && : > /tmp/empty.c
$CC -c /tmp/empty.c -o /tmp/empty.o && $AR rcs /tmp/stub/librt.a /tmp/empty.o
cp libnl-tiny/libnl-tiny.a /tmp/stub/

# 4. A line this build cannot parse must not discard the WHOLE network block.
#    A device that has booted FireOS 6 comes back with Amazon's conf on /data,
#    carrying fields ours rejects (p2p_no_group_iface) and known fields with
#    out-of-range values (max_oper_chwidth=255) -- and one of either loses the
#    network entirely, which means no WiFi and no way to say so but a cable.
#    Upstream already does exactly this for unsupported WEP parameters a few
#    lines above; this extends it to anything else it cannot use. Structural
#    faults (missing '=', bad quoting, unterminated block) still count.
cd "wpa_supplicant-$WPA_VER"
python3 - <<'PYEOF'
p='wpa_supplicant/config_file.c'
s=open(p).read()
old = """#endif /* CONFIG_WEP */
			errors++;
		}"""
new = """#endif /* CONFIG_WEP */
			/* emOS: warn and carry on rather than discarding the
			 * network. See tools/build-wpa-supplicant.sh. */
			wpa_printf(MSG_WARNING,
				   "Line %d: ignoring unusable field '%s'",
				   *line, pos);
			continue;
		}"""
if 'emOS: warn and carry on' not in s:
    assert old in s, "config_file.c network parse site not found"
    s = s.replace(old, new, 1)
    # And the same for an unknown GLOBAL, which otherwise discards the entire
    # file rather than just the line -- p2p_no_group_iface is one FireOS 6
    # writes and this build does not know.
    gold = '''\t\t} else if (wpa_config_process_global(config, pos, line) < 0) {
\t\t\twpa_printf(MSG_ERROR, "Line %d: Invalid configuration "
\t\t\t\t   "line '%s'.", line, pos);
\t\t\terrors++;
\t\t\tcontinue;
\t\t}'''
    gnew = '''\t\t} else if (wpa_config_process_global(config, pos, line) < 0) {
\t\t\twpa_printf(MSG_WARNING, "Line %d: ignoring unusable line "
\t\t\t\t   "'%s'", line, pos);
\t\t\tcontinue;
\t\t}'''
    assert gold in s, "config_file.c global parse site not found"
    s = s.replace(gold, gnew, 1)
    open(p,'w').write(s)
    print("config_file.c patched (network + global)")
PYEOF
cd ..

# 3. hostap's priv_netlink.h redefines the same structs. The system includes
#    must come BEFORE its #ifndef IFLA_* fallbacks, or those become macros and
#    the kernel enums fail to parse.
cd "wpa_supplicant-$WPA_VER"
python3 - <<'PY'
import re
p='src/drivers/priv_netlink.h'
s=open(p).read()
if 'emOS' not in s:
    for n in ('sockaddr_nl','nlmsghdr','ifinfomsg','rtattr'):
        s=re.sub(r'\nstruct %s\s*\n\{.*?\n\};\n' % n, '\n/* emOS: from <linux/netlink.h> */\n', s, flags=re.S)
    s=s.replace('#define PRIV_NETLINK_H',
                '#define PRIV_NETLINK_H\n#include <linux/netlink.h>\n#include <linux/rtnetlink.h>',1)
    open(p,'w').write(s)
PY

# 4. Re-install each new group key a few seconds after installing it. The
#    MediaTek firmware does not start a new GTK's replay counter at the RSC:
#    the driver zeroes the RSC field of CMD_802_11_KEY (mtk_cfg80211_add_key
#    never reads params->seq), and the firmware takes the last group PN it
#    has SEEN. When the AP is still sending under the old key as the new one
#    goes in -- it waits for every station to answer group message 1, and a
#    dozing phone delays that -- the counter is the old key's last PN, the AP
#    starts the new key at 0, and every broadcast and multicast frame is
#    dropped as a replay until the new key's count passes the old one: about
#    as long as the old key lived, most of an hour after an hourly rekey. The
#    Echo keeps every connection it opened and loses ARP and mDNS, so nothing
#    can reach it. Re-installing the same key once the AP has switched makes
#    the last PN seen the new key's own. A re-install before the switch
#    changes nothing, hence several, out to 60 s. Measured 2026-10-26, see
#    docs/experiments/echo-multicast-deafness-2026-10-26.md.
python3 - <<'PY'
p='src/rsn_supp/wpa.c'
s=open(p).read()
if 'emos_gtk_reinstall' not in s:
    head = """
/* emOS: see "Re-install each new group key" in build-wpa-supplicant.sh. */
static struct {
	enum wpa_alg alg;
	int keyidx, tx;
	u8 gtk[32];
	size_t gtk_len;
	u8 rsc[8];
	size_t rsc_len;
} emos_gtk;

static void emos_gtk_reinstall(void *eloop_ctx, void *timeout_ctx)
{
	struct wpa_sm *sm = eloop_ctx;
	int r;

	r = wpa_sm_set_key(sm, emos_gtk.alg, broadcast_ether_addr,
			   emos_gtk.keyidx, emos_gtk.tx, emos_gtk.rsc,
			   emos_gtk.rsc_len, emos_gtk.gtk, emos_gtk.gtk_len,
			   KEY_FLAG_GROUP_RX);
	wpa_printf(MSG_DEBUG, "emOS: GTK re-installed (keyidx=%d) r=%d",
		   emos_gtk.keyidx, r);
}

static void emos_gtk_reinstall_cancel(struct wpa_sm *sm)
{
	eloop_cancel_timeout(emos_gtk_reinstall, sm, NULL);
	forced_memzero(&emos_gtk, sizeof(emos_gtk));
}

static void emos_gtk_reinstall_schedule(struct wpa_sm *sm,
					const struct wpa_gtk_data *gd,
					const u8 *gtk, int tx, const u8 *rsc)
{
	static const int after_s[] = { 1, 3, 10, 30, 60 };
	size_t i;

	emos_gtk_reinstall_cancel(sm);
	if ((size_t) gd->gtk_len > sizeof(emos_gtk.gtk) ||
	    (size_t) gd->key_rsc_len > sizeof(emos_gtk.rsc))
		return;
	emos_gtk.alg = gd->alg;
	emos_gtk.keyidx = gd->keyidx;
	emos_gtk.tx = tx;
	os_memcpy(emos_gtk.gtk, gtk, gd->gtk_len);
	emos_gtk.gtk_len = gd->gtk_len;
	os_memcpy(emos_gtk.rsc, rsc, gd->key_rsc_len);
	emos_gtk.rsc_len = gd->key_rsc_len;
	for (i = 0; i < ARRAY_SIZE(after_s); i++)
		eloop_register_timeout(after_s[i], 0, emos_gtk_reinstall,
				       sm, NULL);
}

static int wpa_supplicant_install_gtk(struct wpa_sm *sm,"""
    old = "\nstatic int wpa_supplicant_install_gtk(struct wpa_sm *sm,"
    assert s.count(old) == 1, "install_gtk definition not found"
    s = s.replace(old, head, 1)
    old = """		forced_memzero(gtk_buf, sizeof(gtk_buf));
		return -1;
	}
	forced_memzero(gtk_buf, sizeof(gtk_buf));
"""
    new = """		forced_memzero(gtk_buf, sizeof(gtk_buf));
		return -1;
	}
	emos_gtk_reinstall_schedule(sm, gd, _gtk, gd->tx, key_rsc);
	forced_memzero(gtk_buf, sizeof(gtk_buf));
"""
    assert s.count(old) == 1, "install_gtk success path not found"
    s = s.replace(old, new, 1)
    # A key re-installed after the link has gone is a key for nobody, and the
    # timers hold sm, so they must not outlive a disassociation or the sm.
    for fn in ('void wpa_sm_notify_disassoc(struct wpa_sm *sm)\n{\n',
               'void wpa_sm_deinit(struct wpa_sm *sm)\n{\n'):
        assert s.count(fn) == 1, fn
        s = s.replace(fn, fn + '\temos_gtk_reinstall_cancel(sm);\n', 1)
    open(p,'w').write(s)
    print("wpa.c patched (GTK re-install)")
PY
cd wpa_supplicant
cat > .config <<'CFG'
CONFIG_DRIVER_NL80211=y
CONFIG_LIBNL_TINY=y
CONFIG_CRYPTO=internal
CONFIG_TLS=internal
CONFIG_INTERNAL_LIBTOMMATH=y
CONFIG_BACKEND=file
CONFIG_CTRL_IFACE=y
CONFIG_NO_RANDOM_POOL=y
CFG
export CC AR RANLIB=$NDK/llvm-ranlib
export CFLAGS="-Os -w -I$PWD/../../libnl-tiny/include"
export LDFLAGS="-static -L/tmp/stub"
make clean >/dev/null 2>&1 || true

# BOTH binaries. wpa_cli was built by hand when these were first made, so the
# script only ever produced half of what the image ships -- and the half it
# skipped is the one init needs to nudge the supplicant into associating.
#
# -j1 rather than $(nproc): the point of this script is now a PUBLISHED artifact,
# and a parallel make is one more thing that differs between the machine that
# built it and anyone checking it. It costs seconds on a codebase this size.
make -j1 wpa_supplicant wpa_cli

# Stripped, which is also what makes the output independent of where it was
# built: hostap has no __DATE__/__TIME__, but debug sections carry the absolute
# build path, and those go here.
$NDK/llvm-strip wpa_supplicant wpa_cli

mkdir -p "$OUT"
install -m 0755 wpa_supplicant wpa_cli "$OUT/"
echo
echo "built into $OUT:"
for f in wpa_supplicant wpa_cli; do
    printf '  %-16s %8d bytes  %s\n' "$f" "$(stat -c%s "$OUT/$f")" \
        "$(sha256sum "$OUT/$f" | cut -c1-16)…"
done
