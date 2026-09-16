# ABOUTME: The song's marker track — voice cues plus the control markers
# ABOUTME: (tempo change, stop click) and their kind metadata. Qt-free.
"""What a song's timeline carries besides notes.

Four things ride a song's timeline that are neither audio nor DMX: a spoken
voice-TTS line, a tempo change, a stop-click, and the segue that hands the show
to the next song. They are edited together in one dialog and drawn together on the
ruler, so their identity — label, colour, glyph — is declared once, here, where
both `ui/` and `timeline/` can read it (a `timeline/` track must not import
`ui/`, so the shared half cannot live in either).

The voice kind keeps its own `SongConfig.voice_tts` list: it is the one kind
that pre-renders to PCM off-thread, and shows written before control markers
existed already persist it under that key. **That key is persistence, not the
name** — the marker is called Voice TTS everywhere a user can read it, while
every show on disk still says `voice_tts`.
"""

from typing import List, Optional, Sequence, Tuple

KIND_VOICE = "voice"
KIND_TEMPO = "tempo"
KIND_CLICK_STOP = "click_stop"

#: The kinds `SongConfig.control_markers` may hold (the voice kind has its own
#: list, and the segue is one per song rather than a marker in a list).
CONTROL_KINDS = (KIND_TEMPO, KIND_CLICK_STOP)

#: Every kind the marker dialog and the ruler draw, including the segue — which
#: is not a list entry but does get a marker on both strips.
KIND_SEGUE = "segue"

KIND_LABELS = {
    KIND_VOICE: "Voice TTS",
    KIND_TEMPO: "Tempo change",
    KIND_CLICK_STOP: "Stop click",
    KIND_SEGUE: "Segue",
}

#: One hue per kind, shared by the dialog's table/strip and the timeline ruler
#: so a marker is the same colour wherever it is drawn.
KIND_COLORS = {
    KIND_VOICE: "#3fd0c9",       # teal — the TTS/voice bus
    KIND_TEMPO: "#ba82f0",       # violet, like the MIDI accents
    KIND_CLICK_STOP: "#e2564a",  # red — something stops here
    KIND_SEGUE: "#e0c341",       # amber — the hand-off out of this song
}

#: The ruler/strip glyph for each kind (names in assets/icons/svg/).
KIND_ICONS = {
    KIND_VOICE: "speech",
    KIND_TEMPO: "music",
    KIND_CLICK_STOP: "close",
    KIND_SEGUE: "next",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def kind_color(kind: str) -> str:
    return KIND_COLORS.get(kind, "#888888")


def tempo_changes(markers: Sequence) -> List[Tuple[float, int]]:
    """The (seconds, bpm) pairs a TempoMap is built from."""
    return sorted((float(m.time), int(m.bpm)) for m in markers
                  if m.kind == KIND_TEMPO and m.bpm)


def click_stop_time(markers: Sequence) -> Optional[float]:
    """When the click stops, or None if it never does.

    The EARLIEST stop-click wins: the marker is one-way (there is no
    start-click), so a second one further down the song changes nothing."""
    stops = [float(m.time) for m in markers if m.kind == KIND_CLICK_STOP]
    return min(stops) if stops else None


def click_stopped_at(markers: Sequence, position: float) -> bool:
    """Whether the click is silent at `position` — the question every seek,
    resume and song load asks, since the marker's effect outlives its crossing."""
    stop = click_stop_time(markers)
    return stop is not None and position >= stop


def marker_at(markers: Sequence, time_sec: float, kind: Optional[str] = None):
    """The marker nearest `time_sec`, optionally restricted to one kind.

    A ruler drag reports what it moved as (kind, the time it sat at): the ruler
    draws tuples, not the song's marker objects, so this is how the edit finds
    the object to retime. Ties are harmless — two markers of one kind at the
    same instant are indistinguishable anyway."""
    candidates = [m for m in markers
                  if kind is None or getattr(m, "kind", None) == kind]
    if not candidates:
        return None
    return min(candidates, key=lambda m: abs(float(m.time) - float(time_sec)))


def ruler_rows(song) -> List[Tuple[float, str, str]]:
    """A song's control markers as the ruler draws them: (time, kind, label).

    Voice cues are NOT included — the ruler takes those from its own list, which
    the UI already keeps in sync for the tooltip text. The segue IS, because it
    has no other way onto the ruler."""
    rows = []
    for marker in getattr(song, "control_markers", []):
        label = f"{marker.bpm} BPM" if marker.kind == KIND_TEMPO else ""
        rows.append((float(marker.time), marker.kind, label))
    if getattr(song, "segue", False):
        rows.append((float(getattr(song, "segue_time", 0.0)), KIND_SEGUE,
                     "next song, no count-in"))
    return sorted(rows)


def earliest_marker(song) -> Optional[Tuple[float, str]]:
    """`(time, label)` of the earliest marker on `song`'s track, or None.

    The label names the marker as a user knows it — its kind, plus the line a
    voice cue speaks or the tempo a change sets — because it is what gets
    quoted when that marker is the one standing in the way of a shift.

    A segue at its 0.0 default is not counted: `segue_time` is kept even while
    `segue` is off (see SongConfig), so 0.0 there means never dialled rather
    than a marker at the top of the song."""
    rows = [(float(c.time), f"{kind_label(KIND_VOICE)} marker “{c.text}”")
            for c in getattr(song, "voice_tts", [])]
    for marker in getattr(song, "control_markers", []):
        label = f"{kind_label(marker.kind)} marker"
        if marker.kind == KIND_TEMPO:
            label += f" ({marker.bpm} BPM)"
        rows.append((float(marker.time), label))
    segue_time = float(getattr(song, "segue_time", 0.0))
    if segue_time > 0.0:
        rows.append((segue_time, f"{kind_label(KIND_SEGUE)} marker"))
    return min(rows, key=lambda row: row[0]) if rows else None


def earliest_marker_time(song) -> Optional[float]:
    """The earliest time anything on `song`'s marker track sits at, or None.

    Voice cues, control markers and the segue are three separate lists on the
    song, so "where does the marker track start" has no single owner to ask —
    which is what a rigid shift of the whole track needs before it can clamp."""
    first = earliest_marker(song)
    return first[0] if first is not None else None


def shift_markers(song, delta_sec: float) -> bool:
    """Slide the whole marker track by `delta_sec`, rigidly. Returns whether
    anything moved.

    Every kind moves — a tempo change is part of the arrangement just as much
    as a voice cue, so an audio offset the show follows must carry it too. The
    caller clamps: this shifts what it is given and only refuses to push a
    marker before t=0, where it would be unreachable."""
    if not delta_sec:
        return False
    earliest = earliest_marker_time(song)
    if earliest is None or earliest + delta_sec < 0.0:
        return False
    for cue in getattr(song, "voice_tts", []):
        cue.time = float(cue.time) + delta_sec
    for marker in getattr(song, "control_markers", []):
        marker.time = float(marker.time) + delta_sec
    if float(getattr(song, "segue_time", 0.0)) > 0.0:
        song.segue_time = float(song.segue_time) + delta_sec
    return True
