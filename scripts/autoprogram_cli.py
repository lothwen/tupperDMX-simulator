#!/usr/bin/env python3
"""The auto program from the command line: one command per part of autoprogram/.

    uv run python scripts/autoprogram_cli.py live AUDIO [--no-audio]
    uv run python scripts/autoprogram_cli.py analyse AUDIO [--bpm N] [--meter 4/4] [--bars]
    uv run python scripts/autoprogram_cli.py program SHOW_DIR SONG OUT.mid
                                    [--track guide|backing] [--palette SPEC]
                                    [--pace 0-8] [--seed N]
    uv run python scripts/autoprogram_cli.py score SHOW_DIR [SONG ...] [--out DIR]
                                    [--px-per-second N] [--from S] [--to S]

  live      play a file while printing the live analysers' onsets, tempo, beats
            and sections (streaming.bench); --no-audio runs silent at full speed.
  analyse   print the map analysis.py makes of a song's audio.
  program   write a take of a song's show to OUT.mid (take.generate). The show is
            never written: to use the result, close the show in the app (its
            autosave would write the old program back), take a version and copy
            OUT.mid over the song's .mid — or use ☰ ▸ Auto program… in the app.
  score     draw songs as light scores, DIR/<title>.png each (score.score); with
            no SONG, every song with a .mid.

SONG is the song's title, or its 1-based position in the setlist.
"""
import argparse
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtGui import QGuiApplication  # noqa: E402

from autoprogram import analysis, colours, decode, score, streaming, take  # noqa: E402
from autoprogram.director import DEFAULT_PACE, PACE_STEPS  # noqa: E402
from core.show import store  # noqa: E402


def live(args):
    streaming.bench(args.audio, *decode.decode(args.audio), play=not args.no_audio)


def analyse(args):
    song_map = analysis.analyse_samples(*decode.decode(args.audio), args.bpm, args.meter)
    print(analysis.describe(song_map, args.bars))


def program(args):
    show = store.load_show(args.show)
    song = _song(show, args.song)
    options = take.Options(args.track, args.pace, args.palette, args.seed)
    made = take.generate(show.fixtures, song, options, args.out,
                         bpm=take.song_tempo(song), stage=lambda _step, text: print(text))
    print("\n".join(made.summary))
    print(f"{made.events} events, {made.fades} fades -> {args.out}")


def draw(args):
    global _app                       # kept: text needs a GUI application
    _app = QGuiApplication.instance() or QGuiApplication([])
    show = store.load_show(args.show)
    songs = [_song(show, name) for name in args.songs] or show.playlist.songs
    os.makedirs(args.out, exist_ok=True)
    for song in songs:
        path = score.score(show, song, args.out, args.px_per_second, args.start, args.stop)
        print(f"{song.title}: " + (path or "no .mid, skipped"))


def _song(show, name):
    """The song `name` names: a title, or a 1-based setlist position."""
    songs = show.playlist.songs
    song = next((s for s in songs if s.title == name), None)
    if song is None and name.isdigit() and 1 <= int(name) <= len(songs):
        song = songs[int(name) - 1]
    if song is None:
        sys.exit(f"no song '{name}' in {show.root}: "
                 + ", ".join(f"{i + 1}={s.title!r}" for i, s in enumerate(songs)))
    return song


def _pace(text: str) -> int:
    value = int(text)
    if not 0 <= value <= PACE_STEPS:
        raise argparse.ArgumentTypeError(f"{value} is not within 0-{PACE_STEPS}")
    return value


def _palette(text: str) -> str:
    try:
        colours.palette(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[1:]))
    commands = ap.add_subparsers(dest="command", required=True)

    cmd = commands.add_parser("live", help="play a file, printing the live analysers")
    cmd.add_argument("audio", help="an audio file")
    cmd.add_argument("--no-audio", action="store_true",
                     help="skip playback and run the analysis at full speed")
    cmd.set_defaults(run=live)

    cmd = commands.add_parser("analyse", help="print the map of a song's audio")
    cmd.add_argument("audio", help="an audio file")
    cmd.add_argument("--bpm", type=float, help="the song's BPM, to search the grid around")
    cmd.add_argument("--meter", default="4/4")
    cmd.add_argument("--bars", action="store_true", help="also print every bar")
    cmd.set_defaults(run=analyse)

    cmd = commands.add_parser("program", help="write a take of a song's show to a .mid")
    cmd.add_argument("show", help="the .show folder")
    cmd.add_argument("song", help="the song's title, or its 1-based position in the setlist")
    cmd.add_argument("out", help="the .mid to write")
    cmd.add_argument("--track", choices=(take.GUIDE, take.BACKING),
                     help="which audio to analyse (default: the guide when there is one)")
    cmd.add_argument("--palette", type=_palette,
                     help="the colour story: a preset (" + ", ".join(sorted(colours.PALETTES))
                          + "); MODE:HUE, the fixture Palette tab's (modes "
                          + ", ".join(colours.MODES) + "; hues " + ", ".join(colours.HUES)
                          + " or random, drawn from the take); or custom:#RRGGBB,#RRGGBB,"
                            "#RRGGBB, primary, secondary and contrast (default: from the "
                            "song's key and drive)")
    cmd.add_argument("--pace", type=_pace, default=DEFAULT_PACE,
                     help=f"how often looks change, 0 (once a section) to {PACE_STEPS} (every "
                          "beat in a chorus, every half beat at the climax), each step about "
                          f"twice as often; default {DEFAULT_PACE}")
    cmd.add_argument("--seed", type=int,
                     help="another take of the same song: other lead roles, look orders "
                          "and lane patterns (default: one from the title)")
    cmd.set_defaults(run=program)

    cmd = commands.add_parser("score", help="draw songs as light scores")
    cmd.add_argument("show", help="the .show folder")
    cmd.add_argument("songs", nargs="*", help="titles or 1-based setlist positions (default: all)")
    cmd.add_argument("--out", default=".", help="the folder to write the images to")
    cmd.add_argument("--px-per-second", type=float, default=6.0,
                     help="horizontal scale (default 6)")
    cmd.add_argument("--from", dest="start", type=float, default=0.0, help="first second drawn")
    cmd.add_argument("--to", dest="stop", type=float, help="last second drawn")
    cmd.set_defaults(run=draw)

    args = ap.parse_args()
    try:
        args.run(args)
    except take.TakeError as e:
        sys.exit(str(e))


_app = None

if __name__ == "__main__":
    main()
