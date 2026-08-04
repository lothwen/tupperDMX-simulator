# ABOUTME: The GLSL the light layer runs — one instanced pass that accumulates
# ABOUTME: every light in linear HDR, and one that tonemaps the result with ACES.
"""Shaders.

Two programs. The first draws one quad per `LightPrimitive` into a float
framebuffer with `GL_ONE, GL_ONE`, so **light adds** — the thing the raster path
cannot do, and the reason two beams crossing there make mud instead of a
brighter patch. Nothing is clamped on the way in; a blinder at `multiply_factor`
2.0 really does land at twice full scale.

The second resolves that buffer to something a screen can show: exposure, then
the ACES filmic curve, which is what turns "brighter than white" into a
believable highlight that rolls off toward white instead of clipping per channel
and shifting hue on the way.

Kept as Python strings rather than files under `assets/`: a frozen build that
cannot find its shaders is a runtime failure on someone else's machine, and
there is nothing to gain from being able to edit them without a restart.
"""

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

out vec4 fragColor;

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
            shape *= 0.25 + 0.75 * u_haze;
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
# Pass 2 — resolve
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
uniform float u_exposure;
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
    vec3 hdr = texture(u_hdr, v_uv).rgb * u_exposure;
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
