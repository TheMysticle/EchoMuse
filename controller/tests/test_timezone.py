"""The ack's zone name: only a name zoneinfo can load, from the sources in order."""
import os

import em_timezone as tz


def test_tz_env_wins_and_a_leading_colon_is_the_posix_form(tmp_path):
    f = tmp_path / "timezone"
    f.write_text("America/New_York\n")
    assert tz.local_zone_name({"TZ": "Europe/Warsaw"}, str(f), "/nonexistent") == "Europe/Warsaw"
    assert tz.local_zone_name({"TZ": ":Europe/Warsaw"}, str(f), "/nonexistent") == "Europe/Warsaw"


def test_etc_timezone_is_used_when_tz_is_absent_or_unusable(tmp_path):
    f = tmp_path / "timezone"
    f.write_text("Asia/Kolkata\n")
    assert tz.local_zone_name({}, str(f), "/nonexistent") == "Asia/Kolkata"
    assert tz.local_zone_name({"TZ": "Not/AZone"}, str(f), "/nonexistent") == "Asia/Kolkata"


def test_localtime_symlink_into_zoneinfo(tmp_path):
    zi = tmp_path / "usr/share/zoneinfo/Europe"
    zi.mkdir(parents=True)
    (zi / "Warsaw").write_text("x")
    link = tmp_path / "localtime"
    os.symlink(zi / "Warsaw", link)
    assert tz.local_zone_name({}, str(tmp_path / "missing"), str(link)) == "Europe/Warsaw"


def test_nothing_known_is_none_never_a_guess(tmp_path):
    assert tz.local_zone_name({}, str(tmp_path / "a"), str(tmp_path / "b")) is None
    assert tz.local_zone_name({"TZ": ""}, str(tmp_path / "a"), str(tmp_path / "b")) is None


def test_names_that_are_paths_or_not_zones_are_refused(tmp_path):
    for bad in ("/etc/passwd", "../../etc/passwd", "Europe/../../x", "not a zone", "\x00"):
        assert tz.local_zone_name({"TZ": bad}, str(tmp_path / "a"), str(tmp_path / "b")) is None


def test_windows_style_and_posix_rule_strings_are_not_sent(tmp_path):
    # TZ=CET-1CEST,M3.5.0,M10.5.0/3 is valid POSIX but not an IANA name.
    assert tz.local_zone_name({"TZ": "CET-1CEST,M3.5.0,M10.5.0/3"}, str(tmp_path / "a"), str(tmp_path / "b")) is None


def test_ack_carries_the_zone_only_when_one_is_known(monkeypatch):
    monkeypatch.setattr(tz, "LOCAL_ZONE", "Europe/Warsaw")
    assert tz.ack_fields() == {"tz": "Europe/Warsaw"}
    monkeypatch.setattr(tz, "LOCAL_ZONE", None)
    assert tz.ack_fields() == {}
