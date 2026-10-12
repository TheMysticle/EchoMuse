"""
em_ringlight.py — the Echo's LED ring as a Home Assistant light (pure logic)

HA integrations that want to signal something on a satellite (an alarm that
turns the ring red, a doorbell that pulses it) do it through a light entity.
The ring is not one, so it was out of reach. This gives it the ESPHome light
shape: on/off, colour, brightness and a short list of effects.

The ring is the device's status display, so the light is a BACKGROUND layer.
It never paints over a voice turn, a ringing timer or the mute ring:
`controller` repaints it when the ring goes idle, and the device's own rules
(mute and volume win over any controller frame) are untouched. That is also
why nothing here knows about turns; it only turns a light state into the
`led_anim` spec the firmware already renders.

Effects are named, not parametrised: ESPHome's light model has no effect
arguments, and HA's `light.turn_on` can only pick one by name. Each maps to a
pattern the firmware's animator already has (internal/server/animator.go).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Effect name -> (pattern, periodMs). Order is the order HA lists them in.
# "Pulse" is a sinusoidal throb whose periodMs is the full cycle; "Spin" is a
# head with a trail whose periodMs is the frame interval.
EFFECTS: dict[str, tuple[str, int]] = {
    "Pulse":      ("pulse", 1200),
    "Slow pulse": ("pulse", 3000),
    "Fast pulse": ("pulse", 500),
    "Spin":       ("spin", 70),
}
EFFECT_NAMES = list(EFFECTS)

# HA shows "None" when no effect is active; ESPHome sends an empty string.
NO_EFFECT = ""

# The spin's trail is the head colour at this fraction.
TRAIL = 0.25

# How long the device keeps painting without a refresh, and how often the
# controller refreshes. The TTL is the dead-man that clears the ring if the
# controller dies, so it stays short; the refresh is well inside it.
TTL_S = 10
REFRESH_S = 4


@dataclass
class RingLight:
    on: bool = False
    # ESPHome keeps colour and brightness separately, 0.0 to 1.0, and HA
    # composes them. White until something chooses a colour.
    red: float = 1.0
    green: float = 1.0
    blue: float = 1.0
    brightness: float = 1.0
    color_brightness: float = 1.0
    effect: str = NO_EFFECT

    def rgb255(self) -> tuple[int, int, int]:
        """The colour as the ring should show it: colour x brightness."""
        k = self.brightness * self.color_brightness
        return tuple(max(0, min(255, round(c * k * 255)))
                     for c in (self.red, self.green, self.blue))


def _unit(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def apply_command(state: RingLight, msg) -> RingLight:
    """
    The state after a LightCommandRequest. `msg` needs the has_* / value
    attribute pairs of that message; fields without their has_ flag are left
    alone, as ESPHome does.

    Turning on with no colour keeps the last one, and an unknown effect name
    is ignored rather than stored, so the entity never reports an effect it
    cannot show.
    """
    out = RingLight(**state.__dict__)
    if msg.has_state:
        out.on = bool(msg.state)
    if msg.has_brightness:
        out.brightness = _unit(msg.brightness)
    if msg.has_color_brightness:
        out.color_brightness = _unit(msg.color_brightness)
    if msg.has_rgb:
        out.red, out.green, out.blue = _unit(msg.red), _unit(msg.green), _unit(msg.blue)
    if msg.has_effect:
        out.effect = msg.effect if msg.effect in EFFECTS else NO_EFFECT
    # HA sends a colour or brightness change WITHOUT has_state when the light
    # is already on, and with state=true when it is not; a change to colour
    # alone on a light that is off does not turn it on (ESPHome's rule).
    return out


def to_anim(state: RingLight) -> dict | None:
    """The led_anim spec for this state, or None when the ring should be dark."""
    if not state.on:
        return None
    r, g, b = state.rgb255()
    if (r, g, b) == (0, 0, 0):
        return None
    spec = EFFECTS.get(state.effect)
    if spec is None:
        return {"pattern": "solid", "colors": [[r, g, b]], "periodMs": 0, "ttlSec": TTL_S}
    pattern, period = spec
    colors = [[r, g, b]]
    if pattern == "spin":
        colors.append([round(c * TRAIL) for c in (r, g, b)])
    return {"pattern": pattern, "colors": colors, "periodMs": period, "ttlSec": TTL_S}
