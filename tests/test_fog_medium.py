"""
Characterization test: what a fog machine puts in the air, and for how long.

The thing this pins is the difference between smoke and an effect switching off.
Before this, a plume was a function of the current DMX value: close the channel
and the stage was clear the same frame. Real smoke does not work that way — what
the machine already made is still hanging there — so each parcel is spawned once
and then lives its own life, and *that* is what these tests are about.

The other half is the hazer/smoke split. Both machines make fog; a hazer makes a
thin layer that fills the room and a smoke machine makes dense local parcels,
and OFL says which is which two different ways (a `fogType` capability, or just
the fixture's category). Both have to work.

GL-free and Qt-light on purpose: this is the model, not the rendering.

Run with:
    uv run python tests/test_fog_medium.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.fixture_library import resolve_fixture
from ui.stage import puffs, renderer_for
from ui.stage.atmosphere import Atmosphere
from ui.stage.light import BLOB
from ui.stage.profiles.atmospheric import HazerFixture, SmokeFixture
import ofl_factory


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


def _fog_fdef(category):
    """A one-channel machine of `category` that declares no `fogType`.

    Deliberately silent about its type: that is the case where the category is
    the only evidence, and the case that used to be read as haze whatever the
    fixture said it was.
    """
    return ofl_factory.fdef(
        categories=(category,),
        channels={"f": ofl_factory.channel(
            "FogOutput", fogOutputStart="off", fogOutputEnd="strong")})


def _body(fdef, **kwargs):
    return renderer_for(fdef)(fdef, 0.0, 0.0, **kwargs)


def _run(field, seconds, output, step=1.0 / 30.0, start=0.0):
    """Drive a field for `seconds` at `output`, at the stage's clock rate."""
    now = start
    end = start + seconds
    while now < end:
        field.update(now, output, (0.0, 0.0))
        now += step
    return now


# ---------------------------------------------------------------------------
# The field itself
# ---------------------------------------------------------------------------

def test_a_machine_shows_something_on_its_first_frame():
    print("test_a_machine_shows_something_on_its_first_frame")
    field = puffs.PuffField(puffs.SMOKE)
    field.update(0.0, 1.0, (0.0, 0.0))
    _assert(len(field) == 1,
            "a machine switched on has fog at the nozzle immediately, not "
            "after a spawn interval")
    _assert(all(p.density > 0.0 for p in field),
            "and it is visible on that first frame")


def test_smoke_outlives_the_channel_that_made_it():
    print("test_smoke_outlives_the_channel_that_made_it")
    life = puffs.SMOKE.lifetime_s
    field = puffs.PuffField(puffs.SMOKE)

    # Run it for four seconds, so the parcels in the air were born across a
    # spread of times rather than all at once.
    now = _run(field, 4.0, 1.0)
    running = len(field)
    _assert(running > 1, f"a machine left running fills the air ({running})")

    # Close the channel. This is the whole point: the stage does not clear.
    now = _run(field, 1.0, 0.0, start=now)
    _assert(len(field) == running,
            "closing the channel does not remove what is already in the air")

    # Past the oldest parcel's lifetime but not the youngest's: the column
    # should be thinning from the top rather than vanishing.
    now = _run(field, (life + 2.0) - now, 0.0, start=now)
    _assert(0 < len(field) < running,
            f"it thins out as the parcels age ({len(field)} of {running} left)")

    # And a full lifetime past the last one made.
    _run(field, 2.0, 0.0, start=4.0 + life + 1.0)
    _assert(len(field) == 0,
            f"and is gone a lifetime after the last parcel ({life}s)")
    _assert(field.settled, "the field reports itself settled, so the clock can stop")


def test_a_parcel_rises_spreads_and_thins():
    print("test_a_parcel_rises_spreads_and_thins")
    field = puffs.PuffField(puffs.SMOKE)
    field.update(0.0, 1.0, (0.0, 0.0))
    first = list(field)[0]

    # Far enough in to be past the attack, not so far as to have retired.
    field.update(puffs.SMOKE.lifetime_s * 0.75, 0.0, (0.0, 0.0))
    later = list(field)[0]

    _assert(later.y < first.y, "it rises (stage up is -Y)")
    _assert(later.radius > first.radius, "it spreads")
    _assert(later.density < first.density, "and it thins as it does")
    _assert(later.z_extent > first.z_extent, "it spreads in depth as well as across")


def test_a_machine_fills_the_stage_rather_than_drawing_a_column():
    print("test_a_machine_fills_the_stage_rather_than_drawing_a_column")
    # The report this pins: "the haze and smoke machines should generate more
    # smoke and spread all over the stage". Before, every parcel left the nozzle
    # on essentially the same line and wandered ±125px over its whole life, so
    # what a machine made was a stripe above itself. A stage is ~1536px across.
    for spec, name, wanted in ((puffs.HAZER, "a hazer", 1200.0),
                               (puffs.SMOKE, "a smoke machine", 700.0)):
        field = puffs.PuffField(spec)
        _run(field, spec.lifetime_s, 1.0)
        xs = [p.x for p in field]
        span = max(xs) - min(xs)
        _assert(span >= wanted,
                f"{name} left running covers {span:.0f}px across the stage "
                f"(wanted at least {wanted:.0f})")
        # And it is a bank of many parcels rather than a handful of blobs: the
        # texture comes from them overlapping, so the count is the look.
        _assert(len(field) >= 40,
                f"{name} holds {len(field)} parcels in the air")


def test_a_parcel_stops_rising():
    print("test_a_parcel_stops_rising")
    # Fog is a cold, heavy fluid being pushed by a fan, not a balloon. A parcel
    # that kept climbing at a constant rate would file off the top of the
    # picture in formation, which is the most obvious tell there is.
    spec = puffs.SMOKE
    field = puffs.PuffField(spec)
    field.update(0.0, 1.0, (0.0, 0.0))

    heights = []
    for fraction in (0.25, 0.5, 0.75):
        field.update(spec.lifetime_s * fraction, 0.0, (0.0, 0.0))
        heights.append(-list(field)[0].y)          # stage up is -Y

    first_leg = heights[1] - heights[0]
    second_leg = heights[2] - heights[1]
    _assert(second_leg < first_leg,
            f"it climbs {first_leg:.0f}px in the second quarter of its life "
            f"and only {second_leg:.0f}px in the third")
    # Its buoyancy is worth a fixed climb, and turbulence can push it a little
    # either side of that. Nothing else lifts it, so it hangs.
    ceiling = spec.rise_px_s * spec.rise_decay_s + spec.turbulence_px * 0.35
    _assert(heights[-1] < ceiling,
            f"and hangs below {ceiling:.0f}px rather than climbing out of "
            f"the picture (reached {heights[-1]:.0f}px)")


def test_parcels_do_not_all_look_the_same():
    print("test_parcels_do_not_all_look_the_same")
    # "It needs some kind of texture, not a white filter." Half of that is the
    # renderer, but the field has to give it something to vary on: a bank of
    # identical circles is identifiable as circles however many are stacked.
    field = puffs.PuffField(puffs.SMOKE)
    _run(field, 6.0, 1.0)
    parcels = list(field)
    _assert(len({p.variant % 6 for p in parcels}) > 1,
            "parcels are dealt different shapes to wear")
    _assert(len({round(p.spin, 3) for p in parcels}) == len(parcels),
            "and each is turned differently, so no two are the same stamp")
    # A parcel keeps its shape for life — flickering between them would read as
    # noise on the fixture rather than as smoke.
    before = {id_: p.variant for id_, p in enumerate(parcels)}
    field.update(8.0, 0.0, (0.0, 0.0))
    after = {id_: p.variant for id_, p in enumerate(list(field))}
    _assert(all(after[i] == before[i] for i in after if i in before),
            "and keeps it as it ages")


def test_the_ring_bounds_a_machine_left_running():
    print("test_the_ring_bounds_a_machine_left_running")
    field = puffs.PuffField(puffs.SMOKE)
    _run(field, 120.0, 1.0)
    _assert(len(field) <= puffs.SMOKE.capacity,
            f"two minutes at full is still at most {puffs.SMOKE.capacity} "
            f"parcels, not thousands ({len(field)})")


def test_a_hazer_makes_a_thinner_medium_than_a_smoke_machine():
    print("test_a_hazer_makes_a_thinner_medium_than_a_smoke_machine")
    haze = puffs.PuffField(puffs.HAZER)
    smoke = puffs.PuffField(puffs.SMOKE)
    _run(haze, 6.0, 1.0)
    _run(smoke, 6.0, 1.0)

    _assert(max(p.density for p in haze) < max(p.density for p in smoke),
            "at the same DMX, a hazer's parcels are thinner")
    _assert(len(haze) < len(smoke),
            f"and there are fewer of them ({len(haze)} vs {len(smoke)})")
    _assert(puffs.HAZER.max_output < 1.0,
            f"because a hazer cannot be driven past {puffs.HAZER.max_output} "
            "however hard its channel is pushed")


def test_a_hazers_ceiling_is_reached_below_full_dmx():
    print("test_a_hazers_ceiling_is_reached_below_full_dmx")
    ceiling = puffs.HAZER.max_output
    _assert(puffs.HAZER.throttle(ceiling) == 1.0,
            f"a hazer is flat out at output {ceiling}")
    _assert(puffs.HAZER.throttle(1.0) == 1.0,
            "and pushing past that changes nothing — it is already flat out")
    _assert(puffs.HAZER.throttle(0.0) == 0.0, "closed is closed")
    _assert(puffs.SMOKE.throttle(ceiling) < 1.0,
            "the same output on a smoke machine is nowhere near its limit")


def test_a_stalled_frame_does_not_teleport_the_column():
    print("test_a_stalled_frame_does_not_teleport_the_column")
    field = puffs.PuffField(puffs.SMOKE)
    field.update(0.0, 1.0, (0.0, 0.0))
    # A modal dialog, a slow decode: the clock jumps. One step's worth is all
    # that may be integrated, or the whole column shoots off the stage.
    field.update(30.0, 1.0, (0.0, 0.0))
    _assert(len(field) <= 2,
            f"a 30-second gap does not spawn a ring's worth ({len(field)})")


# ---------------------------------------------------------------------------
# What the renderers see
# ---------------------------------------------------------------------------

def test_a_plume_emits_medium_not_light(qapp):
    print("test_a_plume_emits_medium_not_light")
    body = _body(_fog_fdef("Smoke"))
    try:
        body.update_dmx(bytes([255]))
        blobs = body.lights.of_kind(BLOB)
        _assert(blobs, f"a running machine emits parcels ({len(blobs)})")
        _assert(len(blobs) == len(body.emission.field),
                "one per live parcel, and no others")
        for blob in blobs:
            _assert(blob.density > 0.0, f"density {blob.density:.4f} > 0")
            _assert(blob.radius > 0.0, "and a radius across the picture")
            _assert(blob.z_extent > 0.0, "and one in depth")
        # Every field finite, whatever it is carrying.
        for blob in blobs:
            for name, value in zip(blob._fields, blob):
                flat = value if isinstance(value, tuple) else (value,)
                for one in flat:
                    _assert_finite(one, name)
    finally:
        body.cleanup()


def _assert_finite(value, name):
    if value != value or value in (float("inf"), float("-inf")):
        raise AssertionError(f"{name} is not finite ({value})")


def test_both_renderers_read_the_same_parcels(qapp):
    print("test_both_renderers_read_the_same_parcels")
    body = _body(_fog_fdef("Smoke"))
    try:
        body.update_dmx(bytes([255]))
        plume = body.emission.plume
        _assert(plume._puffs is body.emission.field,
                "the raster plume paints the field itself, not a copy of it — "
                "so the two paths cannot disagree about where the smoke is")
        _assert(not plume.boundingRect().isEmpty(),
                "and it bounds itself to what is actually in the air")
    finally:
        body.cleanup()


def test_the_field_is_released_with_the_body(qapp):
    print("test_the_field_is_released_with_the_body")
    body = _body(_fog_fdef("Smoke"))
    body.update_dmx(bytes([255]))
    _assert(len(body.emission.field) > 0, "the machine is running")
    body.cleanup()
    _assert(len(body.emission.field) == 0,
            "and a removed fixture takes its smoke with it")


# ---------------------------------------------------------------------------
# Which kind of fog, and how the stage weighs it
# ---------------------------------------------------------------------------

def test_the_category_names_the_fog_a_definition_does_not(qapp):
    print("test_the_category_names_the_fog_a_definition_does_not")
    # Neither definition declares an OFL `fogType`, so the category is the only
    # evidence there is. Before the split, both of these were weighed as haze.
    _assert(HazerFixture.default_fog_kind == "Haze", "a Hazer makes haze")
    _assert(SmokeFixture.default_fog_kind == "Fog", "a Smoke machine makes fog")

    for category, expected in (("Hazer", "Haze"), ("Smoke", "Fog")):
        air = Atmosphere(baseline=0.0)
        body = _body(_fog_fdef(category))
        body.set_atmosphere(air)
        try:
            body.update_dmx(bytes([255]))
            weighted = air.target
        finally:
            body.cleanup()
        # `Atmosphere` weighs Fog below Haze: fog stays where it was made, haze
        # fills the room, and it is the room that makes everyone's beam visible.
        expected_weight = 1.0 if expected == "Haze" else 0.6
        _assert(abs(weighted - expected_weight) < 1e-6,
                f"a {category} at full contributes {expected_weight} "
                f"({expected}), got {weighted:.3f}")


def test_a_declared_fog_type_still_wins(qapp):
    print("test_a_declared_fog_type_still_wins")
    # The fallback is only for a definition that says nothing. A machine that
    # declares what it makes is believed, whatever box it ships in.
    fdef = ofl_factory.fdef(
        categories=("Hazer",),
        channels={
            "f": ofl_factory.channel("FogOutput", fogOutputStart="off",
                                     fogOutputEnd="strong"),
            "t": ofl_factory.channel("FogType", fogType="Fog"),
        })
    air = Atmosphere(baseline=0.0)
    body = _body(fdef)
    body.set_atmosphere(air)
    try:
        body.update_dmx(bytes([255, 255]))
        _assert(abs(air.target - 0.6) < 1e-6,
                f"a Hazer declaring fogType Fog is weighed as fog ({air.target:.3f})")
    finally:
        body.cleanup()


def test_the_shipped_machines_resolve(qapp):
    print("test_the_shipped_machines_resolve")
    for name, profile, spec in (("hazer", HazerFixture, puffs.HAZER),
                                ("smoke-machine", SmokeFixture, puffs.SMOKE)):
        fdef = resolve_fixture(name)
        if fdef is None:
            print(f"  skip: {name} is not installed")
            continue
        body = _body(fdef)
        try:
            _assert(isinstance(body, profile),
                    f"{name} is drawn as {profile.__name__}")
            _assert(body.emission.field.spec is spec,
                    f"and makes {spec is puffs.SMOKE and 'smoke' or 'haze'}")
            body.update_dmx(bytes([255] * fdef.channel_count))
            _assert(body.lights.of_kind(BLOB),
                    f"{name} at full puts something in the air")
            body.update_dmx(bytes([0] * fdef.channel_count))
            _assert(body.lights.of_kind(BLOB),
                    "and closing it does not clear the stage")
        finally:
            body.cleanup()


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    test_a_machine_shows_something_on_its_first_frame()
    test_smoke_outlives_the_channel_that_made_it()
    test_a_parcel_rises_spreads_and_thins()
    test_a_machine_fills_the_stage_rather_than_drawing_a_column()
    test_a_parcel_stops_rising()
    test_parcels_do_not_all_look_the_same()
    test_the_ring_bounds_a_machine_left_running()
    test_a_hazer_makes_a_thinner_medium_than_a_smoke_machine()
    test_a_hazers_ceiling_is_reached_below_full_dmx()
    test_a_stalled_frame_does_not_teleport_the_column()
    test_a_plume_emits_medium_not_light(app)
    test_both_renderers_read_the_same_parcels(app)
    test_the_field_is_released_with_the_body(app)
    test_the_category_names_the_fog_a_definition_does_not(app)
    test_a_declared_fog_type_still_wins(app)
    test_the_shipped_machines_resolve(app)
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
