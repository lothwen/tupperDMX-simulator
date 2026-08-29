# ABOUTME: The GPU light layer — accumulates every fixture's light in one HDR
# ABOUTME: buffer, tonemaps it with ACES, and adds it over the rendered scene.
"""The light layer.

Runs inside `StageView.drawForeground`, between `beginNativePainting()` and
`endNativePainting()`, so it draws onto the same surface the scene was just
rendered to. Three things had to be true for that to work, and all three were
settled by measurement rather than by reading the docs:

* **The GL widget has to BE the viewport.** A `QOpenGLWidget` parented into the
  viewport renders opaque and hides the scene, whatever combination of
  `WA_TranslucentBackground` / `WA_NoSystemBackground` / `WA_OpaquePaintEvent` /
  `WA_AlwaysStackOnTop` it is given. There is no way through that route.
* **The surface format must force desktop GL, and whether it may ask for a
  version is platform-specific** — X11/Wayland must not, macOS must. See
  `surface_format`.
* **The draw calls come from `QOpenGLFunctions_4_1_Core`** (or `_3_3_Core`,
  which is the floor everything here is written against). PyQt6 exposes the
  shader program, the buffers, the FBO and the VAO, but no `glDrawArrays`;
  those live on the version-functions object. PyOpenGL is not a substitute — it
  works on GL ES and errors with "no valid context" on desktop GL.

Everything here fails soft. A context that will not give us a float
framebuffer, a shader that will not compile, a driver that is simply absent:
all of them log once and leave `available` False, and the stage carries on
drawing exactly as it did before.
"""

import ctypes
import sys
from dataclasses import astuple
from typing import Callable, Iterable, Optional

from PyQt6.QtGui import QMatrix4x4, QOpenGLContext, QSurfaceFormat

from tupperlogger import get_logger
from stage.clock import CLOCK
from stage.light import BLOB, CONE, LINE, POINT, QUAD, STRIDE
from stage.gpu import fog as fog_module
from stage.gpu import shaders

logger = get_logger("stage.gpu")

_FLOAT = 0x1406          # GL_FLOAT
_TRIANGLE_STRIP = 0x0005
_BLEND = 0x0BE2
_ONE = 1
_RGBA16F = 0x881A
_TEXTURE_2D = 0x0DE1
_TEXTURE0 = 0x84C0
_TEXTURE1 = 0x84C1
_COLOR_BUFFER_BIT = 0x00004000
_SCISSOR_TEST = 0x0C11

#: Version-functions objects to try, best first. 4.1 is what a current desktop
#: driver gives and exactly what macOS caps at; 3.3 is the floor, and a real
#: one — every GL call in here and in `fog.py` is core in 3.3, and so is the
#: `#version 330` the shaders are written against. Asking only for 4.1 turned
#: a driver one version short into no light at all, which is the wrong trade
#: for a layer that already fails soft.
_GL_VERSIONS = ((4, 1), (3, 3))

#: A ceiling on the glow radius in scene units, whatever `GLOW_REACH` multiplies
#: a lamp by. Without it a big fixture's halo is a screen-filling disc that
#: escapes the stage image entirely — the raster path holds the same line with
#: `cone.MAX_GLOW_RADIUS_PX`, and for the same reason.
MAX_GLOW_RADIUS_PX = 800.0

#: How far a lamp's halo reaches, in lamp radii. The raster path's equivalent
#: is `glow_radius_multiplier`, but that had to be clamped hard because it was
#: painting a 6-stop gradient over an enormous bounding rect; here the falloff
#: is analytic and the cost is one quad, so it can afford to be generous.
GLOW_REACH = 60.0

#: True once the stage has a working light layer. The scene preview reads this
#: rather than the config: a look must preview the way it will appear on stage,
#: so what matters is not what was *asked* for but what the stage actually got.
#: It also keeps every headless and offscreen caller on the raster path for
#: free, since the stage never comes up there to set it.
STAGE_GPU_ACTIVE = False


def stage_gpu_active() -> bool:
    return STAGE_GPU_ACTIVE


def _px_per_scene(scene_to_clip: QMatrix4x4, width: int) -> float:
    """How many device pixels one scene unit covers, from the matrix itself.

    The one thing a shader cannot work out on its own and the one primitive
    that needs it — a laser — is thinner than a pixel, so it has to be drawn
    wider than it is or the rasteriser turns it into a dashed line. The mapping
    is already in the matrix that projects it: scene units go to clip space, and
    clip space spans 2 across the surface.
    """
    return abs(scene_to_clip.column(0).x()) * width / 2.0


def surface_format() -> QSurfaceFormat:
    """The context the layer needs, which two platforms describe oppositely.

    * `setRenderableType(OpenGL)` is mandatory everywhere. Without it a
      Wayland/EGL session hands out **OpenGL ES**, where
      `QOpenGLVersionFunctionsFactory` returns nothing and none of the shaders
      compile.
    * On X11/Wayland `setVersion` / `setProfile` must NOT be called.
      Requesting 3.3 Core fails outright there (`EGL_BAD_MATCH`) and takes
      Qt's own backing store down with it — the window comes up blank, with no
      obvious connection to the request that caused it. Nothing is lost by not
      asking: the compatibility context the driver hands out instead is
      already 4.x, so the core version functions resolve out of it anyway.
    * **On macOS the version must be asked for**, and this is the one platform
      where staying silent is the bug. Cocoa offers exactly two profiles —
      legacy 2.1 and Core — and gives out the legacy one unless a core version
      is requested: no instancing, no `#version 330`, no version-functions
      object, which is the "no OpenGL 4.1 core functions on this context" the
      layer used to refuse every Mac with. 4.1 is exact rather than a floor
      because it is simultaneously what this needs and the highest macOS has
      ever offered (Apple froze OpenGL there and deprecated it in 10.14).
    """
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    if sys.platform == "darwin":
        fmt.setVersion(4, 1)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    return fmt


def configure_surface_format() -> None:
    """Make `surface_format()` the default. Must run before `QApplication`."""
    QSurfaceFormat.setDefaultFormat(surface_format())


def apply_surface_format(widget) -> None:
    """Ask ONE `QOpenGLWidget` for that context, before it has built one.

    The default format is set at startup and only when the preference was
    already on, so a stage switched to the GPU from Settings builds its GL
    viewport in an application whose default is still Qt's own. On macOS that
    default is the legacy 2.1 context, so the layer would turn down the very
    context the user had just asked for and nothing but a restart would help.
    Setting it per widget costs nothing on the platforms that did not need it:
    it is the same format the default would have been.

    The application default is moved too, and not merely as a belt: Qt 6 gives
    every `QOpenGLWidget` in a window one shared context, and whichever of them
    builds it first decides its format. Only the widget's own format would
    leave that to the order the stage and the scene preview happen to open in.
    Safe to do here rather than before `QApplication` because nothing sets
    `AA_ShareOpenGLContexts` — with no global share context to have missed it,
    the default reaches every surface made from here on.
    """
    configure_surface_format()
    widget.setFormat(surface_format())


class LightLayer:
    """One HDR pass over every body's light list.

    `bodies` is a callable returning the bodies to draw, asked once per frame,
    so the layer never holds a reference that could outlive the stage.
    """

    def __init__(self, bodies: Callable[[], Iterable]):
        self._bodies = bodies
        self._fns = None
        self._light_program = None
        self._resolve_program = None
        self._quad = None
        self._instances = None
        self._vao = None
        self._fbo = None
        self._size = (0, 0)
        self._failed = False
        self._packed = None
        self._light_count = 0
        self._medium_count = 0
        self._fog = None
        self._fog_texture = None
        self._converge = 0
        self._last_signature = None
        self.available = False
        self.exposure = 1.0
        self.haze = 0.0
        #: A `core.config.StageFogConfig`, or None for no volumetric fog.
        self.fog = None
        #: The stage image's rectangle, `(x, y, w, h)` in scene units. The fog
        #: needs it to know where the floor is and how big its density map is.
        self.scene_rect = (0.0, 0.0, 1536.0, 1024.0)
        #: Called when the fog march needs another frame to settle. Set by the
        #: stage; nothing else here ever asks for a repaint it was not given a
        #: reason for.
        self.request_repaint = None
        self.last_primitive_count = 0

    # -- setup ---------------------------------------------------------
    def _initialise(self) -> bool:
        """Build everything, once, inside a current context."""
        from PyQt6.QtOpenGL import (
            QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram,
            QOpenGLVertexArrayObject, QOpenGLVersionFunctionsFactory,
            QOpenGLVersionProfile,
        )

        context = QOpenGLContext.currentContext()
        if context is None:
            return self._give_up("no current OpenGL context")
        if context.isOpenGLES():
            return self._give_up(
                "got an OpenGL ES context; the surface format must force "
                "desktop GL (see configure_surface_format)")

        for major, minor in _GL_VERSIONS:
            profile = QOpenGLVersionProfile()
            profile.setVersion(major, minor)
            profile.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            self._fns = QOpenGLVersionFunctionsFactory.get(profile, context)
            if self._fns is not None:
                break
        else:
            got = context.format()
            return self._give_up(
                "no OpenGL 3.3+ core functions on this context (it reports "
                f"OpenGL {got.majorVersion()}.{got.minorVersion()}, "
                f"{got.profile().name})")
        self._fns.initializeOpenGLFunctions()

        self._light_program = self._build(
            QOpenGLShaderProgram, QOpenGLShader,
            shaders.LIGHT_VERT, shaders.LIGHT_FRAG, "light")
        self._resolve_program = self._build(
            QOpenGLShaderProgram, QOpenGLShader,
            shaders.RESOLVE_VERT, shaders.RESOLVE_FRAG, "resolve")
        if self._light_program is None or self._resolve_program is None:
            return False

        self._vao = QOpenGLVertexArrayObject()
        if not self._vao.create():
            return self._give_up("could not create a vertex array object")

        # One unit quad, drawn once per primitive as an instance.
        corners = (-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0)
        data = (ctypes.c_float * len(corners))(*corners)
        self._quad = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._quad.create()
        self._quad.bind()
        self._quad.allocate(bytes(data), len(bytes(data)))
        self._quad.release()

        self._instances = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self._instances.create()
        self._instances.setUsagePattern(QOpenGLBuffer.UsagePattern.DynamicDraw)

        # The fog march is optional: if its programs will not compile, the stage
        # keeps its light and simply has no volumetric air.
        from functools import partial

        from stage.gpu.fog import FogPass
        fog = FogPass(self._fns, self._quad, self._instances, self._vao)
        self._fog = fog if fog.build(
            QOpenGLShaderProgram, QOpenGLShader,
            partial(self._build, optional=True)) else None

        self.available = True
        version = context.format()
        logger.info("GPU light layer ready (OpenGL %d.%d)",
                    version.majorVersion(), version.minorVersion())
        return True

    def _build(self, program_cls, shader_cls, vert, frag, name,
               optional: bool = False):
        """Compile and link one program, or None.

        `optional` marks a program the layer can do without — the fog march.
        A failure there costs the stage its volumetric air; a failure in the
        light or resolve programs costs it everything, so only the second kind
        takes the whole layer down with it.
        """
        def fail(why: str):
            if optional:
                logger.warning("no volumetric fog on this context: %s", why)
                return None
            return self._give_up(why) or None

        program = program_cls()
        if not program.addShaderFromSourceCode(shader_cls.ShaderTypeBit.Vertex, vert):
            return fail(f"{name} vertex shader: {program.log()}")
        if not program.addShaderFromSourceCode(shader_cls.ShaderTypeBit.Fragment, frag):
            return fail(f"{name} fragment shader: {program.log()}")
        if not program.link():
            return fail(f"{name} link: {program.log()}")
        return program

    def _give_up(self, why: str) -> bool:
        if not self._failed:
            logger.warning("GPU light layer unavailable, staying on the raster "
                           "path: %s", why)
        self._failed = True
        self.available = False
        return False

    # -- per frame -----------------------------------------------------
    def render(self, scene_to_clip: QMatrix4x4, width: int, height: int,
               clip=None) -> bool:
        """Draw the light. Call with a context current and the scene already
        rendered. Returns False if the layer is not usable.

        `clip` is the stage image's rectangle in device pixels, Qt-style
        (top-left origin). Light is confined to it: the view letterboxes the
        scene, and the bars either side are not part of the stage — anything
        drawn there is spill onto whatever the window happens to be sitting on.
        """
        if self._failed:
            return False
        if self._fns is None and not self._initialise():
            return False
        if width <= 0 or height <= 0:
            return False

        count = self._pack()
        self.last_primitive_count = count
        if count == 0 and not self._air_worth_drawing():
            return True          # nothing lit is a perfectly good frame

        signature = self._frame_signature() if self._fog_wanted() else None
        changed = signature != self._last_signature
        self._last_signature = signature

        try:
            self._ensure_fbo(width, height)
            self._set_clip(clip, height)
            self._upload_instances()
            self._accumulate(scene_to_clip, self._light_count)
            self._march_fog(scene_to_clip, width, height, clip)
            self._resolve(width, height)
        except Exception as exc:                       # pragma: no cover
            return self._give_up(f"draw failed: {exc!r}")
        finally:
            self._restore(width, height)
        if signature is not None:
            self._settle(changed)
        return True

    def _march_fog(self, scene_to_clip: QMatrix4x4, width: int,
                   height: int, clip) -> None:
        """Integrate every beam through the air, into a half-size buffer.

        Unscissored — and it has to be **turned off** to be so, which is the
        whole reason this is not just a call.

        `clip` is a box in surface pixels, and every target the fog renders into
        is a different size: the march is `fog.resolution` of the surface and
        the medium map an eighth of the *scene*. GL's scissor is in absolute
        framebuffer pixels, so left enabled it cuts each of them at the same
        absolute column — which is one bug wearing two faces, and both were
        reported as such. At half resolution the march loses everything left of
        `clip.x`, and `_resolve` then stretches that boundary to twice the bar
        width: a hard vertical line across the stage with fog only to the right
        of it. The medium map is worse, because it is only ~192 texels wide: a
        clip starting past that misses it entirely, nothing is rasterised into
        the air, and a hazer running flat out puts no smoke on stage at all.

        Confining the fog to the stage image is `_resolve`'s job, and it is
        scissored — so the clip goes back on the way out.
        """
        self._fog_texture = None
        if not self._fog_wanted():
            return
        gl = self._fns
        gl.glDisable(_SCISSOR_TEST)
        try:
            self._fog_texture = self._fog.render(
                packed=self._packed, light_count=self._light_count,
                medium_count=self._medium_count, scene_to_clip=scene_to_clip,
                width=width, height=height, scene_rect=self.scene_rect,
                fog=self.fog, haze=self.haze, seconds=CLOCK.now())
        finally:
            self._set_clip(clip, height)

    def _settle(self, changed: bool) -> None:
        """Ask for one more frame while the fog is still converging.

        The march dithers each ray's start and averages the result with the last
        few frames, so a frame drawn on its own is noisy: after the picture
        stops changing it needs `CONVERGE_FRAMES` more to settle. This is the
        only place anything here asks for a repaint it was not given a reason
        for, so it is also the only place that could spin forever — and the
        countdown is what stops it. Resetting it every frame the march ran
        (rather than every frame the picture *changed*) is exactly that bug: the
        counter never reaches zero, every frame asks for another, and the stage
        redraws flat out for as long as fog is switched on.
        """
        if changed:
            self._converge = fog_module.CONVERGE_FRAMES
        if self._converge <= 0 or self.request_repaint is None:
            return
        self._converge -= 1
        self.request_repaint()

    def _frame_signature(self) -> tuple:
        """What the fog is a picture of, cheaply.

        Everything the march reads: the primitives themselves, the air they are
        in, and the settings shaping it. If none of it moved, the only thing
        left changing between frames is the dither — which is what converges.
        Smoke drifts every frame, so a running machine keeps this changing and
        the fog keeps being redrawn, which is correct: the picture really is
        different.
        """
        fog = self.fog
        return (
            bytes(self._packed) if self._packed is not None else b"",
            self._light_count, self._medium_count, round(self.haze, 4),
            self.scene_rect, None if fog is None else astuple(fog),
        )

    def _set_clip(self, clip, surface_height: int) -> None:
        """Confine every pass to the stage image."""
        gl = self._fns
        if clip is None or clip.isEmpty():
            gl.glDisable(_SCISSOR_TEST)
            return
        # GL's scissor box is measured from the BOTTOM of the surface.
        gl.glScissor(clip.x(), surface_height - clip.y() - clip.height(),
                     clip.width(), clip.height())
        gl.glEnable(_SCISSOR_TEST)

    def _restore(self, width: int, height: int) -> None:
        """Hand the context back the way Qt left it.

        `endNativePainting` restores what Qt itself tracks, not arbitrary state
        we turned on. Leaving the scissor box or additive blending enabled
        corrupts everything painted afterwards — which shows up as stale window
        content around the stage rather than as anything obviously GL-related.
        """
        gl = self._fns
        if gl is None:
            return
        try:
            gl.glDisable(_SCISSOR_TEST)
            gl.glDisable(_BLEND)
            gl.glBindTexture(_TEXTURE_2D, 0)
            gl.glViewport(0, 0, width, height)
        except Exception:                              # pragma: no cover
            pass

    def _pack(self) -> int:
        """Every body's primitives, offset into scene coordinates, in one buffer.

        The lists themselves are body-local — that is what lets a fixture be
        dragged without re-rendering — so the body's position is added here.

        Light and medium are separated on the way through, lights first: a BLOB
        is a parcel of smoke rather than something that glows, and the fog pass
        rasterises it into a density map instead of drawing it. One walk and one
        upload still, with the two ranges back to back — which is why the fog
        pass only has to move an offset rather than keep a buffer of its own.

        With fog off there is nothing to separate them for, so they stay
        together and a blob draws as the faint puff it always was.
        """
        split = self._fog_wanted()
        lights = []
        media = []
        light_count = 0
        medium_count = 0
        for body in self._bodies():
            primitives = getattr(body, "lights", None)
            if primitives is None or len(primitives) == 0:
                continue
            values = list(primitives.buffer())
            offset_x, offset_y = body.x(), body.y()
            for index in range(len(primitives)):
                base = index * STRIDE
                values[base + 1] += offset_x
                values[base + 2] += offset_y
                if split and values[base] == BLOB:
                    media.extend(values[base:base + STRIDE])
                    medium_count += 1
                else:
                    lights.extend(values[base:base + STRIDE])
                    light_count += 1

        total = light_count + medium_count
        # Dropped rather than left standing when the rig goes dark: the frame
        # carries on being drawn for the air alone, and `_frame_signature` would
        # otherwise describe an empty stage with the last lit one's primitives.
        chunks = lights + media
        self._packed = (ctypes.c_float * len(chunks))(*chunks) if chunks else None
        self._light_count = light_count
        self._medium_count = medium_count
        return total

    def _fog_wanted(self) -> bool:
        """Whether the volumetric march runs this frame."""
        return bool(self._fog is not None and self.fog is not None
                    and self.fog.enabled)

    def _air_worth_drawing(self) -> bool:
        """Whether the air alone earns a frame, with nothing emitting at all.

        A dark rig is not an empty frame once there is fog. The march's ambient
        pass lights the room's own baseline haze, and that is stage-wide: it
        needs no fixture, no parcel of smoke, nothing in the light list. So
        `count == 0` is not the same question as "is there anything to draw",
        and treating it as one is why the haze vanished the instant the last
        beam went out and the last puff aged away — which is the one moment a
        hazy room should read as most obviously hazy.

        Both halves of what the ambient pass needs are asked for, because
        without either it marches a fullscreen quad through nothing: `haze` is
        the medium it lights, and `light_intensity` is the light it lights it
        with.
        """
        return bool(self._fog_wanted() and self.haze > 0.0
                    and self.fog.light_intensity > 0.0)

    def air_is_drifting(self) -> bool:
        """Whether the air is a moving picture, and so owed frames of its own.

        The march's noise flows with the clock, so textured air genuinely looks
        different every frame with the whole rig standing perfectly still — and
        `_frame_signature` cannot see that, because time is not in it and must
        not be: putting it there would make `_settle` reset its own countdown
        every frame, which is precisely the spin that countdown exists to stop.

        So the drift is driven from outside instead, by the stage's shared 30 Hz
        clock, exactly like every other animation here. This is the question it
        asks, and all three parts of it are load-bearing: fog that is switched
        off has no noise, `texture` at 0 makes `turbulence()` return a constant,
        and air with nothing in it and nothing lighting it draws nothing to
        move. Any of those and the stage goes back to costing nothing at rest,
        which is the invariant the whole repaint scheme rests on.
        """
        if not self._fog_wanted() or self.fog.texture <= 0.0:
            return False
        return bool(self._light_count > 0 or self._medium_count > 0
                    or self._air_worth_drawing())

    def _ensure_fbo(self, width: int, height: int) -> None:
        from PyQt6.QtOpenGL import (
            QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
        )
        if self._fbo is not None and self._size == (width, height):
            return
        fmt = QOpenGLFramebufferObjectFormat()
        # The whole point: a float target, so light can exceed full scale and
        # still be there when the tonemap runs.
        fmt.setInternalTextureFormat(_RGBA16F)
        self._fbo = QOpenGLFramebufferObject(width, height, fmt)
        self._size = (width, height)

    def _upload_instances(self) -> None:
        """Put this frame's primitives on the GPU, before anything draws them.

        Its own step, and not the light pass's, because **the medium pass reads
        the same buffer** — the parcels of smoke sit after the lights in one
        upload, which is the whole reason there is only one. Left where it was,
        inside `_accumulate` and after its `count == 0` early return, a stage
        whose only emitter is a fog machine never uploaded anything at all:
        `light_count` is 0 for a hazer, so the buffer stayed at whatever size it
        last was — zero on the first frame — and `_build_medium` then drew a
        hundred and fifty instances out of it. That is a read off the end of a
        vertex buffer, which is undefined, and on the driver this was found on
        it took the process down.

        Reachable in ordinary use: a hazer running through a blackout, or a rig
        being patched fog-machine-first.
        """
        if self._packed is None:
            return
        payload = bytes(self._packed)
        self._instances.bind()
        self._instances.allocate(payload, len(payload))
        self._instances.release()

    def _accumulate(self, scene_to_clip: QMatrix4x4, count: int) -> None:
        gl = self._fns
        self._fbo.bind()
        gl.glViewport(0, 0, self._size[0], self._size[1])
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(_COLOR_BUFFER_BIT)
        if count == 0:
            # A dark rig, reached because the air is still worth a frame. The
            # clear is the part that had to happen anyway — this buffer is what
            # `_resolve` reads, and left alone it would hold the last frame's
            # light for as long as the fog kept the layer running.
            self._fbo.release()
            return
        gl.glEnable(_BLEND)
        gl.glBlendFunc(_ONE, _ONE)          # light adds; this is the whole idea

        self._vao.bind()
        program = self._light_program
        program.bind()
        program.setUniformValue("u_glow_reach", float(GLOW_REACH))
        program.setUniformValue("u_max_glow", float(MAX_GLOW_RADIUS_PX))
        program.setUniformValue("u_px_per_scene",
                                _px_per_scene(scene_to_clip, self._size[0]))
        program.setUniformValue("u_haze", float(self.haze))
        # With the march running it owns the shaft, and this pass keeps only the
        # lens — otherwise one beam would be drawn twice, once each way.
        program.setUniformValue("u_fog_active",
                                1.0 if self._fog_wanted() else 0.0)
        program.setUniformValue("u_scene_to_clip", scene_to_clip)

        self._quad.bind()
        corner = program.attributeLocation("a_corner")
        program.enableAttributeArray(corner)
        program.setAttributeBuffer(corner, _FLOAT, 0, 2, 0)
        gl.glVertexAttribDivisor(corner, 0)
        self._quad.release()

        self._instances.bind()
        stride = STRIDE * 4
        offset = 0
        enabled = []
        for name, width in shaders.INSTANCE_ATTRIBUTES:
            location = program.attributeLocation(name)
            if location >= 0:
                program.enableAttributeArray(location)
                program.setAttributeBuffer(location, _FLOAT, offset, width, stride)
                gl.glVertexAttribDivisor(location, 1)   # one set per primitive
                enabled.append(location)
            offset += width * 4
        self._instances.release()

        gl.glDrawArraysInstanced(_TRIANGLE_STRIP, 0, 4, count)

        for location in enabled:
            program.disableAttributeArray(location)
        program.disableAttributeArray(corner)
        program.release()
        self._vao.release()
        self._fbo.release()

    def _resolve(self, width: int, height: int) -> None:
        """Tonemap the HDR buffer over the scene the view already drew."""
        gl = self._fns
        gl.glViewport(0, 0, width, height)
        gl.glEnable(_BLEND)
        gl.glBlendFunc(_ONE, _ONE)

        self._vao.bind()
        program = self._resolve_program
        program.bind()
        gl.glActiveTexture(_TEXTURE0)
        gl.glBindTexture(_TEXTURE_2D, self._fbo.texture())
        # int overload -> the sampler's texture unit
        program.setUniformValue("u_hdr", 0)
        program.setUniformValue("u_exposure", float(self.exposure))

        # The fog rides in on a second sampler and is added before the tonemap,
        # so exposure and ACES cover it too — fog is just more light. Sampling
        # a smaller texture with a linear filter IS the upsample. With no march
        # this frame, unit 1 still needs a valid binding, so it gets the HDR
        # buffer with an amount of zero rather than a dangling sampler.
        gl.glActiveTexture(_TEXTURE1)
        gl.glBindTexture(_TEXTURE_2D, self._fog_texture or self._fbo.texture())
        program.setUniformValue("u_fog", 1)
        program.setUniformValue("u_fog_amount",
                                1.0 if self._fog_texture else 0.0)
        gl.glActiveTexture(_TEXTURE0)

        self._quad.bind()
        corner = program.attributeLocation("a_corner")
        program.enableAttributeArray(corner)
        program.setAttributeBuffer(corner, _FLOAT, 0, 2, 0)
        gl.glVertexAttribDivisor(corner, 0)
        self._quad.release()

        gl.glDrawArrays(_TRIANGLE_STRIP, 0, 4)

        program.disableAttributeArray(corner)
        program.release()
        gl.glActiveTexture(_TEXTURE1)
        gl.glBindTexture(_TEXTURE_2D, 0)
        gl.glActiveTexture(_TEXTURE0)
        gl.glBindTexture(_TEXTURE_2D, 0)
        self._vao.release()

    # -- teardown ------------------------------------------------------
    def cleanup(self) -> None:
        for buffer in (self._quad, self._instances):
            if buffer is not None:
                buffer.destroy()
        if self._vao is not None:
            self._vao.destroy()
        if self._fog is not None:
            self._fog.cleanup()
            self._fog = None
        self._quad = self._instances = self._vao = None
        self._fbo = None
        self._fog_texture = None
        self._light_program = self._resolve_program = None
        self._fns = None
        self.available = False


# The shader hardcodes these; if the Python side ever renumbers them the two
# would disagree silently and every primitive would draw as the wrong shape.
assert (POINT, CONE, QUAD, LINE, BLOB) == (0.0, 1.0, 2.0, 3.0, 4.0)
assert sum(width for _, width in shaders.INSTANCE_ATTRIBUTES) == STRIDE
