# ABOUTME: The volumetric fog pass — a density map built from the smoke on
# ABOUTME: stage, and a ray march that integrates every beam through it.
"""Volumetric fog.

Owned by `LightLayer`, which hands it a context, the packed primitives and the
frame's parameters; everything in here is GL. Split out because the layer's job
is orchestration and this is a renderer of its own — three framebuffers, three
programs and a temporal history.

The march is described in `shaders.py`. What lives here is the plumbing it needs
and the two decisions that keep it affordable:

* **Everything is computed small.** The medium map is an eighth of the stage and
  the march runs at whatever `resolution` says (half, by default). Fog is soft
  and low-frequency, so resolution is far cheaper to give up than step count —
  and the upsample is free, because `RESOLVE_FRAG` samples the result with a
  linear filter.
* **The history is valid or it is gone.** This stage's camera never moves except
  on a resize, so there is nothing to reproject: if the size or the view matrix
  changed, the previous frame is discarded wholesale rather than smeared.

Fails soft like everything else here — `available` goes False, the layer stops
asking, and the stage keeps drawing without it.
"""

import ctypes
from typing import Optional, Tuple

from PyQt6.QtGui import QColor, QMatrix4x4

from tupperlogger import get_logger
from ui.stage.gpu import shaders
from ui.stage.light import STRIDE

logger = get_logger("ui.stage.gpu.fog")

_FLOAT = 0x1406
_TRIANGLE_STRIP = 0x0005
_BLEND = 0x0BE2
_ONE = 1
_RGBA16F = 0x881A
_TEXTURE_2D = 0x0DE1
_TEXTURE0 = 0x84C0
_TEXTURE1 = 0x84C1
_COLOR_BUFFER_BIT = 0x00004000
_TEXTURE_MAG_FILTER = 0x2800
_TEXTURE_MIN_FILTER = 0x2801
_TEXTURE_WRAP_S = 0x2802
_TEXTURE_WRAP_T = 0x2803
_LINEAR = 0x2601
_CLAMP_TO_EDGE = 0x812F

#: How thick the air reads when `stage_haze` is at full, over the whole marched
#: slab. Expressed as an optical depth so the look does not change when the slab
#: is made deeper — `max_distance` moves the *fog's* extent, not its density.
OPTICAL_DEPTH_AT_FULL = 2.5

#: The medium map's size relative to the stage image. A parcel of smoke is tens
#: of scene units across at its smallest, so an eighth still resolves one.
MEDIUM_SCALE = 0.125

#: How much of each frame's march survives into the next. Low enough that the
#: dither averages out over a handful of frames, high enough that a blackout
#: does not leave a ghost anyone can see.
TEMPORAL_BLEND = 0.35

#: How many frames the layer keeps asking for after the rig stops changing, so
#: the temporal average has time to settle. Then it stops: a still rig costs
#: nothing, and that is the invariant the whole repaint scheme rests on.
CONVERGE_FRAMES = 6

#: How the air moves, in scene units per second, for the noise that gives it its
#: structure. Mostly sideways: haze in a room is pushed across it by the air
#: handling and rises only slowly, and a texture that does not move is a dirty
#: lens rather than air.
#:
#: Fast enough to *see*, which the first pass at this was not: at 7 px/s a
#: billow the width of a third of the stage took the better part of a minute to
#: travel its own width, so the honest description of the air was "still". A
#: feature now crosses its own width in something under ten seconds, which reads
#: as drifting rather than as either frozen or blowing a gale. Deliberately in
#: scene units per second and NOT scaled by the feature size: big billows are
#: then the ones whose travel you can actually follow across the stage, which is
#: what makes the motion most obvious exactly where the texture is largest.
FLOW_PX_S = (34.0, -22.0, 16.0)

#: How long the noise's clock runs before it wraps, in seconds. Six hours: long
#: enough that no show meets the step, short enough that a float32 uniform still
#: resolves a hundredth of a noise cell at the finest feature size the settings
#: allow.
TIME_WRAP_S = 6.0 * 3600.0


class FogPass:
    """The medium map, the march and its temporal history."""

    def __init__(self, gl, quad, instances, vao):
        self._gl = gl
        self._quad = quad
        self._instances = instances
        self._vao = vao
        self._medium_program = None
        self._fog_program = None
        self._temporal_program = None
        self._medium_fbo = None
        self._medium_size = (0, 0)
        self._march_fbo = None       # this frame's march, on its own
        self._history_fbos = [None, None]
        self._fog_size = (0, 0)
        self._history = 0            # which of the pair holds the last result
        self._history_valid = False
        self._last_key = None        # (size, view matrix) the history belongs to
        self._frame = 0.0
        self.available = False

    # -- setup ---------------------------------------------------------
    def build(self, program_cls, shader_cls, build) -> bool:
        """Compile the three programs. `build` is the layer's own builder, so a
        failure is logged and surrendered the same way every other one is."""
        self._medium_program = build(
            program_cls, shader_cls, shaders.MEDIUM_VERT, shaders.MEDIUM_FRAG,
            "medium")
        self._fog_program = build(
            program_cls, shader_cls, shaders.FOG_VERT, shaders.FOG_FRAG, "fog")
        self._temporal_program = build(
            program_cls, shader_cls, shaders.RESOLVE_VERT,
            shaders.TEMPORAL_FRAG, "temporal")
        self.available = None not in (self._medium_program, self._fog_program,
                                      self._temporal_program)
        return self.available

    # -- per frame -----------------------------------------------------
    def render(self, *, packed, light_count, medium_count, scene_to_clip,
               width, height, scene_rect, fog, haze, seconds) -> Optional[int]:
        """March the fog. Returns the texture holding it, or None if it did not
        run — an empty stage, or a resolution that rounds away to nothing."""
        fog_width = max(1, int(width * fog.resolution))
        fog_height = max(1, int(height * fog.resolution))
        if fog_width < 2 or fog_height < 2:
            return None

        self._frame = (self._frame + 1.0) % 64.0
        self._build_medium(packed, light_count, medium_count, scene_rect)

        self._ensure_fog_fbos(fog_width, fog_height)
        self._march(packed, light_count, scene_to_clip, fog_width, fog_height,
                    scene_rect, fog, haze, seconds)
        return self._blend(fog_width, fog_height, scene_to_clip)

    def _key(self, size, scene_to_clip) -> tuple:
        """What the history is only valid for: this size, this view."""
        return (size, tuple(round(scene_to_clip.column(i)[j], 4)
                            for i in range(4) for j in range(4)))

    # -- the medium map ------------------------------------------------
    def _ensure_medium_fbo(self, scene_rect) -> None:
        from PyQt6.QtOpenGL import (
            QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
        )
        width = max(2, int(scene_rect[2] * MEDIUM_SCALE))
        height = max(2, int(scene_rect[3] * MEDIUM_SCALE))
        if self._medium_fbo is not None and self._medium_size == (width, height):
            return
        fmt = QOpenGLFramebufferObjectFormat()
        # Float, because density sums and a parcel's depth extent rides in the
        # second channel weighted by it — neither survives 8 bits.
        fmt.setInternalTextureFormat(_RGBA16F)
        self._medium_fbo = QOpenGLFramebufferObject(width, height, fmt)
        self._medium_size = (width, height)
        self._smooth(self._medium_fbo)

    def _build_medium(self, packed, light_count, medium_count, scene_rect) -> None:
        """Rasterise every parcel of smoke into the density map."""
        gl = self._gl
        self._ensure_medium_fbo(scene_rect)
        self._medium_fbo.bind()
        gl.glViewport(0, 0, self._medium_size[0], self._medium_size[1])
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(_COLOR_BUFFER_BIT)
        if medium_count:
            gl.glEnable(_BLEND)
            gl.glBlendFunc(_ONE, _ONE)      # density adds
            program = self._medium_program
            self._vao.bind()
            program.bind()
            program.setUniformValue("u_scene_to_medium",
                                    _scene_to_ndc(scene_rect))
            self._bind_quad(program)
            enabled = self._bind_instances(program, packed, first=light_count)
            gl.glDrawArraysInstanced(_TRIANGLE_STRIP, 0, 4, medium_count)
            self._unbind(program, enabled)
            self._vao.release()
        self._medium_fbo.release()

    # -- the march -----------------------------------------------------
    def _ensure_fog_fbos(self, width, height) -> None:
        """Three buffers, not two.

        The obvious pairing — march into A, average A into B — reads and writes
        B in the same draw, which is a framebuffer feedback loop: undefined in
        GL, and on the driver it was written against it compounded instead of
        averaging, so the fog ramped to white over four frames rather than
        settling. The march therefore gets a buffer of its own and the history
        ping-pongs between the other two, so nothing is ever both source and
        destination.
        """
        from PyQt6.QtOpenGL import (
            QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
        )
        if self._march_fbo is not None and self._fog_size == (width, height):
            return
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setInternalTextureFormat(_RGBA16F)
        self._march_fbo = QOpenGLFramebufferObject(width, height, fmt)
        self._history_fbos = [QOpenGLFramebufferObject(width, height, fmt),
                              QOpenGLFramebufferObject(width, height, fmt)]
        self._fog_size = (width, height)
        for target in (self._march_fbo, *self._history_fbos):
            self._smooth(target)
        self._history_valid = False     # a resized history is not a history

    def _march(self, packed, light_count, scene_to_clip, width, height,
               scene_rect, fog, haze, seconds) -> None:
        gl = self._gl
        program = self._fog_program
        self._march_fbo.bind()
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(_COLOR_BUFFER_BIT)
        gl.glEnable(_BLEND)
        gl.glBlendFunc(_ONE, _ONE)      # each light's in-scattering adds

        self._vao.bind()
        program.bind()
        self._set_march_uniforms(program, scene_to_clip, scene_rect, fog, haze,
                                 seconds)

        gl.glActiveTexture(_TEXTURE0)
        gl.glBindTexture(_TEXTURE_2D, self._medium_fbo.texture())
        program.setUniformValue("u_medium", 0)

        self._bind_quad(program)
        # One bounded quad per light. A light that reaches nothing draws a
        # degenerate quad and costs a vertex, which is the cull.
        program.setUniformValue("u_ambient_pass", 0.0)
        enabled = self._bind_instances(program, packed, first=0)
        if light_count:
            gl.glDrawArraysInstanced(_TRIANGLE_STRIP, 0, 4, light_count)
        self._unbind_arrays(program, enabled)

        # And one fullscreen pass for the room's own light on the medium, so
        # smoke is visible before any fixture is. Same program: with the
        # instance arrays disabled the vertex shader takes its other branch.
        if fog.light_intensity > 0.0:
            program.setUniformValue("u_ambient_pass", 1.0)
            gl.glDrawArrays(_TRIANGLE_STRIP, 0, 4)

        program.disableAttributeArray(program.attributeLocation("a_corner"))
        program.release()
        self._vao.release()
        gl.glBindTexture(_TEXTURE_2D, 0)
        self._march_fbo.release()

    def _set_march_uniforms(self, program, scene_to_clip, scene_rect, fog,
                            haze, seconds) -> None:
        import math

        from ui.stage.gpu.layer import GLOW_REACH, MAX_GLOW_RADIUS_PX

        inverse, invertible = scene_to_clip.inverted()
        program.setUniformValue("u_scene_to_clip", scene_to_clip)
        program.setUniformValue("u_clip_to_scene",
                                inverse if invertible else QMatrix4x4())
        program.setUniformValue("u_glow_reach", float(GLOW_REACH))
        program.setUniformValue("u_max_glow", float(MAX_GLOW_RADIUS_PX))

        program.setUniformValue("u_medium_rect", *(float(v) for v in scene_rect))
        program.setUniformValue("u_haze", float(haze))
        # Density is a 0..1 fraction, not a coefficient: this is what turns it
        # into one, normalised by the slab's depth so making the room deeper
        # does not make its air thicker.
        program.setUniformValue(
            "u_extinction",
            float(OPTICAL_DEPTH_AT_FULL / max(fog.max_distance, 1.0)))
        program.setUniformValue("u_fog_height", float(fog.height))
        program.setUniformValue("u_height_falloff", float(fog.height_falloff))
        # The floor is the bottom of the stage image; height is measured up from
        # it, and the picture's Y grows downward.
        program.setUniformValue("u_floor_y", float(scene_rect[1] + scene_rect[3]))
        # The rig plane sits a little past the middle of the slab, so there is
        # room in front of the fixtures for a beam aimed at the audience.
        program.setUniformValue("u_camera_z", float(fog.max_distance * 0.55))
        program.setUniformValue("u_max_distance", float(fog.max_distance))
        program.setUniformValue("u_g", float(fog.anisotropy))
        program.setUniformValue("u_steps", int(fog.march_steps))
        program.setUniformValue("u_jitter", float(fog.jitter))
        program.setUniformValue("u_frame", float(self._frame))
        program.setUniformValue("u_scattering", float(fog.scattering))

        # The air's own structure, and the fact that it drifts. Without these
        # the march integrates a perfectly smooth field and the result is a
        # translucent sheet laid over the stage — which is what it looked like.
        program.setUniformValue("u_texture", float(fog.texture))
        program.setUniformValue("u_noise_scale",
                                1.0 / max(fog.texture_scale, 1.0))
        program.setUniformValue("u_flow", *(float(v) for v in FLOW_PX_S))
        # Wrapped, because a float32 uniform loses its low bits once the app has
        # been open long enough and the noise would visibly quantise. The wrap
        # itself is a discontinuity — value noise is not periodic, so there is
        # no period that would hide one — which is why it is as far out as the
        # precision allows rather than as near as it is safe to put it. Ten
        # minutes was fine while the air barely moved; a step in the fog every
        # ten minutes of a show is not, now that it does.
        program.setUniformValue("u_time", float(seconds % TIME_WRAP_S))

        red, green, blue = _linear_color(fog.color)
        program.setUniformValue("u_ambient_rgb", red, green, blue)
        program.setUniformValue("u_ambient_intensity", float(fog.light_intensity))
        angle = math.radians(fog.light_dir_deg)
        # Into the picture, so the room's light comes from somewhere rather than
        # from the camera — which would make the phase function do nothing. Unit
        # length, because the phase function takes the cosine of this against the
        # view ray and an unnormalised vector quietly makes that a constant.
        direction = (math.cos(angle), math.sin(angle), 0.35)
        scale = 1.0 / math.sqrt(sum(v * v for v in direction))
        program.setUniformValue("u_ambient_dir",
                                *(float(v * scale) for v in direction))

    # -- temporal ------------------------------------------------------
    def _blend(self, width, height, scene_to_clip) -> int:
        """Average this march with the last one. Returns the texture to use."""
        gl = self._gl
        key = self._key((width, height), scene_to_clip)
        if key != self._last_key:
            # The view moved, so the history is of a different picture. There is
            # nothing to reproject onto — this stage has no motion vectors and
            # no camera to have moved — so it goes rather than smears.
            self._history_valid = False
            self._last_key = key

        source = self._history
        destination = 1 - source
        self._history_fbos[destination].bind()
        gl.glViewport(0, 0, width, height)
        gl.glDisable(_BLEND)                 # this pass replaces, it does not add

        self._vao.bind()
        program = self._temporal_program
        program.bind()
        gl.glActiveTexture(_TEXTURE0)
        gl.glBindTexture(_TEXTURE_2D, self._march_fbo.texture())
        program.setUniformValue("u_current", 0)
        gl.glActiveTexture(_TEXTURE1)
        gl.glBindTexture(_TEXTURE_2D, self._history_fbos[source].texture())
        program.setUniformValue("u_history", 1)
        # With no history to average, take the march whole — one pass either
        # way, so the first frame needs no special case downstream.
        program.setUniformValue(
            "u_blend", float(TEMPORAL_BLEND if self._history_valid else 1.0))

        self._bind_quad(program)
        gl.glDrawArrays(_TRIANGLE_STRIP, 0, 4)

        program.disableAttributeArray(program.attributeLocation("a_corner"))
        program.release()
        self._vao.release()
        gl.glActiveTexture(_TEXTURE1)
        gl.glBindTexture(_TEXTURE_2D, 0)
        gl.glActiveTexture(_TEXTURE0)
        gl.glBindTexture(_TEXTURE_2D, 0)
        self._history_fbos[destination].release()

        self._history = destination
        self._history_valid = True
        return self._history_fbos[destination].texture()

    def invalidate(self) -> None:
        """Throw the history away — the light changed enough to smear."""
        self._history_valid = False

    def _smooth(self, fbo) -> None:
        """Filter this target linearly, and clamp at its edges.

        `QOpenGLFramebufferObject` hands out a GL_NEAREST texture, which is the
        wrong default for every buffer here: the fog is marched small and
        magnified on resolve, and the medium map is sampled at arbitrary points
        along a ray. Left nearest, both show their own texels as blocks — the
        "free" upsample this whole design leans on is only free if the filter is
        actually asked for.
        """
        gl = self._gl
        gl.glBindTexture(_TEXTURE_2D, fbo.texture())
        gl.glTexParameteri(_TEXTURE_2D, _TEXTURE_MIN_FILTER, _LINEAR)
        gl.glTexParameteri(_TEXTURE_2D, _TEXTURE_MAG_FILTER, _LINEAR)
        gl.glTexParameteri(_TEXTURE_2D, _TEXTURE_WRAP_S, _CLAMP_TO_EDGE)
        gl.glTexParameteri(_TEXTURE_2D, _TEXTURE_WRAP_T, _CLAMP_TO_EDGE)
        gl.glBindTexture(_TEXTURE_2D, 0)

    # -- shared plumbing -----------------------------------------------
    def _bind_quad(self, program) -> None:
        gl = self._gl
        self._quad.bind()
        corner = program.attributeLocation("a_corner")
        program.enableAttributeArray(corner)
        program.setAttributeBuffer(corner, _FLOAT, 0, 2, 0)
        gl.glVertexAttribDivisor(corner, 0)
        self._quad.release()

    def _bind_instances(self, program, packed, first: int) -> list:
        """Point the instance attributes at the buffer, starting at `first`.

        The whole packed array is already on the GPU from the light pass, so
        this only moves the offsets — the media sit after the lights in the same
        upload, which is why one buffer serves both.
        """
        gl = self._gl
        self._instances.bind()
        stride = STRIDE * 4
        offset = first * stride
        enabled = []
        for name, width in shaders.INSTANCE_ATTRIBUTES:
            location = program.attributeLocation(name)
            if location >= 0:
                program.enableAttributeArray(location)
                program.setAttributeBuffer(location, _FLOAT, offset, width, stride)
                gl.glVertexAttribDivisor(location, 1)
                enabled.append(location)
            offset += width * 4
        self._instances.release()
        return enabled

    def _unbind_arrays(self, program, enabled) -> None:
        for location in enabled:
            program.disableAttributeArray(location)

    def _unbind(self, program, enabled) -> None:
        self._unbind_arrays(program, enabled)
        program.disableAttributeArray(program.attributeLocation("a_corner"))
        program.release()

    def cleanup(self) -> None:
        self._medium_fbo = None
        self._march_fbo = None
        self._history_fbos = [None, None]
        self._medium_program = None
        self._fog_program = None
        self._temporal_program = None
        self._gl = None
        self.available = False


def _scene_to_ndc(scene_rect) -> QMatrix4x4:
    """Scene coordinates onto the medium map's clip space.

    Its own projection rather than the view's: the map covers the whole stage
    whatever part of it is currently on screen, so a parcel of smoke that has
    drifted out of frame still thickens the air of the bit that has not.
    """
    x, y, width, height = scene_rect
    matrix = QMatrix4x4()
    matrix.translate(-1.0, -1.0)
    matrix.scale(2.0 / max(width, 1.0), 2.0 / max(height, 1.0))
    matrix.translate(-x, -y)
    return matrix


def _linear_color(name: str):
    """A `#rrggbb` string as linear floats, the space the HDR buffer is in."""
    from ui.stage.light import linear_rgb

    color = QColor(name)
    if not color.isValid():
        color = QColor(223, 228, 238)
    return linear_rgb((color.red(), color.green(), color.blue()))
