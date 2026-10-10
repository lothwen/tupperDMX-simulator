#!/usr/bin/env python3
# ABOUTME: Compiles the chapter markdown files in docs/manual/ into one
# ABOUTME: print-ready PDF via the system markdown module and headless Chromium.
"""Build the TupperDMX Simulator user manual.

    python3 docs/manual/build_manual.py                 # -> docs/Manual_TupperDMX.pdf
    python3 docs/manual/build_manual.py --html-only     # keep the intermediate HTML
    python3 docs/manual/build_manual.py --output /tmp/m.pdf

Chapters are the `NN-*.md` files in this folder, compiled in filename order —
the numeric prefix *is* the order. A cover page and a table of contents are
generated from the chapter headings.

Two dependencies, both of which are already on most systems and neither of
which is a project dependency:

* the `markdown` Python module (Debian/Ubuntu `python3-markdown`,
  Arch `python-markdown`, or `pip install markdown`);
* `chromium` (or `google-chrome`), used headless to print the HTML to PDF.

If `markdown` is missing from the interpreter you run this with, try
`uv run --with markdown python docs/manual/build_manual.py`.
"""

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_OUTPUT = HERE.parent / "Manual_TupperDMX.pdf"

TITLE = "TupperDMX Simulator"
SUBTITLE = "User manual"
# The manual's own edition, printed on the cover. Bumped when the manual is
# revised; the application's version comes from its git tag, not from here.
VERSION = "1.1"
CHROME_CANDIDATES = ("chromium", "chromium-browser", "google-chrome",
                     "google-chrome-stable", "chrome")

MD_EXTENSIONS = ["toc", "tables", "fenced_code", "attr_list", "sane_lists",
                 "admonition", "md_in_html"]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def require_markdown():
    try:
        import markdown  # noqa: F401
    except ImportError:
        sys.exit(
            "The `markdown` module is not available to this interpreter.\n"
            "  Arch:          sudo pacman -S python-markdown\n"
            "  Debian/Ubuntu: sudo apt install python3-markdown\n"
            "  Anywhere:      uv run --with markdown python "
            "docs/manual/build_manual.py")
    import markdown
    return markdown


def find_chrome() -> str:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    sys.exit("No Chromium/Chrome found — install one of: "
             + ", ".join(CHROME_CANDIDATES))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def chapter_files() -> list[Path]:
    files = sorted(p for p in HERE.glob("[0-9][0-9]-*.md"))
    if not files:
        sys.exit(f"No chapter files (NN-*.md) found in {HERE}")
    return files


def figures_to_html(body: str, counter: list[int]) -> str:
    """Turn each standalone image paragraph into a numbered <figure>.

    The markdown is written as `![Caption](images/x.png)` on its own line, so
    the alt text doubles as the caption — one source of truth, and the alt
    stays useful for anything reading the HTML directly.
    """
    pattern = re.compile(
        r'<p>\s*<img alt="([^"]*)" src="([^"]+)"\s*/?>\s*</p>')

    def replace(match: re.Match) -> str:
        caption, src = match.group(1), match.group(2)
        counter[0] += 1
        number = f"Figure {counter[0]}"
        return (f'<figure><img alt="{caption}" src="{src}">'
                f'<figcaption><span class="fignum">{number}</span> — '
                f'{caption}</figcaption></figure>')

    return pattern.sub(replace, body)


def render_chapters(markdown_module) -> tuple[str, list[dict]]:
    """Render every chapter to HTML, collecting the heading tree for the TOC."""
    parts: list[str] = []
    toc: list[dict] = []
    figure_counter = [0]

    for path in chapter_files():
        md = markdown_module.Markdown(extensions=MD_EXTENSIONS,
                                      extension_configs={"toc": {"toc_depth": "1-3"}})
        body = md.convert(path.read_text(encoding="utf-8"))
        body = figures_to_html(body, figure_counter)
        parts.append(f'<section class="chapter">\n{body}\n</section>')
        toc.extend(md.toc_tokens)

    return "\n".join(parts), toc


def toc_html(tokens: list[dict]) -> str:
    """A two-level contents list from the collected heading tokens."""
    lines = ['<nav class="toc">', "<h2>Contents</h2>", "<ul>"]
    for top in tokens:
        lines.append(f'<li><a href="#{top["id"]}">{html.escape(top["name"])}</a>')
        children = top.get("children") or []
        if children:
            lines.append("<ul>")
            for child in children:
                lines.append(
                    f'<li><a href="#{child["id"]}">'
                    f'{html.escape(child["name"])}</a></li>')
            lines.append("</ul>")
        lines.append("</li>")
    lines += ["</ul>", "</nav>"]
    return "\n".join(lines)


def cover_html() -> str:
    logo = REPO / "assets" / "icons" / "TupperDMX-icon.png"
    logo_tag = (f'<img class="logo" src="{logo.as_uri()}" alt="">'
                if logo.exists() else "")
    return f"""<header class="cover">
{logo_tag}
<h1>{TITLE}</h1>
<p class="subtitle">{SUBTITLE} · audio, MIDI and DMX on one timeline</p>
<div class="meta">
<div><strong>Version</strong> &nbsp; {VERSION}</div>
<div><strong>Built</strong> &nbsp; {date.today().isoformat()}</div>
</div>
</header>"""


def build_html() -> str:
    markdown_module = require_markdown()
    body, tokens = render_chapters(markdown_module)
    css = (HERE / "assets" / "manual.css").read_text(encoding="utf-8")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{TITLE} — {SUBTITLE}</title>
<style>
{css}
</style>
</head>
<body>
{cover_html()}
{toc_html(tokens)}
{body}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def print_to_pdf(html_path: Path, output: Path) -> None:
    chrome = find_chrome()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="tupperdmx-manual-chrome-") as profile:
        result = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             f"--user-data-dir={profile}",
             "--no-pdf-header-footer",
             "--virtual-time-budget=20000",
             f"--print-to-pdf={output}", html_path.as_uri()],
            capture_output=True, text=True, timeout=300)
    if not output.exists():
        sys.exit(f"Chromium produced no PDF.\n{result.stderr.strip()[:800]}")


def describe(output: Path) -> None:
    size_mb = output.stat().st_size / (1024 * 1024)
    pages = ""
    if shutil.which("pdfinfo"):
        info = subprocess.run(["pdfinfo", str(output)],
                              capture_output=True, text=True)
        match = re.search(r"^Pages:\s+(\d+)", info.stdout, re.MULTILINE)
        if match:
            pages = f"{match.group(1)} pages, "
    print(f"wrote {output}  ({pages}{size_mb:.1f} MB)")


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"PDF path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--html-only", action="store_true",
                        help="write the intermediate HTML next to the PDF and stop")
    parser.add_argument("--open", action="store_true",
                        help="open the finished PDF")
    args = parser.parse_args()

    document = build_html()

    if args.html_only:
        target = args.output.with_suffix(".html")
        target.write_text(document, encoding="utf-8")
        print(f"wrote {target}")
        return 0

    # Chromium must load the HTML from the manual folder so that `images/…`
    # and the stylesheet's relative references resolve.
    staged = HERE / ".manual-build.html"
    staged.write_text(document, encoding="utf-8")
    try:
        print_to_pdf(staged, args.output)
    finally:
        staged.unlink(missing_ok=True)

    describe(args.output)
    if args.open:
        webbrowser.open(args.output.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
