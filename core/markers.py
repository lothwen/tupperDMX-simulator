# ABOUTME: The song's marker track — voice cues plus the control markers
# ABOUTME: (tempo change, stop click) and their kind metadata. Qt-free.
"""What a song's timeline carries besides notes.

Four things ride a song's timeline that are neither audio nor DMX: a spoken
voice cue, a tempo change, a stop-click, and the segue that hands the show to
the next song. They are edited together in one dialog and drawn together on the
ruler, so their identity — label, colour, glyph — is declared once, here, where
both `ui/` and `timeline/` can read it (a `timeline/` track must not import
`ui/`, so the shared half cannot live in either).

Voice cues stay in their own `SongConfig.voice_cues` list: they are the one
kind that pre-renders to PCM off-thread, and shows written before control
markers existed already persist them under that key.
"""

from typing import List, Optional, Sequence, Tuple

KIND_VOICE = "voice"
KIND_TEMPO = "tempo"
KIND_CLICK_STOP = "click_stop"

#: The kinds `SongConfig.control_markers` may hold (voice cues have their own
#: list, and the segue is one per song rather than a marker in a list).
CONTROL_KINDS = (KIND_TEMPO, KIND_CLICK_STOP)

#: Every kind the marker dialog and the ruler draw, including the segue — which
#: is not a list entry but does get a marker on both strips.
KIND_SEGUE = "segue"

KIND_LABELS = {
    KIND_VOICE: "Voice cue",
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
