# ABOUTME: AboutDialog — modal showing app identity + build metadata (tag,
# ABOUTME: date, commit, branch, platform). Opened from the toolbar's ☰ menu.

from __future__ import annotations

import platform as _platform
import re
from datetime import datetime, timezone

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication, QPixmap
from PyQt6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from core import buildinfo
from core.paths import asset_path

_REPO_URL = "https://github.com/lothwen/tupperDMX-simulator"
_CHANGELOG_URL = _REPO_URL + "/commits"

# A clean release tag like "v3.4.2", and a pre-release like "v1.1-rc1", whose
# word names its channel. Anything else (describe output, "dev", "unknown") is
# treated as a development build.
_RELEASE_TAG_RE = re.compile(r"^v?\d+(\.\d+)+$")
_PRERELEASE_TAG_RE = re.compile(r"^v?\d+(\.\d+)+-(?P<channel>[a-z]+)\d*$")


def _platform_line() -> str:
    system = _platform.system()
    if system == "Darwin":
        name = f"macOS {_platform.mac_ver()[0]}".strip()
    else:
        name = f"{system} {_platform.release()}".strip()
    return f"{name} · {_platform.machine()}"


def _format_build_date(iso_date: str) -> str:
    try:
        parsed = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    except ValueError:
        return iso_date
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d · %H:%M UTC")


class AboutDialog(QDialog):
    """Modal showing the app identity and build provenance."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About")
        self.setObjectName("AboutDialog")  # styled in dark-theme.qss
        self.setModal(True)
        self.setFixedWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_hero())
        layout.addWidget(self._build_metadata())
        layout.addWidget(self._build_footer())

    # --- sections -------------------------------------------------------

    def _build_hero(self) -> QWidget:
        hero = QWidget()
        hero.setObjectName("aboutHero")
        hero.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        row = QHBoxLayout(hero)
        row.setContentsMargins(24, 22, 24, 22)
        row.setSpacing(18)

        icon = QLabel()
        dpr = self.devicePixelRatioF()
        pixmap = QPixmap(asset_path("icons", "TupperDMX-icon.png")).scaled(
            int(64 * dpr), int(64 * dpr),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        pixmap.setDevicePixelRatio(dpr)
        icon.setPixmap(pixmap)
        icon.setFixedSize(64, 64)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("TupperDMX")
        title.setObjectName("aboutTitle")
        text.addWidget(title)
        tagline = QLabel("DMX lighting design for live shows")
        tagline.setObjectName("aboutTagline")
        text.addWidget(tagline)
        text.addSpacing(8)

        pill_row = QHBoxLayout()
        pill_row.addWidget(self._version_pill())
        pill_row.addStretch(1)
        text.addLayout(pill_row)
        text.addStretch(1)
        row.addLayout(text, 1)
        return hero

    def _version_pill(self) -> QLabel:
        tag = buildinfo.GIT_TAG
        version = buildinfo.release_of(tag)
        prerelease = _PRERELEASE_TAG_RE.match(tag)
        if _RELEASE_TAG_RE.match(tag):
            dot_color, channel = "#43d17c", "STABLE"
        elif prerelease:
            dot_color, channel = "#79cdf0", prerelease.group("channel").upper()
        else:
            dot_color, channel = "#f0b429", "DEV"

        pill = QLabel(
            f'<span style="color:{dot_color}">●</span>'
            f'&nbsp;<span style="color:#d8efe0">{version}</span>'
            f'&nbsp;<span style="color:#8a8a8a">· {channel}</span>'
        )
        pill.setObjectName("aboutPill")
        pill.setTextFormat(Qt.TextFormat.RichText)
        return pill

    def _build_metadata(self) -> QWidget:
        section = QWidget()
        section.setObjectName("aboutBuild")
        section.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        grid = QGridLayout(section)
        grid.setContentsMargins(24, 16, 24, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        header = QLabel("BUILD")
        header.setObjectName("aboutSectionHeader")
        grid.addWidget(header, 0, 0, 1, 2)

        rows = [
            ("Release tag", buildinfo.GIT_TAG),
            ("Build date", _format_build_date(buildinfo.BUILD_DATE)),
        ]
        for row_index, (label_text, value_text) in enumerate(rows, start=1):
            grid.addWidget(self._row_label(label_text), row_index, 0)
            grid.addWidget(self._row_value(value_text), row_index, 1)

        grid.addWidget(self._row_label("Commit"), 3, 0)
        commit_cell = QHBoxLayout()
        commit_cell.setSpacing(8)
        commit_cell.addStretch(1)
        commit_cell.addWidget(self._row_value(buildinfo.GIT_COMMIT))
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setObjectName("aboutCopy")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.clicked.connect(self._copy_commit)
        commit_cell.addWidget(self._copy_btn)
        grid.addLayout(commit_cell, 3, 1)

        grid.addWidget(self._row_label("Branch"), 4, 0)
        grid.addWidget(self._row_value(buildinfo.GIT_BRANCH), 4, 1)
        grid.addWidget(self._row_label("Platform"), 5, 0)
        grid.addWidget(self._row_value(_platform_line()), 5, 1)

        self._copy_reset = QTimer(self)
        self._copy_reset.setSingleShot(True)
        self._copy_reset.setInterval(1200)
        self._copy_reset.timeout.connect(lambda: self._copy_btn.setText("Copy"))
        return section

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("aboutFooter")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        row = QHBoxLayout(footer)
        row.setContentsMargins(24, 12, 24, 12)
        row.setSpacing(8)

        copyright_label = QLabel(f"© {datetime.now().year} TupperDMX")
        copyright_label.setObjectName("aboutCopyright")
        row.addWidget(copyright_label)
        row.addStretch(1)

        changelog = QPushButton("Changelog")
        changelog.setObjectName("aboutLink")
        changelog.setCursor(Qt.CursorShape.PointingHandCursor)
        changelog.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_CHANGELOG_URL)))
        row.addWidget(changelog)

        website = QPushButton("Website ↗︎")  # ↗ forced to text style
        website.setObjectName("aboutWebsite")
        website.setCursor(Qt.CursorShape.PointingHandCursor)
        website.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(_REPO_URL)))
        row.addWidget(website)
        return footer

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _row_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("aboutRowLabel")
        return label

    @staticmethod
    def _row_value(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("aboutRowValue")
        label.setAlignment(Qt.AlignmentFlag.AlignRight |
                           Qt.AlignmentFlag.AlignVCenter)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _copy_commit(self):
        QGuiApplication.clipboard().setText(buildinfo.GIT_COMMIT)
        self._copy_btn.setText("Copied ✓")
        self._copy_reset.start()
