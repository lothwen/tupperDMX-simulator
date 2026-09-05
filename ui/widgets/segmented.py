# ABOUTME: The segmented switch — N exclusive buttons butted into one pill,
# ABOUTME: shared by the timeline toolbar and the fixture dialog.
"""One way to pick one of a few modes.

    ┌───────┬───────┐        ┌────┬────┬────┐
    │ Bars  │ Time  │        │ S  │ M  │ L  │
    └───────┴───────┘        └────┴────┴────┘

Buttons butted together at zero spacing, only the ends rounded, and the seam
between two segments drawn as ONE border rather than two — which is what the
`seg` property on the end segments is for. The look is `QPushButton#TTSegment`
in the stylesheet, named for the timeline toolbar it started in; the rule is no
longer scoped to that toolbar, because this is now the app's segmented switch
wherever one is wanted.

Nothing here decides how it looks. It exists because the construction is fiddly
in a way that goes wrong quietly — the button group has to be owned or Python
collects it and exclusivity silently stops working, and a segment sized while
unchecked comes back clipped, since the stylesheet makes the checked one BOLD
and a size hint is measured in the font the widget has at the time ("Seconds"
arriving as "econd").
"""

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFont, QFontMetrics, QIcon
from PyQt6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QPushButton, QSizePolicy, QWidget,
)

# Room for the stylesheet's horizontal padding and borders around a label, on
# top of the width its own text needs.
_LABEL_PADDING = 20


@dataclass(frozen=True)
class Segment:
    """One choice. `value` is what `on_pick` is handed and what keys the
    returned buttons; `text` is what the segment reads (empty for an icon)."""
    value: str
    text: str = ""
    tooltip: str = ""
    icon: Optional[QIcon] = None


def segmented(segments: Sequence[Segment],
              on_pick: Optional[Callable[[str], None]] = None,
              checked: Optional[str] = None,
              compact: bool = False,
              icon_size: int = 11,
              parent: Optional[QWidget] = None) -> Tuple[QWidget,
                                                         Dict[str, QPushButton]]:
    """The switch, and its buttons by value.

    `compact` fixes every segment to one narrow width — for single letters
    (Rows S/M/L) and for icon-only segments, which have no text to size to.
    """
    holder = QWidget(parent)
    # Fixed, so a crowded row squeezes its stretchy neighbours and never this:
    # a compressed segment clips its own labels rather than scrolling them.
    holder.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    # Parented to the holder: a QButtonGroup that goes out of scope takes the
    # exclusivity with it, and the switch starts letting two segments check.
    group = QButtonGroup(holder)
    group.setExclusive(True)

    buttons: Dict[str, QPushButton] = {}
    for index, segment in enumerate(segments):
        button = QPushButton(segment.text)
        button.setObjectName("TTSegment")
        if index == 0:
            button.setProperty("seg", "left")
        elif index == len(segments) - 1:
            button.setProperty("seg", "right")
        if compact:
            button.setProperty("compact", "true")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if segment.icon is not None:
            button.setIcon(segment.icon)
            button.setIconSize(QSize(icon_size, icon_size))
        if segment.tooltip:
            button.setToolTip(segment.tooltip)
        if segment.text and not compact:
            button.ensurePolished()
            bold = QFont(button.font())
            bold.setBold(True)
            button.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(
                segment.text) + _LABEL_PADDING)
        button.setChecked(segment.value == checked)
        if on_pick is not None:
            button.clicked.connect(
                lambda _checked, value=segment.value: on_pick(value))
        group.addButton(button)
        layout.addWidget(button)
        buttons[segment.value] = button
    return holder, buttons
