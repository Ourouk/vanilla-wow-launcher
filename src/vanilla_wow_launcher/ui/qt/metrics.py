"""UI metrics: responsive layout math for the Qt interface.

The interface is designed around fixed logical dimensions (1000x700). All
geometry flows through these pure helpers, which compute placements from a
*current* window size instead of hardcoded constants, so the UI can resize
and reflow:

- `initial_window_size` derives a fresh window size from the screen,
  optionally multiplied by a scale factor, capped so it never overflows.

Display scaling needs no detection here: Qt renders in logical pixels and
applies the display scale factor internally.
"""

# Logical design size (in "100%" pixels).
BASE_W = 1000
BASE_H = 700

# Typography scale — the only point sizes widgets should use. Ad hoc
# per-widget sizes drift; these tokens keep headings, body and hints on a
# consistent ladder.
PT_TITLE = 17  # header wordmark / window titles
PT_PAGE = 16  # big per-tab page titles ("CLIENT UPDATE")
PT_DIALOG = 13  # dialog header titles
PT_SECTION = 12  # panel section headers ("ANNOUNCEMENTS", gold headers)
PT_BODY = 10  # default row/list text
PT_HINT = 9  # dim explanatory hints
PT_ICON = 14  # icon-only toolbuttons (⚙)
PT_LINK_ICON = 15  # row website-link glyphs (⧉)
PT_BADGE = 8  # tab count badges

# Spacing scale — vertical rhythm and paddings across panels/dialogs.
PAD_S = 4
PAD_M = 8
PAD_L = 16
ROW_GAP = 6  # gap between rows inside a list panel
SECTION_GAP = 18  # gap between sections in a form/settings layout


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def initial_window_size(
    sw: int, sh: int, factor: float = 1.0
) -> tuple[int, int]:
    """Window size for a fresh start: the design size × `factor`, capped at
    ~90% of the screen so it never overflows on small displays."""
    w = BASE_W * factor
    h = BASE_H * factor
    max_w, max_h = int(sw * 0.92), int(sh * 0.92)
    return min(int(w), max_w), min(int(h), max_h)
