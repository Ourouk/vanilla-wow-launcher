"""Launcher-configured color themes.

The app's color theme is driven by the server: `vanilla_wow_launcher.json`
may carry an optional ``"theme"`` object listing hex colors for the app's
color slots (e.g. ``{"C_GOLD": "#d4a02f"}``) plus an optional ``"logo"`` URL
for the header wordmark. Any theme problem silently falls back to the
default theme — a cosmetic setting must never stop the launcher from
starting.

This module is pure data + pure-stdlib logic (no Qt), so `core/launcher.py`
can stay toolkit-agnostic while the Qt layer binds the resulting colors.
"""

from urllib.parse import urlsplit

# The default (octowow) theme: the dark purple/gold palette. A theme dict is
# applied on top of this base, so a distribution only lists the slots it
# wants to change. Also the fallback whenever a configured theme is invalid.
DEFAULT_COLORS = {
    "C_BG": "#120e1a",
    "C_PANEL": "#161120",
    "C_HDR": "#0d0a14",
    "C_PANEL_BDR": "#261d3a",
    "C_DIVIDER": "#2a2142",
    "C_GOLD": "#c8922a",
    "C_GOLD_LT": "#e8b84b",
    "C_PURPLE": "#8a4fa5",
    "C_GREEN_BTN": "#4a7c2f",
    "C_GREEN_HOV": "#5a9438",
    "C_TEXT": "#d8d4cc",
    "C_TEXT_DIM": "#7a7670",
    "C_LOG_BG": "#0f0b16",
    "C_OK": "#6abf69",
    "C_ERR": "#bf6969",
    "C_MOD_HL": "#a8b83c",
    "C_PARCH": "#e9dcb8",
    "C_PARCH_BAND": "#ddcda0",
    "C_PARCH_LINE": "#c3b083",
    "C_PARCH_TITLE": "#7c5a12",
    "C_PARCH_TEXT": "#3a352a",
    "C_PARCH_DIM": "#8b8064",
    "C_PARCH_LINK": "#a3561c",
    "C_PARCH_EDGE": "#b7a678",
    "C_PINK": "#d76f9e",
    "C_PINK_LT": "#eb96ba",
    "C_WARN": "#d4b43c",
    "C_BTN_TEXT": "#ffffff",
}

# The color slots a theme may set — exactly the default theme's keys.
COLOR_KEYS = frozenset(DEFAULT_COLORS)

# The one non-color key a theme may carry: the header logo URL.
LOGO_KEY = "logo"


def _valid_hex(value) -> bool:
    """Whether a value is a 6-digit ``#rrggbb`` hex color string."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True


def _valid_logo_url(value) -> bool:
    """Whether a value is an https logo URL (no embedded credentials)."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    if not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname:
        return False
    if parts.username or parts.password:
        return False
    return True


def resolve_colors(spec) -> dict:
    """The effective color dict for a launcher ``theme`` value.

    Returns `DEFAULT_COLORS` whenever the spec is unusable — None, not a
    dict, empty, or containing anything invalid (an unknown color slot or a
    non-hex value). The ``logo`` key is not a color slot and is ignored here.
    A valid dict yields the default palette overlaid with the given colors.
    Never raises: a bad theme falls back, it does not break the launcher.
    """
    if not isinstance(spec, dict) or not spec:
        return DEFAULT_COLORS
    overrides = {}
    for key, value in spec.items():
        if key == LOGO_KEY:
            continue
        if key not in COLOR_KEYS or not _valid_hex(value):
            # A single bad entry poisons the whole theme — no partial themes.
            return DEFAULT_COLORS
        overrides[key] = value.strip()
    merged = dict(DEFAULT_COLORS)
    merged.update(overrides)
    return merged


def resolve_logo(spec) -> str | None:
    """The header logo URL from a launcher ``theme`` value, or None.

    Only a non-empty https URL is accepted (bad values fall back to no logo
    rather than an error). Never raises.
    """
    if not isinstance(spec, dict):
        return None
    value = spec.get(LOGO_KEY)
    if not _valid_logo_url(value):
        return None
    return value.strip()
