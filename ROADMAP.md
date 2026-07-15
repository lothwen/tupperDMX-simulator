# Roadmap TupperDMX Simulator

> Documento de producto, no de implementación. Ordena y desarrolla las ideas de futuro,
> añade mejoras y propone funcionalidades nuevas. **No entra en cómo implementar.**

## Contexto

TupperDMX hoy es una app PyQt6 sólida para un setup concreto: 1 controlador de LED
bar (7 canales) + 2 blinders (8 canales), universo fijo de 96 canales, timeline con
2 pistas fijas (`ledbar`, `blinder`), reproducción de un MP3 + metrónomo + secuencias
MIDI→DMX, y un controlador software emulado. Funciona, pero casi todo está **cableado
a esos fixtures concretos** (clases `LedBar`/`Blinder`, rangos de canal `LED@0 /
Blinder1@32 / Blinder2@64`, mapa MIDI notas 1–7, escenas `LedScene`/`BlinderScene`).

Las ideas de futuro apuntan en dos direcciones: **(a)** quitar ese cableado para poder
montar cualquier escenario, y **(b)** convertir la app en una herramienta de *directo*
real (player de click/backing, control remoto, gestión de shows, luces automáticas).

Criterio elegido: **base arquitectónica primero** — porque el sistema de fixtures es la
columna vertebral de la que cuelgan el resto de ideas. Y **personal ahora, producto
después**: priorizamos fiabilidad y ergonomía para el uso propio, pero diseñando las bases
(modelo de fixture, persistencia de show) para no cerrar la puerta a abrirlo a otros.

El hilo conductor del orden es la **dependencia técnica**: cada fase desbloquea la
siguiente, evitando rehacer trabajo.

---

## Orden propuesto (vista rápida)

| # | Fase | Idea(s) del TODO | Por qué aquí |
|---|------|------------------|--------------|
| 1 | Núcleo de fixtures generalizado | Sistema general de fixtures + rework midi map + 1 track por fixture | Spine: todo lo demás depende de esto |
| 2 | Patch y salida DMX repensados | DMX output / UI input-output Artnet-USB | Acompaña al modelo de fixture (patch = fixture→dirección) |
| 3 | Modelo de "Show" y persistencia | Gestión de canciones/playlists/shows, saves/exports/versionado | Base que sostiene el player y el control remoto |
| 4 | Live show player | Click/backing por canales L/R, precuenta auto, control track TTS | Necesita el modelo de show y la base de audio |
| 5 | UI remota web (SBC + tablet) | Webserver con UI web simple | Mejor sobre un núcleo y un modelo de show estables |
| 6 | Modo automático de luces | Análisis de música en tiempo real (mic/line-in) | El más independiente/experimental; puede ir en paralelo como I+D |

---

## Fase 1 — Núcleo de fixtures generalizado *(la columna vertebral)*

**Idea TODO:** "General fixture system — diseñador de escenarios, librería de fixtures,
formato OFL" + "Rework midi map → generalizado para cualquier fixture" + "One track per
fixture on the timeline". Se agrupan porque **son la misma refactorización vista desde
tres ángulos** y no tiene sentido hacerlas por separado.

**Qué es / alcance**
- Un **modelo de fixture data-driven**: cada fixture deja de ser una clase Python
  (`LedBar`, `Blinder`) y pasa a ser una definición (tipo, nº de canales, qué hace cada
  canal, cómo se renderiza). Eso permite tener N fixtures de tipos distintos.
- Una **librería de fixtures** importable, idealmente compatible con
  [Open Fixture Library (OFL)](https://open-fixture-library.org/) para no tener que
  describir cada cacharro a mano.
- Un **diseñador de escenario**: colocar fixtures en el stage virtual, asignarles tipo y
  dirección DMX, agruparlos.
- **Mapa MIDI generalizado**: hoy las notas 1–7 = canales del LED bar está cableado en
  varios sitios (`midi/constants.py CHANNEL_NOTE_MAP`, `clip_library.py`,
  `software_controller/main.py handle_note_on`). Pasa a derivarse del tipo de fixture.
- **Una pista de timeline por fixture** (no por categoría). El modelo de datos ya casi
  lo permite (cada evento lleva `info.fixture`); hoy solo el *instanciado* de pistas está
  cableado a 2 filtros. Ojo de UX: con muchos fixtures harán falta agrupar/colapsar/
  ocultar pistas para que el timeline siga siendo legible.

**Por qué aquí (primero)**
Casi todo está acoplado a los 2 tipos actuales: `fixtures.py`, `config.py`/`config.json`,
`software_controller/` (constantes de canal, escenas, `handle_note_on`),
`ui/stage_controller.py`, `ui/clip_library.py`, `timeline/`, `midi/constants.py`. Hacer
cualquier otra idea antes implicaría tocar este código cableado y luego rehacerlo. Es la
inversión que más trabajo futuro ahorra.

**Desglose sugerido (subfases)**
1. Modelo abstracto de fixture + escena genérica (canales variables) reemplazando
   `LedScene`/`BlinderScene` y las clases monolíticas.
2. Patch/config data-driven: `config.json` describe instancias con *tipo* + dirección
   (hoy solo guarda posición y `dmx_start`, sin tipo).
3. Importador de librería (OFL) + diseñador de escenario en el stage.
4. Mapa MIDI por tipo de fixture.
5. Timeline: pistas dinámicas (1 por fixture) con agrupado/colapso.

**Nota producto-después:** este es *el* punto donde se decide si el proyecto puede crecer
a otros usuarios. Merece el diseño más cuidadoso aunque hoy solo se usen 3 fixtures.

---

## Fase 2 — Patch y salida DMX repensados

**Idea TODO:** "DMX output → repensar UI input/output Artnet/USB".

**Qué es / alcance**
- Hoy la salida DMX ya es razonablemente flexible (combo en toolbar + diálogo de ajustes
  con IP/puerto/universo Art-Net, USB FTDI, hot-swap). Lo que falta es **el concepto de
  patch**: qué fixture vive en qué universo y dirección, y por qué salida sale.
- Repensar la UI para separar claramente: **entrada** (qué controla la app: MIDI, click,
  software emulado) vs **salida** (Art-Net / USB / multi-universo).
- **Multi-universo**: hoy el universo es de 96 canales fijos. En cuanto haya N fixtures
  arbitrarios, 1 universo de 96 se queda corto.

**Por qué aquí**
El patch es la otra cara del modelo de fixture (un fixture *se direcciona*). Hacerlo justo
después de la Fase 1 cierra el bucle "defino fixture → lo coloco → lo direcciono → sale por
X". Antes de la Fase 1 no hay nada estable que direccionar.

**Mejora concreta detectada:** la separación in/out actual está algo difusa (selector de
fuente DMX y de puerto MIDI conviven en la toolbar). Una pantalla de "Setup E/S" unificada
ayudaría mucho de cara a un SBC headless (Fase 5).

---

## Fase 3 — Modelo de "Show" y persistencia

**Idea TODO:** "Gestión de canciones, playlists, shows → saves, exports, incluso gestión
de versiones integrada".

**Qué es / alcance**
- Hoy **no existe el concepto de "show" ni "proyecto"**: la playlist es una lista de
  `SongConfig` (ruta midi + ruta audio + offset) en `userprefs.json`, y **las ediciones de
  clips/escenas son solo en memoria** — se pierden si no exportas el MIDI a mano. Esto es un
  punto de fricción real y diario.
- Un **fichero de Show** que empaquete: patch de fixtures + setlist + secuencias MIDI por
  canción + ajustes (BPM, offsets, salidas). Un solo archivo que abres y "ya está montado".
- **Autosave por canción** (mata el "se me han ido las ediciones").
- **Export** (a MIDI estándar, a bundle portable).
- **Versionado integrado**: historial de cambios del show / snapshots. (Para "producto
  después" esto puede ser tan simple como git por debajo o snapshots con nombre.)

**Por qué aquí**
El player de directo (Fase 4) y la UI remota (Fase 5) necesitan algo que cargar/guardar de
forma fiable. Un "show" bien definido es el contenedor natural de todo. Además, ahora que el
modelo de fixture es data-driven (Fase 1), el patch *forma parte* del show.

**Cosas a mejorar que encajan aquí**
- Autosave + recuperación ante crash (ya hay un `test_crash_repro.py`, señal de que importa).
- Plantillas de show / duplicar show.

---

## Fase 4 — Live show player

**Idea TODO:** "Live show player: click/backing track por distintos canales (L/R),
precuenta automática activada/desactivada, control track con text-to-speech".

**Qué es / alcance**
- Hoy hay **una sola salida de audio** (`QMediaPlayer`, sin ruteo de canales) y un metrónomo
  **independiente** (`QAudioSink`, beeps). No hay separación click/backing ni TTS.
- **Click + backing por canales separados**: el click (y guías) van a un canal/salida (p.ej.
  monitores del músico) y el backing al PA. Es la feature estrella de cualquier player de
  directo (estilo Playaudio12 / MultiTracks / Ableton).
- **Precuenta automática** configurable (on/off, nº de compases) entre canciones. El motor de
  metrónomo actual ya calcula el siguiente beat con precisión: buena base conceptual.
- **Control track con text-to-speech**: avisos hablados al músico ("siguiente: estribillo",
  "entra batería"), generados por TTS y ruteados al canal de monitores.

**Por qué aquí**
Necesita el modelo de show (Fase 3) para saber qué setlist/precuentas hay, y necesita
repensar el subsistema de audio (multi-salida) que hoy es mono-salida. Es donde la app pasa
de "editor" a "instrumento de directo".

**Nota:** multi-salida/ruteo de canales es un cambio de fondo en audio — conviene tratarlo
como su propia subfase antes de TTS y precuenta.

---

## Fase 5 — UI remota web (SBC + tablet)

**Idea TODO:** "Webserver con UI web simple → UI remota desde tablet, app corriendo en un
SBC en el rack".

**Qué es / alcance**
- Hoy **no hay nada de web/HTTP/remoto**; la app es solo desktop PyQt6.
- Un **servidor embebido** + **UI web ligera** para controlar lo esencial desde una tablet
  (play/pause/stop, siguiente/anterior, blackout/pánico, ver canción actual, disparar cues).
- Pensado para que la app corra *headless* en un **SBC en el rack** y se maneje desde el
  escenario/FOH con la tablet.

**Por qué aquí**
Una UI remota expone un *modelo*. Si se hace antes de estabilizar fixtures (1), patch (2) y
show (3), se reescribe la API cada vez que cambie el núcleo. Sobre una base estable, la web es
una capa fina.

**Mejoras que la habilitan**
- Modo headless real (la app hoy asume GUI; correr en SBC sin pantalla necesita separar
  núcleo de UI — algo que las fases anteriores ya empujan).
  ✅ *Hecho (2026-07): núcleo separado en `app/` (`AppCore` compartido por
  MainWindow y headless) y `main.py --headless` arranca controlador + I/O +
  reproducción + show + web remote sin GUI. Pendiente: validar audio/piper en
  el SBC real.*
- Botón de **pánico/blackout** accesible (seguridad en directo) — útil ya, encaja aquí.

---

## Fase 6 — Modo automático de luces

**Idea TODO:** "Modo automático de luces: análisis en tiempo real de la música (micro o
line-in) que reacciona a intensidad, ritmo, etc."

**Qué es / alcance**
- Hoy el análisis con librosa es **offline sobre fichero** (waveform + BPM desde el tempo del
  MIDI). No hay entrada de audio en tiempo real.
- Captura de **mic/line-in** + análisis en vivo (energía/RMS, bandas de frecuencia, detección
  de beat/onset) que mapea a intensidad, color y movimiento de los fixtures.
- Modo "fiesta/jam" sin secuencia programada: las luces reaccionan solas.

**Por qué al final**
Es la idea **más independiente** del resto (no depende del show ni del player) y la más
experimental/I+D. Puede arrancarse en paralelo como prototipo cuando apetezca, pero da más
fruto cuando el modelo de fixture/grupos (Fase 1) ya existe, porque "reaccionar a la música"
es mucho más potente sobre grupos de fixtures que sobre canales sueltos.

---

## Mejoras transversales (cosas a mejorar)

Detectadas explorando el código; conviene abordarlas *dentro* de la fase donde encajan, no
como big-bang:

- **Persistencia de ediciones (urgente):** las ediciones de clips/escenas son solo en
  memoria; se pierden sin export manual. → Autosave (Fase 3).
- **Deuda técnica de cableado:** rangos de canal, mapa MIDI y escenas cableados a los 2
  fixtures actuales en muchos ficheros. → Se paga en Fase 1.
- **Red de seguridad de tests:** ya hay suite de caracterización (`tests/`, `uv run pytest`,
  `test_crash_repro.py`). Mantenerla y *ampliarla antes* de cada refactor grande para no
  romper comportamiento. Es lo que hace viable la Fase 1.
- **Separación núcleo / UI:** hoy todo asume GUI. Empujar la lógica fuera de la UI habilita
  headless (Fase 5) y tests más fáciles.
- **Multi-universo:** el límite de 96 canales / 1 universo se queda corto en cuanto haya
  fixtures arbitrarios (Fase 2).
- **Setup E/S unificado:** entrada (MIDI/click) y salida (Art-Net/USB) hoy conviven en la
  toolbar de forma difusa (Fase 2).

---

## Ideas adicionales (propuestas)

Priorizadas por encaje con el uso de directo; no están en el TODO original:

- **Cue list / botón GO (alta utilidad):** además del timeline scriptado, una pila de cues
  manuales que se disparan con un botón. Para momentos no cronometrados (charlas, bises).
  Encaja tras Fase 3 y brilla en la tablet (Fase 5).
- **Busking / overrides en vivo (alta):** sobreescribir color/intensidad/blackout a mano
  durante la reproducción sin tocar la secuencia. Imprescindible en directo real.
- **Mapeo de superficie de control MIDI (alta):** pedalera/Launchpad/APC para disparar
  cues, tap-tempo, blackout con los pies. Hay infra MIDI; sería un consumidor más.
- **Sync a timecode externo (MTC/LTC) o Ableton Link (media):** para tocar con otros
  sistemas/PA con timecode común. Relevante en sitios con backline serio.
- **Tap-tempo + detección de BPM en vivo (media):** complementa el modo automático y el
  metrónomo; útil cuando no hay MIDI con tempo.
- **Motor de efectos genérico (media):** chases, arcoíris, sinusoidales sobre *grupos* de
  fixtures. Mucho más potente con el modelo data-driven de Fase 1.
- **Grupos y macros de fixtures (media):** seleccionar "todos los pares", "frontales", etc.
  Habilitador del motor de efectos y del modo automático.
- **Fixtures con movimiento/beam (movers, gobos) en el visualizador (baja/futuro):** si la
  librería OFL entra, el stage virtual querrá representar más que barras y blinders.
- **Undo/redo en el timeline (media):** edición clip-style sin deshacer es frágil.
- **Backup/restore de shows (baja):** y, "producto después", sync opcional a la nube.

---

## Principios de ejecución y validación

Aunque esto es un roadmap, conviene fijar cómo se valida cada avance (el repo ya tiene la
infraestructura para ello):

- **Cada refactor de fundación (Fases 1–2) va guardado por la suite de caracterización**
  (`uv run pytest`). Ampliar tests *antes* de tocar el código cableado; el comportamiento
  observable (DMX que sale, render del stage) no debe cambiar mientras se generaliza.
- **"Done" por fase:**
  - F1: se puede definir un fixture nuevo por datos (no por código), colocarlo, verlo en
    stage, editarlo en su propia pista y que salga DMX correcto.
  - F2: patch multi-universo configurable; in/out claramente separados en UI.
  - F3: se abre un fichero de show y queda todo montado; las ediciones persisten solas.
  - F4: click y backing salen por canales distintos; precuenta on/off; aviso TTS audible.
  - F5: se controla play/stop/next/blackout desde una tablet contra la app en SBC headless.
  - F6: con line-in, las luces reaccionan a intensidad/beat sin secuencia programada.
- **Regla de oro:** no empezar una fase sin que la anterior sea usable. Personal-ahora exige
  que la app siga tocando shows reales en cada punto del camino.
