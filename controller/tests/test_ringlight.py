"""em_ringlight: the ring as a light, from the ESPHome light message's edges."""
from types import SimpleNamespace as NS

import pytest

import em_ringlight as R


def cmd(**kw):
    base = dict(has_state=False, state=False, has_brightness=False, brightness=0.0,
                has_color_brightness=False, color_brightness=0.0, has_rgb=False,
                red=0.0, green=0.0, blue=0.0, has_effect=False, effect="")
    base.update(kw)
    return NS(**base)


def test_fields_without_their_flag_are_left_alone():
    s = R.RingLight(on=True, red=0.2, green=0.4, blue=0.6, brightness=0.5, effect="Pulse")
    out = R.apply_command(s, cmd(red=1.0, brightness=1.0, state=False, effect="Spin"))
    assert out == s


def test_turn_on_with_a_colour_and_an_effect():
    out = R.apply_command(R.RingLight(), cmd(has_state=True, state=True, has_rgb=True,
                                             red=1.0, green=0.0, blue=0.0,
                                             has_effect=True, effect="Fast pulse"))
    assert out.on and (out.red, out.green, out.blue) == (1.0, 0.0, 0.0)
    assert out.effect == "Fast pulse"


def test_unknown_effect_is_not_stored():
    out = R.apply_command(R.RingLight(on=True, effect="Pulse"),
                          cmd(has_effect=True, effect="Rainbow"))
    assert out.effect == R.NO_EFFECT


@pytest.mark.parametrize("name", ["", "None", "none"])
def test_none_or_empty_clears_the_effect(name):
    out = R.apply_command(R.RingLight(on=True, effect="Spin"), cmd(has_effect=True, effect=name))
    assert out.effect == R.NO_EFFECT


def test_the_list_leads_with_none_as_an_esphome_light_does():
    assert R.EFFECT_NAMES[0] == "None"
    assert R.EFFECT_NAMES[1:] == list(R.EFFECTS)


@pytest.mark.parametrize("v", [-1.0, 2.5, 7.0])
def test_values_outside_zero_to_one_are_clamped(v):
    out = R.apply_command(R.RingLight(), cmd(has_brightness=True, brightness=v,
                                             has_rgb=True, red=v, green=v, blue=v))
    assert 0.0 <= out.brightness <= 1.0
    assert all(0.0 <= c <= 1.0 for c in (out.red, out.green, out.blue))


def test_colour_is_scaled_by_both_brightnesses():
    s = R.RingLight(on=True, red=1.0, green=0.5, blue=0.0, brightness=0.5, color_brightness=0.5)
    assert s.rgb255() == (64, 32, 0)


def test_off_and_black_paint_nothing():
    assert R.to_anim(R.RingLight(on=False)) is None
    assert R.to_anim(R.RingLight(on=True, brightness=0.0)) is None


def test_no_effect_is_a_solid_ring():
    a = R.to_anim(R.RingLight(on=True, red=1.0, green=0.0, blue=0.0))
    assert a["pattern"] == "solid" and a["colors"] == [[255, 0, 0]]
    assert a["ttlSec"] == R.TTL_S


def test_every_effect_makes_a_spec_the_firmware_knows():
    # The animator's patterns (internal/server/animator.go); anything else
    # clears the ring, so an effect added here must be one of them.
    known = {"solid", "spin", "rotate", "pulse"}
    for name in R.EFFECTS:
        a = R.to_anim(R.RingLight(on=True, effect=name, red=0.0, green=1.0, blue=0.0))
        assert a["pattern"] in known, name
        assert a["periodMs"] > 0
        assert a["ttlSec"] > R.REFRESH_S, "refresh must land inside the dead-man"


def test_spin_has_a_dimmer_trail():
    a = R.to_anim(R.RingLight(on=True, effect="Spin", red=1.0, green=0.0, blue=0.0))
    head, trail = a["colors"]
    assert head == [255, 0, 0] and trail == [64, 0, 0]


def test_the_effect_list_matches_the_firmware_patterns_in_the_source():
    # Held against the Go source so renaming a pattern there fails here.
    import pathlib, re
    src = (pathlib.Path(__file__).resolve().parents[2] / "device" / "internal" / "server" / "animator.go").read_text()
    for _, (pattern, _) in R.EFFECTS.items():
        assert re.search(rf'"{pattern}"', src), pattern


# ── wiring guards: the suite cannot import em_esphome or em_controller ────────

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ESP = (_ROOT / "em_esphome.py").read_text()
_CTL = (_ROOT / "em_controller.py").read_text()


def test_entity_keys_are_unique_and_the_light_is_appended():
    keys = dict(re.findall(r"^([A-Z_]+_KEY)\s*=\s*(\d+)", _ESP, re.M))
    assert keys["RING_LIGHT_KEY"] == "5"
    assert len(set(keys.values())) == len(keys), "two entities share a key"


def test_the_light_is_only_advertised_for_firmware_that_animates_locally():
    assert re.search(r'def _ring_light_capable\(self\)[^\n]*\n(?:\s*#[^\n]*\n)*\s*return self\._device_has\("led_anim"\)', _ESP)
    assert "if self._ring_light_capable:\n                yield api_pb2.ListEntitiesLightResponse(" in _ESP


def test_the_state_goes_to_ha_before_the_ring_is_touched():
    body = _ESP[_ESP.index("isinstance(msg, api_pb2.LightCommandRequest)"):]
    body = body[:body.index("if isinstance(msg, api_pb2.SubscribeVoiceAssistantRequest)")]
    assert body.index("yield self._ring_light_msg()") < body.index("srv._set_ring_light(")


def test_a_command_for_another_key_is_ignored():
    assert "msg.key != RING_LIGHT_KEY" in _ESP


def test_every_ring_clear_repaints_the_light():
    leds_off = _CTL[_CTL.index("async def leds_off("):]
    leds_off = leds_off[:leds_off.index("# ─── The ring as a Home Assistant light")]
    assert "await paint_ring_light(device)" in leds_off


def test_the_light_never_paints_over_a_turn_or_a_ringing_timer():
    idle = _CTL[_CTL.index("def _ring_idle("):]
    idle = idle[:idle.index("async def paint_ring_light")]
    for owner in ("listening", "thinking", "speaking", "timer_alarm_ringing"):
        assert owner in idle, owner
    paint = _CTL[_CTL.index("async def paint_ring_light"):]
    paint = paint[:paint.index("async def _ring_light_loop")]
    assert "_ring_idle(device)" in paint


def test_an_off_command_clears_only_a_ring_the_light_was_painting():
    apply = _CTL[_CTL.index("async def apply_ring_light"):]
    off = apply[:apply.index("if device.device_id not in _ring_light_tasks")]
    assert "_ring_idle(device)" in off


def test_the_refresher_stops_when_the_device_goes():
    assert "stop_ring_light(device)\n        await esphome.device_disconnected" in _CTL
