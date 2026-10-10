"""
The on-device Spotify binaries: what they are, where they go, and whether
what somebody uploaded is one of them.

Pure logic, so it is unit-tested without aiohttp or a device. The two
transports live in `em_api.py`, exactly as `em_oww_assets` splits planning
from carrying out.

**Why these are uploaded rather than shipped in the firmware.** The engine
is ~4MB and the firmware OTA writes both A/B slots, so folding it in would
double the payload for every update whether or not Spotify changed — the
same reasoning that keeps the 12.3MB ONNX runtime out of the firmware. It
therefore travels the asset path: into the controller's data volume once,
then to each device over the shell plane, md5-verified.

**Why validation is by ELF HEADER and not by filename.** A file called
`em-spotify` that is actually an x86 build, a shell script or a truncated
download installs perfectly and then fails on the device with nothing
useful said — the firmware would report a process that exits immediately,
which is indistinguishable from a dozen other faults. The header answers
three questions that each produce that same silent failure: is it ARM, is
it 32-bit, and is it statically linked. emOS has no dynamic loader for a
musl binary, so a dynamically linked build cannot run at all, and
`emos/tools/build-busybox.sh` asserts the same three things about busybox
for the same reason.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from pathlib import Path

# Where the artefacts live on the device. Under /data/local/share rather
# than /data/local/bin, which the A/B firmware symlink owns — these are
# assets with their own lifecycle, like the wake word runtime beside them.
DEVICE_DIR = "/data/local/share/echomuse/spotify"

# Where they are kept on the controller: beside the database, so they
# survive a container restart and travel with a /data backup.
ASSETS_SUBDIR = "spotify"

# The engine. Started and supervised by the firmware.
ENGINE = "em-spotify"
# The feasibility probe. A diagnostic that writes nothing, touches no mixer
# control and opens no socket — see device/spotify/src/probe.rs.
PROBE = "em-spotify-probe"

#: Artefacts the controller will accept, in install order.
ARTEFACTS: tuple[str, ...] = (ENGINE, PROBE)

# Generous against the measured 4.1MB engine, tight enough that a wrong
# file (a core dump, somebody's album) is refused rather than stored.
MAX_ARTEFACT_BYTES = 24 * 1024 * 1024


class BadArtefact(ValueError):
    """An upload that is not a binary this device can run."""


@dataclass(frozen=True)
class Artefact:
    """One stored binary."""

    name: str
    size: int
    md5: str

    def as_json(self) -> dict:
        return {"name": self.name, "size": self.size, "md5": self.md5}


def assets_dir(db_path: str | None = None) -> Path:
    """Where artefacts are stored, beside the database."""
    if db_path is None:
        db_path = os.environ.get("DB_PATH", "echomuse.db")
    return Path(db_path).resolve().parent / ASSETS_SUBDIR


def safe_artefact_name(filename: str) -> str | None:
    """
    The artefact a given upload filename is, or None.

    An allowlist of exactly two names rather than a sanitiser. A sanitiser
    answers "is this filename safe to write" and the question here is "is
    this one of the two files the firmware looks for" — a path component
    that is safe and wrong is still a file nothing will ever load.
    """
    base = Path(filename).name
    return base if base in ARTEFACTS else None


def device_path(name: str) -> str:
    """Where `name` is installed on the device."""
    if name not in ARTEFACTS:
        raise ValueError(f"{name} is not a Spotify artefact")
    return f"{DEVICE_DIR}/{name}"


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ── ELF validation ──────────────────────────────────────────────────────────
#
# Field offsets and values from the ELF32 specification. Named rather than
# inlined because a wrong constant here refuses a working binary, which is
# worse than accepting a bad one: it blocks the operator from their own
# device with a confident message.

_ELF_MAGIC = b"\x7fELF"
_EI_CLASS = 4          # 1 = ELF32, 2 = ELF64
_EI_DATA = 5           # 1 = little endian
_ELFCLASS32 = 1
_ELFDATA2LSB = 1
_ET_EXEC = 2           # e_type: a non-relocatable executable
_ET_DYN = 3            # a shared object, which is also what a PIE is
_EM_ARM = 40           # e_machine
_PT_INTERP = 3         # a program header naming a dynamic loader
_ELF_HEADER_MIN = 52   # size of the ELF32 header


def describe_binary(data: bytes) -> dict:
    """
    What an uploaded file is, as far as its ELF header says.

    Raises BadArtefact naming the first thing that is wrong. The order is
    deliberate: the most specific answer first, so "this is an x86 build"
    is never reported as "this is not static".
    """
    if len(data) < _ELF_HEADER_MIN:
        raise BadArtefact(
            f"the file is {len(data)} bytes, too short to be a binary"
        )
    if data[:4] != _ELF_MAGIC:
        raise BadArtefact(
            "the file is not an ELF binary — a script, an archive or a "
            "truncated download would look like this"
        )
    if data[_EI_CLASS] != _ELFCLASS32:
        raise BadArtefact(
            "this is a 64-bit binary; the Echo runs a 32-bit armv7 userspace"
        )
    if data[_EI_DATA] != _ELFDATA2LSB:
        raise BadArtefact("this is a big-endian binary; the Echo is little-endian")

    # e_type at 16, e_machine at 18, both u16 little-endian.
    (e_type, e_machine) = struct.unpack_from("<HH", data, 16)
    if e_machine != _EM_ARM:
        raise BadArtefact(
            f"this is not an ARM binary (ELF machine {e_machine}); it was "
            "probably built for the host instead of cross-compiled"
        )

    # Program headers, to answer "static" properly. e_phoff at 28,
    # e_phentsize at 42, e_phnum at 44.
    (e_phoff,) = struct.unpack_from("<I", data, 28)
    (e_phentsize, e_phnum) = struct.unpack_from("<HH", data, 42)
    interp = False
    for i in range(e_phnum):
        at = e_phoff + i * e_phentsize
        if at + 4 > len(data):
            # A header table that runs off the end of the file is a
            # truncated download. Refused rather than read as "no INTERP
            # found", which would pass a half-transferred binary.
            raise BadArtefact(
                "the program header table runs past the end of the file — "
                "the upload is truncated"
            )
        (p_type,) = struct.unpack_from("<I", data, at)
        if p_type == _PT_INTERP:
            interp = True
            break

    if interp:
        raise BadArtefact(
            "this binary is dynamically linked; emOS has no loader for it, "
            "so it would fail to start with nothing useful logged. Build "
            "with device/spotify/compile.sh"
        )
    if e_type == _ET_DYN:
        raise BadArtefact(
            "this binary is a PIE; the Echo's kernel wants ET_EXEC, which is "
            "the shape emOS's own busybox uses. Build with "
            "device/spotify/compile.sh"
        )
    if e_type != _ET_EXEC:
        raise BadArtefact(f"this is not an executable (ELF type {e_type})")

    return {"arch": "arm", "bits": 32, "static": True, "type": "ET_EXEC"}


def validate_upload(filename: str, data: bytes) -> str:
    """
    Check an upload and return the artefact name it will be stored as.

    Raises BadArtefact with something a person can act on.
    """
    name = safe_artefact_name(filename)
    if name is None:
        raise BadArtefact(
            f"{Path(filename).name or 'the upload'} is not a Spotify "
            f"artefact; expected one of {', '.join(ARTEFACTS)}"
        )
    if not data:
        raise BadArtefact("the upload is empty")
    if len(data) > MAX_ARTEFACT_BYTES:
        raise BadArtefact(
            f"the upload is {len(data) / 1024 / 1024:.1f} MB, over the "
            f"{MAX_ARTEFACT_BYTES // 1024 // 1024} MB limit"
        )
    describe_binary(data)
    return name


# ── Install planning ────────────────────────────────────────────────────────


def plan_install(
    stored: dict[str, str],
    on_device: dict[str, str],
    wanted: tuple[str, ...] = ARTEFACTS,
) -> list[str]:
    """
    Which artefacts need pushing, given md5s on each side.

    `stored` and `on_device` map artefact name to md5. A name missing from
    `on_device` needs pushing; a name whose md5 differs needs pushing; a
    name that matches is left alone.

    **Presence is judged by md5, never by the file existing**, for the
    reason the wake word assets are: a rebuilt engine keeps its name, and
    counting that as installed leaves the device running a binary that
    silently disagrees with the controller. It is also what makes a
    reinstall after a failed transfer cheap rather than a no-op.

    An artefact the controller does not hold is not planned — there is
    nothing to send — and that is NOT an error here. Whether a device
    should have the engine at all is a policy question for the caller.
    """
    plan = []
    for name in wanted:
        have = stored.get(name)
        if have is None:
            continue
        if on_device.get(name) != have:
            plan.append(name)
    return plan
