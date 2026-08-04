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
* **The surface format must force desktop GL and must not ask for a version.**
  See `configure_surface_format`.
* **The draw calls come from `QOpenGLFunctions_4_1_Core`.** PyQt6 exposes the
  shader program, the buffers, the FBO and the VAO, but no `glDrawArrays`;
  those live on the version-functions object. PyOpenGL is not a substitute — it
  works on GL ES and errors with "no valid context" on desktop GL.

Everything here fails soft. A context that will not give us a float
framebuffer, a shader that will not compile, a driver that is simply absent:
all of them log once and leave `available` False, and the stage carries on
drawing exactly as it did before.
"""

import ctypes
from typing import Callable, Iterable, Optional

from PyQt6.QtGui import QMatrix4x4, QOpenGLContext, QSurfaceFormat

from tupperlogger import get_logger
from ui.stage.light import BLOB, CONE, LINE, POINT, QUAD, STRIDE
from ui.stage.gpu import shaders

logger = get_logger("ui.stage.gpu")

_FLOAT = 0x1406          # GL_FLOAT
_TRIANGLE_STRIP = 0x0005
_BLEND = 0x0BE2
_ONE = 1
_RGBA16F = 0x881A
_TEXTURE_2D = 0x0DE1
_TEXTURE0 = 0x84C0
_COLOR_BUFFER_BIT = 0x00004000
_SCISSOR_TEST = 0x0C11

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


def configure_surface_format() -> None:
    """Ask for the context the layer needs. Must run before `QApplication`.

    Two lines, and what is *missing* matters as much as what is here:

    * `setRenderableType(OpenGL)` is mandatory. Without it a Wayland/EGL
      session hands out **OpenGL ES**, where `QOpenGLVersionFunctionsFactory`
      returns nothing and none of the shaders compile.
    * `setVersion` / `setProfile` must NOT be called. Requesting 3.3 Core fails
      outright there (`EGL_BAD_MATCH`) and takes Qt's own backing store down
      with it — the window comes up blank, with no obvious connection to the
      request that caused it.
    """
    fmt = QSurfaceFormat()
    fmt.setAlphaBufferSize(8)
    fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    QSurfaceFormat.setDefaultFormat(fmt)


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
        self.available = False
        self.exposure = 1.0
        self.haze = 0.0
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

        profile = QOpenGLVersionProfile()
        profile.setVersion(4, 1)
        profile.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        self._fns = QOpenGLVersionFunctionsFactory.get(profile, context)
        if self._fns is None:
            return self._give_up("no OpenGL 4.1 core functions on this context")
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

        self.available = True
        version = context.format()
        logger.info("GPU light layer ready (OpenGL %d.%d)",
                    version.majorVersion(), version.minorVersion())
        return True

    def _build(self, program_cls, shader_cls, vert, frag, name):
        program = program_cls()
        if not program.addShaderFromSourceCode(shader_cls.ShaderTypeBit.Vertex, vert):
            return self._give_up(f"{name} vertex shader: {program.log()}") or None
        if not program.addShaderFromSourceCode(shader_cls.ShaderTypeBit.Fragment, frag):
            return self._give_up(f"{name} fragment shader: {program.log()}") or None
        if not program.link():
            return self._give_up(f"{name} link: {program.log()}") or None
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
        if count == 0:
            return True          # nothing lit is a perfectly good frame

        try:
            self._ensure_fbo(width, height)
            self._set_clip(clip, height)
            self._accumulate(scene_to_clip, count)
            self._resolve(width, height)
        except Exception as exc:                       # pragma: no cover
            return self._give_up(f"draw failed: {exc!r}")
        finally:
            self._restore(width, height)
        return True

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
        """Every body's light, offset into scene coordinates, in one buffer.

        The lists themselves are body-local — that is what lets a fixture be
        dragged without re-rendering — so the body's position is added here.
        """
        chunks = []
        total = 0
        for body in self._bodies():
            lights = getattr(body, "lights", None)
            if lights is None or len(lights) == 0:
                continue
            values = list(lights.buffer())
            offset_x, offset_y = body.x(), body.y()
            for index in range(len(lights)):
                base = index * STRIDE
                values[base + 1] += offset_x
                values[base + 2] += offset_y
            chunks.extend(values)
            total += len(lights)

        if total:
            self._packed = (ctypes.c_float * len(chunks))(*chunks)
        return total

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

    def _accumulate(self, scene_to_clip: QMatrix4x4, count: int) -> None:
        gl = self._fns
        self._fbo.bind()
        gl.glViewport(0, 0, self._size[0], self._size[1])
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(_COLOR_BUFFER_BIT)
        gl.glEnable(_BLEND)
        gl.glBlendFunc(_ONE, _ONE)          # light adds; this is the whole idea

        self._vao.bind()
        program = self._light_program
        program.bind()
        program.setUniformValue("u_glow_reach", float(GLOW_REACH))
        program.setUniformValue("u_max_glow", float(MAX_GLOW_RADIUS_PX))
        program.setUniformValue("u_haze", float(self.haze))
        program.setUniformValue("u_scene_to_clip", scene_to_clip)

        self._quad.bind()
        corner = program.attributeLocation("a_corner")
        program.enableAttributeArray(corner)
        program.setAttributeBuffer(corner, _FLOAT, 0, 2, 0)
        gl.glVertexAttribDivisor(corner, 0)
        self._quad.release()

        self._instances.bind()
        payload = bytes(self._packed)
        self._instances.allocate(payload, len(payload))
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

        self._quad.bind()
        corner = program.attributeLocation("a_corner")
        program.enableAttributeArray(corner)
        program.setAttributeBuffer(corner, _FLOAT, 0, 2, 0)
        gl.glVertexAttribDivisor(corner, 0)
        self._quad.release()

        gl.glDrawArrays(_TRIANGLE_STRIP, 0, 4)

        program.disableAttributeArray(corner)
        program.release()
        gl.glBindTexture(_TEXTURE_2D, 0)
        self._vao.release()

    # -- teardown ------------------------------------------------------
    def cleanup(self) -> None:
        for buffer in (self._quad, self._instances):
            if buffer is not None:
                buffer.destroy()
        if self._vao is not None:
            self._vao.destroy()
        self._quad = self._instances = self._vao = None
        self._fbo = None
        self._light_program = self._resolve_program = None
        self._fns = None
        self.available = False


# The shader hardcodes these; if the Python side ever renumbers them the two
# would disagree silently and every primitive would draw as the wrong shape.
assert (POINT, CONE, QUAD, LINE, BLOB) == (0.0, 1.0, 2.0, 3.0, 4.0)
assert sum(width for _, width in shaders.INSTANCE_ATTRIBUTES) == STRIDE
