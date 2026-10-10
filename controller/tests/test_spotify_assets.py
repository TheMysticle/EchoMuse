"""
em_spotify_assets: what the controller will accept as an on-device Spotify
binary, and what it plans to install.

The ELF tests are written from the format's edges rather than from a real
binary, so each failure mode is exercised on its own — the point of the
validator is that a wrong file is refused with the RIGHT reason, since
every one of these produces the same symptom on the device (a process that
exits at once) and the reason is the only thing that distinguishes them.
"""

import struct

import pytest

import em_spotify_assets as sa


# ── A synthetic ELF32 so each field can be wrong in isolation ───────────────


def elf32(
    *,
    ei_class: int = 1,       # ELFCLASS32
    ei_data: int = 1,        # little endian
    e_type: int = 2,         # ET_EXEC
    e_machine: int = 40,     # EM_ARM
    interp: bool = False,
    phnum: int | None = None,
    truncate_headers: bool = False,
) -> bytes:
    """Build a minimal, structurally valid ELF32 header plus program headers."""
    e_phentsize = 32
    headers = []
    if interp:
        headers.append(3)    # PT_INTERP
    headers.append(1)        # PT_LOAD, so there is always at least one
    if phnum is None:
        phnum = len(headers)

    e_phoff = 52
    buf = bytearray(b"\x00" * e_phoff)
    buf[0:4] = b"\x7fELF"
    buf[sa._EI_CLASS] = ei_class
    buf[sa._EI_DATA] = ei_data
    struct.pack_into("<HH", buf, 16, e_type, e_machine)
    struct.pack_into("<I", buf, 28, e_phoff)
    struct.pack_into("<HH", buf, 42, e_phentsize, phnum)

    if not truncate_headers:
        for p_type in headers:
            entry = bytearray(b"\x00" * e_phentsize)
            struct.pack_into("<I", entry, 0, p_type)
            buf += entry
    return bytes(buf)


def test_a_good_binary_is_accepted():
    got = sa.describe_binary(elf32())
    assert got == {"arch": "arm", "bits": 32, "static": True, "type": "ET_EXEC"}


def test_a_64_bit_binary_says_so():
    with pytest.raises(sa.BadArtefact, match="64-bit"):
        sa.describe_binary(elf32(ei_class=2))


def test_a_host_build_is_named_as_the_wrong_architecture():
    # The likeliest mistake: `cargo build --release` without the target, so
    # an x86-64 binary with the right filename. On the device it would fail
    # to exec and report nothing about why.
    with pytest.raises(sa.BadArtefact, match="not an ARM binary"):
        sa.describe_binary(elf32(e_machine=62))  # EM_X86_64


def test_a_dynamically_linked_binary_is_refused():
    # emOS has no loader for a musl binary, so this cannot run at all.
    with pytest.raises(sa.BadArtefact, match="dynamically linked"):
        sa.describe_binary(elf32(interp=True))


def test_a_pie_is_refused_and_named_as_a_pie():
    # Rust's musl targets can emit a static PIE, which this 3.18 kernel
    # would have to self-relocate; emos/tools/build-busybox.sh passes
    # -no-pie for the same reason and calls ET_EXEC the proven shape. A PIE
    # has no INTERP, so without this check it would pass as "static".
    with pytest.raises(sa.BadArtefact, match="PIE"):
        sa.describe_binary(elf32(e_type=3))


def test_a_script_is_not_an_elf_binary():
    with pytest.raises(sa.BadArtefact, match="not an ELF binary"):
        sa.describe_binary(b"#!/bin/sh\necho hello\n" + b"x" * 60)


def test_something_far_too_short():
    with pytest.raises(sa.BadArtefact, match="too short"):
        sa.describe_binary(b"\x7fELF")


def test_a_truncated_upload_is_refused_rather_than_read_as_static():
    # A header table that runs off the end would otherwise be walked, find
    # no PT_INTERP, and be called static — passing a half-transferred
    # binary as a good one.
    with pytest.raises(sa.BadArtefact, match="truncated"):
        sa.describe_binary(elf32(phnum=64, truncate_headers=True))


def test_big_endian_is_refused():
    with pytest.raises(sa.BadArtefact, match="big-endian"):
        sa.describe_binary(elf32(ei_data=2))


# ── Filenames ───────────────────────────────────────────────────────────────


def test_only_the_two_known_names_are_accepted():
    assert sa.safe_artefact_name("em-spotify") == "em-spotify"
    assert sa.safe_artefact_name("em-spotify-probe") == "em-spotify-probe"
    assert sa.safe_artefact_name("librespot") is None
    assert sa.safe_artefact_name("em-spotify.exe") is None


def test_a_path_is_reduced_to_its_basename():
    # Browsers send a bare filename, but a client need not. An allowlist of
    # exact names makes traversal structurally impossible rather than
    # filtered: the result is either one of two constants or a refusal.
    assert sa.safe_artefact_name("/tmp/em-spotify") == "em-spotify"
    assert sa.safe_artefact_name("../../etc/passwd") is None
    assert sa.safe_artefact_name("../em-spotify") == "em-spotify"


def test_device_path_refuses_anything_not_an_artefact():
    assert sa.device_path("em-spotify") == f"{sa.DEVICE_DIR}/em-spotify"
    with pytest.raises(ValueError):
        sa.device_path("../../bin/sh")


def test_validate_upload_names_the_artefact():
    assert sa.validate_upload("em-spotify", elf32()) == "em-spotify"


def test_validate_upload_rejects_an_unknown_name_before_looking_at_bytes():
    with pytest.raises(sa.BadArtefact, match="not a Spotify"):
        sa.validate_upload("server", elf32())


def test_validate_upload_rejects_an_empty_file():
    with pytest.raises(sa.BadArtefact, match="empty"):
        sa.validate_upload("em-spotify", b"")


def test_validate_upload_rejects_something_enormous():
    with pytest.raises(sa.BadArtefact, match="limit"):
        sa.validate_upload("em-spotify", b"\x7fELF" + b"\x00" * sa.MAX_ARTEFACT_BYTES)


# ── Install planning ────────────────────────────────────────────────────────


def test_a_missing_artefact_is_planned():
    plan = sa.plan_install(stored={"em-spotify": "aaa"}, on_device={})
    assert plan == ["em-spotify"]


def test_a_matching_md5_is_left_alone():
    plan = sa.plan_install(
        stored={"em-spotify": "aaa"}, on_device={"em-spotify": "aaa"}
    )
    assert plan == []


def test_a_rebuilt_binary_keeps_its_name_and_is_still_planned():
    # The reason presence is md5 and not the file existing: a rebuilt
    # engine has the same name, and calling it installed leaves the device
    # running something that silently disagrees with the controller. Same
    # rule as em_oww_assets.
    plan = sa.plan_install(
        stored={"em-spotify": "bbb"}, on_device={"em-spotify": "aaa"}
    )
    assert plan == ["em-spotify"]


def test_nothing_is_planned_for_an_artefact_the_controller_does_not_hold():
    # Not an error. Whether a device should have the engine is a policy
    # question for the caller; there is simply nothing to send.
    assert sa.plan_install(stored={}, on_device={}) == []
    assert sa.plan_install(stored={}, on_device={"em-spotify": "aaa"}) == []


def test_the_plan_follows_install_order():
    plan = sa.plan_install(
        stored={"em-spotify-probe": "p", "em-spotify": "e"}, on_device={}
    )
    assert plan == ["em-spotify", "em-spotify-probe"]


def test_wanted_narrows_the_plan():
    # Installing only the probe is a real operation — it is a diagnostic
    # somebody runs once, and pushing 4MB of engine alongside it is not
    # what they asked for.
    plan = sa.plan_install(
        stored={"em-spotify": "e", "em-spotify-probe": "p"},
        on_device={},
        wanted=(sa.PROBE,),
    )
    assert plan == ["em-spotify-probe"]


def test_assets_dir_sits_beside_the_database():
    got = sa.assets_dir("/data/echomuse.db")
    assert got.name == sa.ASSETS_SUBDIR
    assert got.parent.name == "data"


def test_the_device_dir_is_not_the_firmware_slot_directory():
    # /data/local/bin holds the A/B server symlink and its two slots. An
    # asset landing there would be in the way of the OTA.
    assert not sa.DEVICE_DIR.startswith("/data/local/bin")
    assert sa.DEVICE_DIR.startswith("/data/local/share")
