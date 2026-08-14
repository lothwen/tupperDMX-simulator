"""
Coverage gate: every OFL capability type resolves into the photometric model.

The repo ships four fixture definitions between them covering seven capability
types, so nothing else would ever be exercised by a real fixture. This walks all
43 of them through `resolve_state` on a synthetic one-channel fixture and checks
the value lands somewhere a renderer can read it.

The point is the *table*: a capability type added to OFL, or one somebody
forgets to dispatch, fails loudly here instead of silently rendering nothing.

Run with:
    uv run python tests/test_capability_coverage.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_state import _HANDLERS, resolve_state
import ofl_factory


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# Every capability type in OFL schema 12.5.1 (definitions.json#/capabilityTypes).
OFL_CAPABILITY_TYPES = (
    "NoFunction", "ShutterStrobe", "StrobeSpeed", "StrobeDuration", "Intensity",
    "ColorIntensity", "ColorPreset", "ColorTemperature", "Pan", "PanContinuous",
    "Tilt", "TiltContinuous", "PanTiltSpeed", "WheelSlot", "WheelShake",
    "WheelSlotRotation", "WheelRotation", "Effect", "EffectSpeed",
    "EffectDuration", "EffectParameter", "SoundSensitivity", "BeamAngle",
    "BeamPosition", "Focus", "Zoom", "Iris", "IrisEffect", "Frost",
    "FrostEffect", "Prism", "PrismRotation", "BladeInsertion", "BladeRotation",
    "BladeSystemRotation", "Fog", "FogOutput", "FogType", "Rotation", "Speed",
    "Time", "Maintenance", "Generic",
)

WHEELS = {"ch": {"slots": [
    {"type": "Open"},
    {"type": "Color", "name": "Green", "colors": ["#00ff00"]},
    {"type": "Gobo", "resource": "gobos/biohazard", "name": "Biohazard"},
    {"type": "Prism", "name": "3-facet", "facets": 3},
]}}

# capability type -> (channel properties, predicate on the state at DMX 255).
# The channel is always named "ch", so a wheel capability with no explicit
# `wheel` property addresses the wheel of the same name — OFL's own default.
EXPECTED = {
    "NoFunction": ({}, lambda s: s.intensity == 1.0 and not s.has_color),
    "ShutterStrobe": ({"shutterEffect": "Strobe", "speedStart": "1Hz",
                       "speedEnd": "15Hz"},
                      lambda s: s.strobe.effect == "Strobe"
                      and abs(s.strobe.hz - 15.0) < 0.01),
    "StrobeSpeed": ({"speedStart": "0Hz", "speedEnd": "12Hz"},
                    lambda s: abs(s.strobe.hz - 12.0) < 0.01
                    and s.strobe.effect == "Strobe"),
    "StrobeDuration": ({"durationStart": "1s", "durationEnd": "100ms"},
                       lambda s: abs(s.strobe.duration_s - 0.1) < 1e-6),
    "Intensity": ({"brightnessStart": "off", "brightnessEnd": "bright"},
                  lambda s: s.intensity == 1.0),
    "ColorIntensity": ({"color": "Red"}, lambda s: s.rgb == (255, 0, 0)),
    "ColorPreset": ({"colors": ["#00ff00"]}, lambda s: s.rgb == (0, 255, 0)),
    "ColorTemperature": ({"colorTemperature": "8000K"},
                         lambda s: abs(s.color.color_temp_k - 8000.0) < 1e-6),
    "Pan": ({"angleStart": "0deg", "angleEnd": "540deg"},
            lambda s: abs(s.motion.pan_deg - 540.0) < 1e-6),
    "PanContinuous": ({"speedStart": "slow CW", "speedEnd": "fast CW"},
                      lambda s: s.motion.pan_spin_hz > 1.0),
    "Tilt": ({"angleStart": "0deg", "angleEnd": "270deg"},
             lambda s: abs(s.motion.tilt_deg - 270.0) < 1e-6),
    "TiltContinuous": ({"speedStart": "slow CCW", "speedEnd": "fast CCW"},
                       lambda s: s.motion.tilt_spin_hz < -1.0),
    "PanTiltSpeed": ({"speedStart": "slow", "speedEnd": "fast"},
                     lambda s: s.motion.move_speed_hz > 1.0),
    "WheelSlot": ({"slotNumber": 2},
                  lambda s: s.wheels["ch"].slot.name == "Green"
                  and s.wheels["ch"].slot.colors == ((0, 255, 0),)),
    "WheelShake": ({"isShaking": "slot", "slotNumber": 3,
                    "shakeSpeedStart": "slow", "shakeSpeedEnd": "fast",
                    "shakeAngleStart": "narrow", "shakeAngleEnd": "wide"},
                   lambda s: s.wheels["ch"].shake_hz > 1.0
                   and s.wheels["ch"].shake_scope == "slot"
                   and s.wheels["ch"].slot.type == "Gobo"),
    "WheelSlotRotation": ({"speedStart": "slow CW", "speedEnd": "fast CW"},
                          lambda s: s.wheels["ch"].slot_spin_hz > 1.0),
    "WheelRotation": ({"speedStart": "slow CW", "speedEnd": "fast CW"},
                      lambda s: s.wheels["ch"].spin_hz > 1.0),
    "Effect": ({"effectName": "Rainbow", "speedStart": "slow",
                "speedEnd": "fast"},
               lambda s: s.program.name == "Rainbow" and s.program.speed_hz > 1.0),
    "EffectSpeed": ({"speedStart": "0Hz", "speedEnd": "8Hz"},
                    lambda s: abs(s.program.speed_hz - 8.0) < 1e-6),
    "EffectDuration": ({"durationStart": "10s", "durationEnd": "2s"},
                       lambda s: abs(s.program.duration_s - 2.0) < 1e-6),
    "EffectParameter": ({"parameterStart": "off", "parameterEnd": "high"},
                        lambda s: abs(s.program.parameter - 1.0) < 1e-6),
    "SoundSensitivity": ({"soundSensitivityStart": "off",
                          "soundSensitivityEnd": "high"},
                         lambda s: s.program.sensitivity == 1.0 and s.program.sound),
    "BeamAngle": ({"angleStart": "narrow", "angleEnd": "wide"},
                  lambda s: abs(s.optics.beam_angle_deg - 60.0) < 1e-6
                  and s.optics.zoom_deg == s.optics.beam_angle_deg),
    "BeamPosition": ({"horizontalAngleStart": "left", "horizontalAngleEnd": "right"},
                     lambda s: abs(s.optics.beam_offset_deg[0] - 90.0) < 1e-6),
    "Focus": ({"distanceStart": "near", "distanceEnd": "far"},
              lambda s: s.optics.focus_m > 1.0),
    "Zoom": ({"angleStart": "10deg", "angleEnd": "45deg"},
             lambda s: abs(s.optics.zoom_deg - 45.0) < 1e-6),
    "Iris": ({"openPercentStart": "closed", "openPercentEnd": "open"},
             lambda s: s.optics.iris == 1.0),
    "IrisEffect": ({"effectName": "Pulse", "speedStart": "slow",
                    "speedEnd": "fast"},
                   lambda s: s.optics.iris_effect[0] == "Pulse"
                   and s.optics.iris_effect[1] > 1.0),
    "Frost": ({"frostIntensityStart": "off", "frostIntensityEnd": "high"},
              lambda s: s.optics.frost == 1.0),
    "FrostEffect": ({"effectName": "Pulse"},
                    lambda s: s.optics.frost_effect[0] == "Pulse"),
    "Prism": ({}, lambda s: s.optics.prism_facets == 3),
    "PrismRotation": ({"speedStart": "slow CW", "speedEnd": "fast CW"},
                      lambda s: s.optics.prism_spin_hz > 1.0),
    "BladeInsertion": ({"blade": "Top", "insertionStart": "out",
                        "insertionEnd": "in"},
                       lambda s: s.optics.blades["Top"].insertion == 1.0),
    "BladeRotation": ({"blade": "Top", "angleStart": "0deg", "angleEnd": "45deg"},
                      lambda s: abs(s.optics.blades["Top"].angle_deg - 45.0) < 1e-6),
    "BladeSystemRotation": ({"angleStart": "0deg", "angleEnd": "90deg"},
                            lambda s: abs(s.optics.blade_system_deg - 90.0) < 1e-6),
    "Fog": ({"fogType": "Haze"},
            lambda s: s.atmosphere.output > 0.0 and s.atmosphere.kind == "Haze"),
    "FogOutput": ({"fogOutputStart": "off", "fogOutputEnd": "strong"},
                  lambda s: s.atmosphere.output == 1.0),
    "FogType": ({"fogType": "Fog"}, lambda s: s.atmosphere.kind == "Fog"),
    "Rotation": ({"speedStart": "slow CW", "speedEnd": "fast CW"},
                 lambda s: s.motion.rotation_hz > 1.0),
    "Speed": ({"speedStart": "0Hz", "speedEnd": "5Hz"},
              lambda s: abs(s.program.speed_hz - 5.0) < 1e-6),
    "Time": ({"timeStart": "instant", "timeEnd": "long"},
             lambda s: abs(s.program.duration_s - 5.0) < 1e-6),
    "Maintenance": ({"parameterStart": "off", "parameterEnd": "high"},
                    lambda s: s.intensity == 1.0 and s.program.name == ""),
    "Generic": ({}, lambda s: s.intensity == 1.0 and s.program.name == ""),
}


def test_the_table_covers_every_ofl_capability_type():
    print("test_the_table_covers_every_ofl_capability_type")
    _assert(len(OFL_CAPABILITY_TYPES) == 43, "OFL 12.5.1 defines 43 capability types")
    _assert(set(EXPECTED) == set(OFL_CAPABILITY_TYPES),
            "the coverage table names every one of them")
    missing = set(OFL_CAPABILITY_TYPES) - set(_HANDLERS)
    _assert(not missing, f"every type has a handler (missing: {sorted(missing)})")
    unknown = set(_HANDLERS) - set(OFL_CAPABILITY_TYPES)
    _assert(not unknown, f"no handler invents a type (extra: {sorted(unknown)})")


def test_every_capability_type_resolves():
    print("test_every_capability_type_resolves")
    for cap_type in OFL_CAPABILITY_TYPES:
        props, predicate = EXPECTED[cap_type]
        fdef = ofl_factory.fdef(
            channels={"ch": ofl_factory.channel(cap_type, **props)},
            wheels=WHEELS)
        state = resolve_state(fdef, [255])
        _assert(predicate(state), f"{cap_type} reaches the state")


# Every value OFL's shutterEffect enum allows. The renderer draws each of them
# differently, so losing one here means a fixture silently stops flashing.
SHUTTER_EFFECTS = ("Open", "Closed", "Strobe", "Pulse", "RampUp", "RampDown",
                   "RampUpDown", "Lightning", "Spikes", "Burst")


def test_every_shutter_effect_survives():
    print("test_every_shutter_effect_survives")
    for effect in SHUTTER_EFFECTS:
        fdef = ofl_factory.fdef(channels={
            "d": ofl_factory.channel("Intensity"),
            "s": ofl_factory.channel("ShutterStrobe", shutterEffect=effect,
                                     speed="5Hz")})
        state = resolve_state(fdef, [255, 255])
        _assert(state.strobe.effect == effect, f"{effect} reaches the state")
        if effect == "Closed":
            _assert(state.intensity == 0.0, "a closed shutter emits nothing")
        else:
            _assert(state.intensity == 1.0, f"{effect} still passes the dimmer")


def main():
    test_the_table_covers_every_ofl_capability_type()
    test_every_capability_type_resolves()
    test_every_shutter_effect_survives()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
