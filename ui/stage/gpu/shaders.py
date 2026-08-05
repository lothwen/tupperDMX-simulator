# ABOUTME: The GLSL the light layer runs — an instanced pass that accumulates
# ABOUTME: every light in linear HDR, a volumetric fog march, and an ACES resolve.
"""Shaders.

Five programs, in the order they run.

**LIGHT** draws one quad per `LightPrimitive` into a float framebuffer with
`GL_ONE, GL_ONE`, so **light adds** — the thing the raster path cannot do, and
the reason two beams crossing there make mud instead of a brighter patch.
Nothing is clamped on the way in; two blinders lighting the same patch really do
land at twice full scale.

**MEDIUM** rasterises the BLOB primitives — parcels of smoke and haze — into a
small two-channel map: how much medium is in the air above each part of the
picture, and how deep it reaches. Additive, because density adds.

**FOG** is the volumetric march, and the one place this renderer pretends to
have a third dimension. It gets away with it because the projection really is
orthographic (`u_scene_to_clip` is affine, with no perspective divide), so a
pixel's view ray is exactly `(scene_x, scene_y, -camera_z + t)` — the scene
coordinate under the pixel *is* its world XY, exactly rather than
approximately. Every light primitive is lifted into that space, marched through
the density field with Beer-Lambert extinction and a Henyey-Greenstein phase
function, and accumulated. One quad per light, bounded to its own reach, so the
cost follows lit screen area rather than light count.

**TEMPORAL** averages that march with the previous frame's, because the ray
starts are dithered and a single frame is noisy.

**RESOLVE** turns the lot into something a screen can show: exposure, then the
ACES filmic curve, which is what makes "brighter than white" a believable
highlight that rolls off toward white instead of clipping per channel and
shifting hue on the way.

Kept as Python strings rather than files under `assets/`: a frozen build that
cannot find its shaders is a runtime failure on someone else's machine, and
there is nothing to gain from being able to edit them without a restart.
"""

from ui.stage.frame import FORESHORTEN_FLOOR

# The context is desktop GL (see ui/stage/gpu/layer.py — the surface format
# forces it, because Wayland/EGL otherwise hands out GL ES, where none of this
# compiles). 330 is the lowest version with the instancing this relies on.
_HEADER = "#version 330\n"

# Primitive kinds, mirroring ui/stage/light.py. Duplicated as GLSL constants
# because a shader cannot import; `layer.py` asserts they still agree.
_KINDS = """
const float KIND_POINT = 0.0;
const float KIND_CONE  = 1.0;
const float KIND_QUAD  = 2.0;
const float KIND_LINE  = 3.0;
const float KIND_BLOB  = 4.0;
"""

# ---------------------------------------------------------------------------
# Pass 1 — accumulate
# ---------------------------------------------------------------------------

LIGHT_VERT = _HEADER + _KINDS + """
in vec2 a_corner;          // unit quad, -1..1

// One set per primitive (instanced).
in float i_kind;
in vec2  i_pos;
in vec3  i_rgb;
in float i_intensity;
in float i_radius;
in float i_axis_deg;
in float i_length;
in float i_half_width;
in float i_softness;
in float i_hardness;
in float i_foreshorten;
in vec2  i_size;

uniform mat4 u_scene_to_clip;
uniform float u_glow_reach;   // how far a lamp's halo spreads, in lamp radii
uniform float u_max_glow;     // and a ceiling on that, in scene units

out vec2 v_local;             // position within the primitive's own frame
flat out float v_kind;
flat out vec3  v_rgb;
flat out float v_intensity;
flat out float v_softness;
flat out float v_hardness;
flat out float v_foreshorten;
flat out vec3  v_box;         // QUAD only: (half width, half height, halo)

// A cone's quad is as wide as its tip for its whole length, with a margin for
// the soft edge; the fragment shader narrows it back down per slice.
const float CONE_MARGIN = 1.35;

void main() {
    vec2 scene;

    if (i_kind == KIND_QUAD) {
        // The cell's own rectangle, grown by however far its light carries.
        vec2 half_size = i_size * 0.5;
        vec2 centre = i_pos + half_size;
        float halo = clamp(i_radius, 0.0, u_max_glow);
        scene = centre + a_corner * (half_size + halo);
        // Scene units from the centre, so the fragment can measure its
        // distance from the face itself rather than from the padded quad.
        v_local = a_corner * (half_size + halo);
        v_box = vec3(half_size, halo);
    } else if (i_kind == KIND_CONE || i_kind == KIND_LINE) {
        float rad = radians(i_axis_deg);
        vec2 dir = vec2(cos(rad), sin(rad));
        vec2 nrm = vec2(-dir.y, dir.x);
        float t = a_corner.x * 0.5 + 0.5;            // 0 at the lens, 1 at the tip
        float half_width = max(i_half_width, 0.5) * CONE_MARGIN;
        scene = i_pos + dir * (t * i_length) + nrm * (a_corner.y * half_width);
        v_local = vec2(t, a_corner.y * CONE_MARGIN);
    } else {
        // POINT and BLOB: a square centred on the source. Capped, or a big
        // fixture's halo becomes a disc the size of the whole picture.
        float reach = i_radius * (i_kind == KIND_BLOB ? 1.0 : u_glow_reach);
        reach = clamp(reach, 1.0, u_max_glow);
        scene = i_pos + a_corner * reach;
        v_local = a_corner;
        v_box = vec3(0.0);
    }

    v_kind = i_kind;
    v_rgb = i_rgb;
    v_intensity = i_intensity;
    v_softness = i_softness;
    v_hardness = i_hardness;
    v_foreshorten = i_foreshorten;
    gl_Position = u_scene_to_clip * vec4(scene, 0.0, 1.0);
}
"""

LIGHT_FRAG = _HEADER + _KINDS + """
in vec2 v_local;
flat in float v_kind;
flat in vec3  v_rgb;
flat in float v_intensity;
flat in float v_softness;
flat in float v_hardness;
flat in float v_foreshorten;
flat in vec3  v_box;

uniform float u_haze;        // 0..1, the stage's air
uniform float u_fog_active;  // 1 when the volumetric march owns the shaft

out vec4 fragColor;

// How much of a beam this pass still draws once the fog march is running.
// The march integrates the shaft through the air it is actually in, so drawing
// the sprite as well would double it — but the near field is the lens itself,
// which is a hot spot on the fixture rather than light in the room, and that
// still belongs here.
const float FOG_CORE = 0.18;

// A parcel of medium, drawn as a faint additive puff, for when the fog march
// is NOT running. `intensity` is a density now rather than the old plume's
// opacity, so this is what maps one onto the other.
const float BLOB_GLOW = 0.6;

// How much brighter a beam scatters when it points at the viewer.
//
// This is the term that makes a flat front elevation read as a stage with
// depth. A shaft crossing the picture shows you the little light it scatters
// sideways; the same shaft aimed down the lens puts its whole output into your
// eye. Henyey-Greenstein is the standard phase function for exactly this, and
// `foreshorten` (1 across the picture, 0 toward the viewer) is already the
// cosine it wants. Normalised so "across the picture" is 1.0.
float forward_scatter(float foreshorten) {
    const float g = 0.35;
    const float gg = g * g;
    float fwd = clamp(1.0 - foreshorten, 0.0, 1.0);
    float d = 1.0 + gg - 2.0 * g * fwd;
    float phase = (1.0 - gg) / pow(max(d, 1e-4), 1.5);
    const float at_rest = (1.0 - gg) / pow(1.0 + gg, 1.5);
    return phase / at_rest;
}

void main() {
    float shape;

    if (v_kind == KIND_QUAD) {
        // Flat across the face itself, falling off in the air around it. The
        // face is what the fixture looks like; the halo is what it lights.
        //
        // Distance to the rectangle, not to its centre — a matrix cell is
        // wider than it is tall, and a radial falloff would make a row of
        // pixels read as a row of dots instead of a lit bar.
        vec2 outside = max(abs(v_local) - v_box.xy, vec2(0.0));
        float t = length(outside) / max(v_box.z, 1e-3);
        // SPILL ONLY. The lit face is already drawn by the scene underneath,
        // so this is just what the pixel throws into the air. Adding the face
        // again here at full radiance is what made a bar read as a dense slab
        // no matter how the reach was tuned — and it coupled the two, because
        // the only way to reach further was to put out more light.
        //
        // Gaussian rather than inverse-square: it carries a long way at low
        // level instead of concentrating near the source, which is what
        // "far but subtle" actually looks like. How bright it is at all is
        // the primitive's own intensity (see emission/surface.py).
        shape = exp(-2.5 * t * t);
        shape *= 1.0 - smoothstep(0.75, 1.0, t);

    } else if (v_kind == KIND_CONE) {
        float t = v_local.x;
        // The shaft narrows toward the lens; 0.12 is the lens itself.
        float width_here = mix(0.12, 1.0, t);
        float across = abs(v_local.y) / width_here;

        // A spot holds a defined edge, a wash dissolves into one; frost opens
        // whatever edge there was back up.
        float edge = mix(0.75, 0.15, v_hardness) + v_softness * 0.5;
        float radial = 1.0 - smoothstep(1.0 - clamp(edge, 0.05, 0.98), 1.0, across);

        // Inverse-square along the throw, which is what a real shaft does. A
        // hard beam carries much further before it gives up.
        float axial = 1.0 / (1.0 + 3.0 * t * t);
        axial = mix(axial, mix(1.0, 0.55, t), v_hardness);
        axial *= 1.0 - smoothstep(0.90, 1.0, t);      // the beam ends

        // Beams are only visible because there is something to scatter off.
        float air = 0.25 + 0.75 * u_haze;
        shape = radial * axial * air * forward_scatter(v_foreshorten);

        // With the march running, hand it the shaft and keep the lens.
        shape *= mix(1.0, 1.0 - smoothstep(0.0, FOG_CORE, t), u_fog_active);

    } else if (v_kind == KIND_LINE) {
        // Coherent light: thin, hard, and still there where a wash has gone.
        float across = abs(v_local.y) / 0.35;
        shape = (1.0 - smoothstep(0.6, 1.0, across))
              * (1.0 - smoothstep(0.85, 1.0, v_local.x));

    } else {
        // POINT and BLOB: an inverse-square halo with a hot core, faded out at
        // the quad's edge so no seam shows.
        float r = length(v_local);
        float halo = 1.0 / (1.0 + 24.0 * r * r);
        float core = exp(-r * r * 40.0) * (v_kind == KIND_BLOB ? 0.0 : 0.6);
        shape = (halo + core) * (1.0 - smoothstep(0.75, 1.0, r));
        if (v_kind == KIND_BLOB) {
            // Only reached with the march off: with it on, the parcels are
            // routed to the medium map instead and never drawn as light.
            shape *= (0.25 + 0.75 * u_haze) * BLOB_GLOW;
        }
    }

    if (shape <= 0.0) {
        discard;
    }
    // Unbounded on purpose: this is the HDR buffer, and clamping here is
    // exactly the mistake the raster path makes.
    fragColor = vec4(v_rgb * (v_intensity * shape), 1.0);
}
"""

# ---------------------------------------------------------------------------
# Pass 2 — the medium map
#
# Every parcel of smoke and haze, rasterised into a small two-channel picture of
# what is in the air: how much, and how deep it reaches. Additive, because
# density adds — two puffs overlapping really are twice as thick.
#
# Deliberately flat in depth. The stage is a front elevation, so a parcel's `z`
# is synthetic to begin with; storing a curtain per screen position and giving
# it a depth profile analytically costs one texture fetch per march step
# instead of a loop over every puff, and nothing about the picture betrays the
# difference.
# ---------------------------------------------------------------------------

MEDIUM_VERT = _HEADER + """
in vec2 a_corner;
in float i_kind;
in vec2  i_pos;
in vec3  i_rgb;
in float i_intensity;      // BLOB: density
in float i_radius;
in float i_axis_deg;       // BLOB: z
in float i_length;         // BLOB: z extent

uniform mat4 u_scene_to_medium;

out vec2 v_local;
flat out float v_density;
flat out float v_z_extent;

void main() {
    vec2 scene = i_pos + a_corner * max(i_radius, 1.0);
    v_local = a_corner;
    v_density = i_intensity;
    v_z_extent = max(i_length, 1.0);
    gl_Position = u_scene_to_medium * vec4(scene, 0.0, 1.0);
}
"""

MEDIUM_FRAG = _HEADER + """
in vec2 v_local;
flat in float v_density;
flat in float v_z_extent;

out vec4 fragColor;

void main() {
    float r = length(v_local);
    if (r >= 1.0) { discard; }
    // Soft-edged, and faded right out at the rim so no parcel shows a seam.
    float shape = exp(-2.5 * r * r) * (1.0 - smoothstep(0.75, 1.0, r));
    float density = v_density * shape;
    if (density <= 0.0) { discard; }
    // The depth extent is stored density-WEIGHTED, because this buffer sums.
    // The march divides it back out, which makes it the average extent of
    // whatever is stacked over that point rather than the sum of them.
    fragColor = vec4(density, density * v_z_extent, 0.0, 1.0);
}
"""


# ---------------------------------------------------------------------------
# Pass 3 — the volumetric march
#
# The one place this renderer has a third dimension, and it is not invented: the
# projection really is orthographic, so a pixel's view ray is exactly
# `(scene_x, scene_y, -camera_z + t)` and the scene coordinate under the pixel
# IS its world XY. There is no depth buffer to reconstruct from and none is
# needed — the depth is an analytic plane.
#
# Beams become real cones. `axis_deg` is the direction in the picture and
# `foreshorten` is the cosine of how far out of it the fixture is aiming, which
# is the missing Z component already sitting in the light list.
#
# One quad per light, bounded to its own reach, rather than a fullscreen pass
# looping over a light array: the cost then follows lit screen area, and the
# number of lights stops mattering. The one fullscreen draw is the ambient term,
# which is the same program with `u_ambient_pass` on.
# ---------------------------------------------------------------------------

# Shared by both stages of the fog program: the vertex shader needs the slant
# length to size a beam's screen bound, and the fragment needs it to march one.
_FOG_CONSTANTS = """
const float PI = 3.14159265359;
const vec3 VIEW_DIR = vec3(0.0, 0.0, 1.0);   // orthographic: every ray is +Z

// How much of a beam's length survives being aimed at the viewer. Mirrors
// `ui/stage/frame.FORESHORTEN_FLOOR`; inverted below to recover the throw.
const float FORESHORTEN_FLOOR = %(foreshorten_floor)s;

// Fog scatters far more than it absorbs, so almost all the light that leaves a
// beam is still going somewhere rather than being swallowed.
const float FOG_ALBEDO = 0.9;

// How far a bare lamp lights the air around it, in lamp radii — and NOT the
// sprite pass's `u_glow_reach`, which is 60 of them.
//
// The two are not the same quantity. A sprite halo is a flat 2D falloff drawn
// once; the march integrates the same falloff along a ray a thousand units
// long, which turns a gentle glow into a screen-filling wash that swamps every
// beam on stage and shows its own quad's edges as seams. A lamp lights the air
// it is *in*; a beam lights the air along its throw, and that is the one that
// is supposed to read.
const float FOG_POINT_REACH = 10.0;

// How much of the room's baseline haze the stage-wide ambient term lights.
//
// Not 1, and not 0. The backdrop is a photograph of a room that was already lit
// and already had some air in it, so lighting all of that haze a second time is
// exactly the flat grey wash this whole pass is supposed to avoid. But zero was
// worse in a way that was actually reported: the HAZE slider then did nothing
// you could see unless a fixture happened to be throwing a shaft through it,
// and "is the room hazy" is the first question the control is asked.
//
// This is the fraction that reads as air standing in front of the picture
// rather than as air already in it. Machine-made smoke is NOT scaled by it —
// nothing put that in the photograph.
const float AMBIENT_HAZE_WEIGHT = 0.09;

// How bright a beam is, in the units the density scale uses.
//
// `density` here is a 0..1 number rather than a concentration, so the light in
// the air has to be quoted in the same made-up units — and a lamp behind a lens
// is enormously brighter than "one unit of haze". Without this the integral is
// still right and still invisible: a thin sheet of thin haze genuinely scatters
// about 0.01, against a lamp's own glow at 0.06 and a tonemap that needs to see
// something near 1. This is the number that makes the beam the thing you look
// at, which on a stage it is.
const float BEAM_RADIANCE = 20.0;

// The throw a fixture actually has, undoing the foreshortening the light list
// applied on the way in. A beam aimed at the viewer is short on the picture and
// long in the room, and the march needs the second number.
float slant_length(float drawn_length, float foreshorten) {
    return drawn_length / max(FORESHORTEN_FLOOR
        + (1.0 - FORESHORTEN_FLOOR) * clamp(foreshorten, 0.0, 1.0), 0.05);
}
""" % {"foreshorten_floor": repr(FORESHORTEN_FLOOR)}

_FOG_COMMON = """
uniform sampler2D u_medium;
uniform vec4  u_medium_rect;     // scene x, y, w, h that the medium map covers
uniform float u_haze;            // 0..1, the air before any parcel of smoke
uniform float u_extinction;      // per scene unit, per unit of density
uniform float u_fog_height;
uniform float u_height_falloff;
uniform float u_floor_y;         // scene Y of the stage floor
uniform float u_camera_z;
uniform float u_max_distance;
uniform float u_g;
uniform float u_texture;         // 0..3, how far the air is broken up
uniform float u_noise_scale;     // 1 / feature size, in scene units
uniform vec3  u_flow;            // scene units per second the air drifts
uniform float u_time;            // seconds, for that drift

// A hash with no visible lattice, from one 3D lattice point to 0..1.
float hash31(vec3 p) {
    p = fract(p * 0.3183099 + vec3(0.71, 0.113, 0.419));
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}

// Trilinear value noise. Cheap, smooth, and its lattice is invisible once two
// octaves are stacked — which is all that is needed here, because what this is
// modulating is already a soft field.
float value_noise(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    float n000 = hash31(i + vec3(0.0, 0.0, 0.0));
    float n100 = hash31(i + vec3(1.0, 0.0, 0.0));
    float n010 = hash31(i + vec3(0.0, 1.0, 0.0));
    float n110 = hash31(i + vec3(1.0, 1.0, 0.0));
    float n001 = hash31(i + vec3(0.0, 0.0, 1.0));
    float n101 = hash31(i + vec3(1.0, 0.0, 1.0));
    float n011 = hash31(i + vec3(0.0, 1.0, 1.0));
    float n111 = hash31(i + vec3(1.0, 1.0, 1.0));
    return mix(mix(mix(n000, n100, f.x), mix(n010, n110, f.x), f.y),
               mix(mix(n001, n101, f.x), mix(n011, n111, f.x), f.y), f.z);
}

// How the two octaves relate. The fine one is this much higher in frequency,
// its domain displaced by the coarse one, and it rides a much faster current.
const float FINE_RATIO = 2.7;
const float FINE_WARP = 1.7;
const float FINE_DRIFT = 3.5;
const float COARSE_WEIGHT = 0.62;

// How far the raw noise is stretched before `texture` scales it.
//
// Value noise is a weighted average of eight lattice hashes, so it piles up
// hard around its own middle: two octaves of it swing about a fifth either side
// of 0.5 and essentially never reach 0 or 1. Fed straight into the density that
// is a *faint dirtying* of an otherwise flat field, which is what the texture
// control used to look like however far it was pushed — the slider moved and
// the air stayed a sheet. Stretching first is what puts the range of the
// control onto air that is genuinely ragged.
const float TEXTURE_CONTRAST = 3.4;

// How much thicker or thinner the air is *here* than the smooth field says.
//
// This is the difference between fog and a filter. A medium map built from soft
// round parcels and a baseline that varies only with height is, at every scale
// anyone looks at, flat — so the shaft through it is flat, and the whole thing
// reads as a translucent sheet laid over the picture rather than as air. Real
// haze is ragged at every scale and it MOVES, which is the other half: a still
// texture is just a dirty lens.
//
// Two octaves, each on its own current: one that gives a shaft its billows, one
// that gives the billows their edges. Both drifting, and at different speeds on
// purpose — a single rigid translation of one field reads as a texture sliding
// behind the picture, whereas two shearing past each other reads as air that is
// churning while it travels. Clamped at zero, so the thin side of the noise can
// empty a pocket of air completely — the holes are what make it read as volume.
float turbulence(vec3 p) {
    if (u_texture <= 0.0) { return 1.0; }
    vec3 drift = u_flow * u_time * u_noise_scale;
    vec3 q = p * u_noise_scale + drift;
    float coarse = value_noise(q);
    // Domain warping, for the price of nothing: the fine octave is sampled at a
    // point the coarse one has pushed around, so its detail wraps around the
    // billows rather than sitting on top of them in a grid of its own. That is
    // the whole difference between smoke that curls and a field of round blobs,
    // and it costs no extra noise lookup — the displacement is a value already
    // in hand.
    vec3 r = q * FINE_RATIO
           + vec3(coarse, coarse * 0.73, coarse * 1.31) * FINE_WARP
           + drift * FINE_DRIFT + 19.3;
    float n = mix(value_noise(r), coarse, COARSE_WEIGHT);
    float s = clamp((n - 0.5) * TEXTURE_CONTRAST, -1.0, 1.0);
    // Past `u_texture = 1` the thin side runs into empty air and stops, which
    // is what separates billows into banks rather than a bug. It does leave the
    // *average* density slightly lopsided — but only slightly, because the
    // stretched noise still crowds its own middle rather than spreading evenly,
    // so almost nothing is clamped away. `tests/test_fog_math.py` measures it
    // and holds it there: how much fog there is stays the HAZE control's job,
    // and this one stays about how ragged it is.
    return max(0.0, 1.0 + u_texture * s);
}

// Henyey-Greenstein, normalised over the sphere so it integrates to 1. This is
// what decides that a beam aimed at you is blinding and the same beam crossing
// the stage is a shaft you can see the edge of.
float henyey_greenstein(float cos_theta, float g) {
    float gg = g * g;
    float d = 1.0 + gg - 2.0 * g * cos_theta;
    return (1.0 - gg) / (4.0 * PI * pow(max(d, 1e-4), 1.5));
}

// The same phase function with its broadside value divided out.
//
// The physical normalisation is over the sphere, which is right for radiance in
// physical units — and nothing here is in physical units. A beam's `level` is a
// 0..1 number off a DMX channel, so integrating against a 1/4pi phase buries the
// fog three orders of magnitude below the lamp that made it. Dividing by the
// broadside value keeps the SHAPE exactly (a beam aimed at you is still many
// times brighter than one crossing the stage) while putting `scattering = 1` at
// "as bright as the beam always looked". It is the same choice the raster
// path's `forward_scatter` makes, for the same reason.
float scatter_gain(float cos_theta, float g) {
    return henyey_greenstein(cos_theta, g) / henyey_greenstein(0.0, g);
}

// How much medium is at a point: the room's own haze, thinning with height
// because haze settles, plus whatever the machines have put there.
//
// `base_weight` is a weight rather than a flag because the two things lighting
// this medium do not see the baseline the same way. A fixture's beam scatters
// off everything in the air, baseline haze included, so it passes 1. The
// stage-wide ambient passes much less: the backdrop is a photograph of a real
// room that was already lit and already slightly hazy, so lighting all of that
// haze again a second time is what lifts the whole frame into flat grey. It is
// not zero either — a room you can SEE the haze in is the point of the slider —
// it is the part that reads as air standing in front of the photograph.
//
// Whatever comes out is then broken up by `turbulence`, which is what stops a
// smooth field of soft parcels reading as a sheet of tracing paper.
float density_at(vec3 p, float base_weight) {
    float height = max(0.0, u_floor_y - p.y) / max(u_fog_height, 1.0);
    float base = u_haze * exp(-u_height_falloff * height) * base_weight;

    float medium_density = 0.0;
    vec2 uv = (p.xy - u_medium_rect.xy) / u_medium_rect.zw;
    if (uv.x >= 0.0 && uv.x <= 1.0 && uv.y >= 0.0 && uv.y <= 1.0) {
        vec2 medium = texture(u_medium, uv).rg;
        if (medium.r > 0.0) {
            // The weighted average extent back out of the sum it was stored in.
            float extent = medium.g / medium.r;
            float depth = p.z / max(extent, 1.0);
            medium_density = medium.r * exp(-depth * depth);
        }
    }
    return (base + medium_density) * turbulence(p);
}

// Jimenez's interleaved gradient noise. Cheap, and its error is spread across
// the screen rather than banded, which is exactly what the temporal pass can
// then average away.
float ign(vec2 pixel, float frame) {
    vec3 magic = vec3(0.06711056, 0.00583715, 52.9829189);
    pixel += frame * 5.588238;
    return fract(magic.z * fract(dot(pixel, magic.xy)));
}
"""

FOG_VERT = _HEADER + _KINDS + _FOG_CONSTANTS + """
in vec2 a_corner;

in float i_kind;
in vec2  i_pos;
in vec3  i_rgb;
in float i_intensity;
in float i_radius;
in float i_axis_deg;
in float i_length;
in float i_half_width;
in float i_softness;
in float i_hardness;
in float i_foreshorten;
in vec2  i_size;

uniform mat4  u_scene_to_clip;
uniform mat4  u_clip_to_scene;
uniform float u_glow_reach;
uniform float u_max_glow;
uniform float u_ambient_pass;    // 1 for the one fullscreen draw

out vec2 v_scene;
flat out float v_kind;
flat out vec2  v_light_pos;
flat out vec3  v_rgb;
flat out float v_intensity;
flat out float v_radius;
flat out float v_axis_deg;
flat out float v_length;
flat out float v_half_width;
flat out float v_softness;
flat out float v_hardness;
flat out float v_foreshorten;

void main() {
    if (u_ambient_pass > 0.5) {
        // The medium lit by the room rather than by any fixture. Fullscreen,
        // and the scene position comes back through the inverse of the same
        // matrix the per-light path projects with, so the two agree.
        v_scene = (u_clip_to_scene * vec4(a_corner, 0.0, 1.0)).xy;
        v_kind = -1.0;
        gl_Position = vec4(a_corner, 0.0, 1.0);
    } else {
        // A conservative screen bound for this light. Conservative on purpose:
        // being generous costs a few fragments that march and find nothing,
        // while being tight by a pixel clips the end off a shaft.
        vec2 centre = i_pos;
        float reach;
        if (i_kind == KIND_CONE || i_kind == KIND_LINE) {
            float rad = radians(i_axis_deg);
            // The cone lives in three dimensions now, so its footprint has to
            // allow for the whole slant length: a beam aimed at the viewer is
            // short on the picture and long in the room, and its scattering
            // still lands on screen around the lens.
            float slant = slant_length(i_length, i_foreshorten);
            vec2 tip = i_pos + vec2(cos(rad), sin(rad)) * i_length;
            centre = mix(i_pos, tip, 0.5);
            reach = 0.5 * i_length + max(i_half_width, 2.0)
                  + slant * (1.0 - i_foreshorten);
        } else if (i_kind == KIND_POINT) {
            reach = i_radius * FOG_POINT_REACH;
        } else {
            // QUAD faces and BLOB parcels are not light sources to the march.
            reach = 0.0;
        }
        reach = clamp(reach, 0.0, u_max_glow);
        v_scene = centre + a_corner * reach;
        gl_Position = u_scene_to_clip * vec4(v_scene, 0.0, 1.0);
        v_kind = i_kind;
    }

    v_light_pos = i_pos;
    v_rgb = i_rgb;
    v_intensity = i_intensity;
    v_radius = i_radius;
    v_axis_deg = i_axis_deg;
    v_length = i_length;
    v_half_width = i_half_width;
    v_softness = i_softness;
    v_hardness = i_hardness;
    v_foreshorten = i_foreshorten;
}
"""

FOG_FRAG = _HEADER + _KINDS + _FOG_CONSTANTS + _FOG_COMMON + """
in vec2 v_scene;
flat in float v_kind;
flat in vec2  v_light_pos;
flat in vec3  v_rgb;
flat in float v_intensity;
flat in float v_radius;
flat in float v_axis_deg;
flat in float v_length;
flat in float v_half_width;
flat in float v_softness;
flat in float v_hardness;
flat in float v_foreshorten;

uniform int   u_steps;
uniform float u_jitter;
uniform float u_frame;
uniform float u_scattering;
uniform float u_glow_reach;
uniform vec3  u_ambient_rgb;
uniform float u_ambient_intensity;
uniform vec3  u_ambient_dir;

out vec4 fragColor;

// The beam's direction in three dimensions. `axis_deg` is where it points in
// the picture; `foreshorten` is the cosine of how far it points out of it, so
// the two together are a unit vector and nothing had to be added to the light
// list to get one.
vec3 beam_direction() {
    float rad = radians(v_axis_deg);
    float f = clamp(v_foreshorten, 0.0, 1.0);
    return vec3(cos(rad) * f, sin(rad) * f, -sqrt(max(0.0, 1.0 - f * f)));
}

// Where this ray enters and leaves the cone, exactly.
//
// The bounding sphere is not enough on its own: a beam is a thin sheet in
// depth — a 25 degree par is ~66 units thick at its widest — while the sphere's
// chord is hundreds, so a march spread over the chord put two samples inside
// the cone and twenty-two in empty air. Solving the cone gives every sample to
// the shaft.
//
// A ray along +Z through a cone is a quadratic in t. `a <= 0` means the ray is
// inside the cone's own opening angle of the axis, which only happens for a
// beam aimed almost straight down the lens; the sphere's chord is a fine
// answer there, and the branch keeps the algebra from dividing by nothing.
bool cone_segment(vec3 apex, vec3 axis, float k, out float enter,
                  out float leave) {
    vec3 o = vec3(v_scene, -u_camera_z) - apex;
    float kk = 1.0 + k * k;
    float along = dot(o, axis);
    float a = 1.0 - kk * axis.z * axis.z;
    float b = 2.0 * (o.z - kk * along * axis.z);
    float c = dot(o, o) - kk * along * along;
    if (a <= 1e-4) { return false; }
    float disc = b * b - 4.0 * a * c;
    if (disc <= 0.0) { return false; }             // the ray misses the cone
    float root = sqrt(disc);
    enter = (-b - root) / (2.0 * a);
    leave = (-b + root) / (2.0 * a);
    return true;
}

// What this light puts on a point in the air, and which way it was going.
vec3 radiance_at(vec3 p, out vec3 to_light) {
    vec3 origin = vec3(v_light_pos, 0.0);
    vec3 offset = p - origin;
    float dist = length(offset);
    to_light = dist > 1e-4 ? offset / dist : VIEW_DIR;

    if (v_kind == KIND_POINT) {
        float reach = max(v_radius * FOG_POINT_REACH, 1.0);
        float r = dist / reach;
        if (r > 1.0) { return vec3(0.0); }
        // Inverse-square, faded right out at the bound so the quad's own edge
        // can never show as a seam where two lamps overlap.
        float halo = 1.0 / (1.0 + 24.0 * r * r);
        return v_rgb * (v_intensity * halo * (1.0 - smoothstep(0.5, 1.0, r)));
    }

    float slant = slant_length(v_length, v_foreshorten);
    if (dist > slant) { return vec3(0.0); }
    vec3 axis = beam_direction();
    float along = dot(offset, axis);
    if (along <= 0.0) { return vec3(0.0); }        // behind the lens

    if (v_kind == KIND_LINE) {
        // A laser is far thinner than a march step, so it is given a radius it
        // can actually be hit at; below that it would flicker in and out as the
        // dither moved the samples around.
        float across = length(offset - axis * along);
        float width = max(v_half_width, 3.0);
        float profile = 1.0 - smoothstep(0.0, width, across);
        return v_rgb * (v_intensity * BEAM_RADIANCE * profile
                        * (1.0 - smoothstep(0.85, 1.0, along / slant)));
    }

    // A cone. Its half-angle comes from the two numbers `cone_geometry` already
    // worked out, so the marched shaft is exactly as wide as the drawn one.
    float t = along / slant;
    float radius_here = max(v_half_width, 1.0) * t;
    float across = length(offset - axis * along);
    if (across > radius_here) { return vec3(0.0); }

    // A spot holds a defined edge, a wash dissolves into one, frost opens
    // whatever edge there was back up.
    float edge = clamp(mix(0.75, 0.15, v_hardness) + v_softness * 0.5, 0.05, 0.98);
    float radial = 1.0 - smoothstep(1.0 - edge, 1.0, across / radius_here);

    float axial = 1.0 / (1.0 + 3.0 * t * t);
    axial = mix(axial, mix(1.0, 0.55, t), v_hardness);
    axial *= 1.0 - smoothstep(0.90, 1.0, t);       // the beam ends
    return v_rgb * (v_intensity * BEAM_RADIANCE * radial * axial);
}

void main() {
    float t0 = 0.0;
    float t1 = u_max_distance;
    int steps = u_steps;

    if (v_kind >= 0.0) {
        // Solve where this ray actually enters and leaves the light, and march
        // only that. Not an optimisation — a correctness fix.
        //
        // A beam is a thin sheet in depth: a par with a 25 degree lens is at
        // most ~140 units thick at its widest, while a march spread evenly over
        // a 1400-unit slab steps 58 units at a time. Nought to two samples ever
        // landed inside the cone, so the shaft simply did not appear, and no
        // step count small enough to fix that is affordable. Marching the
        // light's own volume puts every sample inside it instead.
        //
        // Every light sits on the rig plane at z = 0 and the ray runs along z,
        // so a bounding sphere is a chord: exact, four instructions, and a
        // superset of the cone inside it.
        float reach = (v_kind == KIND_POINT)
            ? max(v_radius * FOG_POINT_REACH, 1.0)
            : slant_length(v_length, v_foreshorten);
        vec2 offset = v_light_pos - v_scene;
        float half_chord = reach * reach - dot(offset, offset);
        if (half_chord <= 0.0) { discard; }        // the ray misses entirely
        half_chord = sqrt(half_chord);
        t0 = max(t0, u_camera_z - half_chord);
        t1 = min(t1, u_camera_z + half_chord);

        // For a beam, narrow that to the cone itself.
        if (v_kind == KIND_CONE) {
            float slant = slant_length(v_length, v_foreshorten);
            float enter, leave;
            if (cone_segment(vec3(v_light_pos, 0.0), beam_direction(),
                             max(v_half_width, 1.0) / slant, enter, leave)) {
                t0 = max(t0, enter);
                t1 = min(t1, leave);
            }
        }
        if (t1 <= t0) { discard; }
    }

    float dt = (t1 - t0) / float(steps);
    float jitter = ign(gl_FragCoord.xy, u_frame) * u_jitter;

    vec3 scatter = vec3(0.0);
    float transmittance = 1.0;

    bool ambient = v_kind < 0.0;
    float base_weight = ambient ? AMBIENT_HAZE_WEIGHT : 1.0;
    for (int k = 0; k < steps; ++k) {
        vec3 p = vec3(v_scene, -u_camera_z + t0 + (float(k) + jitter) * dt);
        float density = density_at(p, base_weight);
        if (density <= 1e-4) { continue; }         // clean air costs nothing

        float sigma = density * u_extinction;
        vec3 to_light;
        vec3 radiance = (v_kind < 0.0)
            ? u_ambient_rgb * u_ambient_intensity
            : radiance_at(p, to_light);
        if (v_kind < 0.0) { to_light = u_ambient_dir; }

        if (radiance.r + radiance.g + radiance.b > 0.0) {
            float phase = scatter_gain(dot(VIEW_DIR, to_light), u_g);
            scatter += transmittance * sigma * FOG_ALBEDO * u_scattering
                     * radiance * phase * dt;
        }
        // Beer-Lambert: what is left of the ray after this slice of air.
        transmittance *= exp(-sigma * dt);
        if (transmittance < 0.01) { break; }       // nothing behind this matters
    }

    if (scatter.r + scatter.g + scatter.b <= 0.0) { discard; }
    fragColor = vec4(scatter, 1.0);
}
"""


# ---------------------------------------------------------------------------
# Pass 4 — temporal
#
# The march starts each ray at a dithered offset, which trades banding for noise.
# This is where the noise goes: an exponential average with the previous frame.
# The camera on this stage never moves except on a resize, so there is nothing
# to reproject — the history is valid or it is thrown away wholesale.
# ---------------------------------------------------------------------------

TEMPORAL_FRAG = _HEADER + """
in vec2 v_uv;
uniform sampler2D u_current;
uniform sampler2D u_history;
uniform float u_blend;      // 1 = ignore the history entirely
out vec4 fragColor;

void main() {
    vec3 current = texture(u_current, v_uv).rgb;
    vec3 history = texture(u_history, v_uv).rgb;
    fragColor = vec4(mix(history, current, u_blend), 1.0);
}
"""


# ---------------------------------------------------------------------------
# Pass 5 — resolve
# ---------------------------------------------------------------------------

RESOLVE_VERT = _HEADER + """
in vec2 a_corner;
out vec2 v_uv;
void main() {
    v_uv = a_corner * 0.5 + 0.5;
    gl_Position = vec4(a_corner, 0.0, 1.0);
}
"""

RESOLVE_FRAG = _HEADER + """
in vec2 v_uv;
uniform sampler2D u_hdr;
uniform sampler2D u_fog;
uniform float u_exposure;
uniform float u_fog_amount;   // 0 when the march did not run
out vec4 fragColor;

// ACES filmic tonemap, Stephen Hill's fitted RRT+ODT (MIT, from BakingLab).
// Preferred over the cheaper Narkowicz one-liner because it holds saturated
// hues far better, and a stage is full of deep blue and magenta washes that
// the cheap fit turns to pastel.
//
// NOTE: Hill publishes these row-major for HLSL's mul(M, v). GLSL's mat3
// constructor fills COLUMNS, so what is written here is the transpose - which
// is what makes `MAT * v` the intended row-major product. Getting this wrong
// does not fail loudly; it just tints everything slightly green or magenta.
const mat3 ACES_IN = mat3(
    0.59719, 0.07600, 0.02840,
    0.35458, 0.90834, 0.13383,
    0.04823, 0.01566, 0.83777);
const mat3 ACES_OUT = mat3(
     1.60475, -0.10208, -0.00327,
    -0.53108,  1.10813, -0.07276,
    -0.07367, -0.00605,  1.07602);

vec3 rrt_and_odt_fit(vec3 v) {
    vec3 a = v * (v + 0.0245786) - 0.000090537;
    vec3 b = v * (0.983729 * v + 0.4329510) + 0.238081;
    return a / b;
}

vec3 aces_fitted(vec3 colour) {
    colour = ACES_IN * colour;
    colour = rrt_and_odt_fit(colour);
    colour = ACES_OUT * colour;
    return clamp(colour, 0.0, 1.0);
}

void main() {
    // The fog buffer is marched at a fraction of this one's size; sampling it
    // with a linear filter IS the upsample. Nothing fancier is warranted —
    // there is no depth or normal buffer to be bilateral against, and fog is
    // low-frequency by construction, which is why it was safe to compute small.
    vec3 fog = texture(u_fog, v_uv).rgb * u_fog_amount;
    vec3 hdr = (texture(u_hdr, v_uv).rgb + fog) * u_exposure;
    vec3 mapped = aces_fitted(hdr);
    // Back to the display's own encoding. The scene underneath is already
    // sRGB, and this is added onto it.
    vec3 srgb = pow(max(mapped, 0.0), vec3(1.0 / 2.2));
    fragColor = vec4(srgb, 1.0);
}
"""

#: Every per-instance attribute, in the order `ui.stage.light` packs them and
#: with its width in floats. `layer.py` builds the vertex-attribute pointers
#: from this, so the packing and the shader cannot disagree silently.
INSTANCE_ATTRIBUTES = (
    ("i_kind", 1),
    ("i_pos", 2),
    ("i_rgb", 3),
    ("i_intensity", 1),
    ("i_radius", 1),
    ("i_axis_deg", 1),
    ("i_length", 1),
    ("i_half_width", 1),
    ("i_softness", 1),
    ("i_hardness", 1),
    ("i_foreshorten", 1),
    ("i_size", 2),
)
