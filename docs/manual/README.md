# The user manual

Source for `docs/Manual_TupperDMX.pdf`.

```
01-introduction.md … 14-appendix-buildroot.md   chapters, in filename order
assets/manual.css                             the print stylesheet (A4)
images/                                       captured screenshots + diagrams
build_manual.py                               chapters -> one PDF
capture_screenshots.py                        the app -> images/*.png
make_diagrams.py                              the chapter 2 diagrams -> images/*.svg
```

## Rebuilding the PDF

```bash
python3 docs/manual/build_manual.py
```

Needs the `markdown` Python module and a Chromium/Chrome binary — neither is a
project dependency. If your interpreter has no `markdown`:

```bash
uv run --with markdown python docs/manual/build_manual.py
```

Useful flags: `--html-only` (keep the intermediate HTML for debugging),
`--output PATH`, `--open`.

Chapters are compiled in filename order; the numeric prefix *is* the order. The
cover page and the table of contents are generated from the chapter headings, so
adding a chapter means adding a file and nothing else.

**Figures are written as plain images with the caption in the alt text** —
`![Caption](images/foo.png)` on a line of its own becomes a numbered `<figure>`
with that caption. Keep them on their own line or they stay inline. SVG works
the same way as PNG.

**Cross-references are `[7.1](#71-show-the-project)`** — the anchor is the
heading run through Python-Markdown's slugify, which folds non-ASCII away and
collapses any run of spaces and hyphens to **one** hyphen. So `## 7.1 SHOW — the
project` is `#71-show-the-project`, not `#71-show--the-project`. Guessing these
by hand is how they end up broken; derive them from the heading.

## The operating-mode diagrams

```bash
python3 docs/manual/make_diagrams.py
```

Writes the three chapter-2 signal-chain SVGs. It is plain string formatting with
no dependencies: boxes on a shared grid, joined by labelled arrows, in the same
light palette as `manual.css`. Edit the `DIAGRAMS` table at the bottom to change
what a chain contains — box widths are derived from the node count, so a
three-stage and a four-stage chain still span the same width.

## Re-capturing the screenshots

```bash
uv run python docs/manual/capture_screenshots.py            # all figures
uv run python docs/manual/capture_screenshots.py --list     # figure names
uv run python docs/manual/capture_screenshots.py --only stage-view
```

This **drives the real application on the desktop session** — it raises and
focuses the window repeatedly for several minutes, and anything that steals
focus mid-run spoils a figure. Run it on an idle machine.

It is safe with respect to your data:

- the show is a `copytree` of `~/Documents/TupperDMX/Billares2026.show` into a
  temp folder, so the real project is never opened;
- `core.config.userprefs_path` is redirected to a patched copy of the real
  preferences, with every volume at zero, the MIDI hardware port disabled and
  logging at INFO (at DEBUG the stage prints its cost into every stage figure);
- a show that stars nothing gets one scene per fixture type starred **in the
  copy**, since the web remote's cue grid is the show's favourites.

The auto-program figures wait for a real analysis of the demo song, so that part
of the run takes as long as the analysis does.

Change `REAL_SHOW` at the top of the script to point it at a different project.

### Why not offscreen?

Two constraints rule out the obvious approaches:

- **`QWidget.grab()` cannot read back an OpenGL viewport.** It returns a blank
  image for the whole stage view, which is the one thing these figures exist to
  show.
- **A Wayland client is never told where it sits on the desktop**, so
  `frameGeometry()` and `mapToGlobal()` come back meaningless and every crop
  lands in the wrong place.

So the script forces `QT_QPA_PLATFORM=xcb` (real coordinates, still composited
normally) and captures with `spectacle`, cropping to each widget's global
geometry with ImageMagick. The web-remote figure is the exception: it is a
headless Chromium screenshot of the running server.
