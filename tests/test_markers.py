"""Completeness guarantees for the worker→controller marker protocol.

The marker strings are a brittle-by-nature wire format; these tests pin the
two failure modes review §4.3 called out: a marker emitted without a handler,
and raw marker literals drifting away from the constants.
"""

import re
from pathlib import Path

from vanilla_wow_launcher.controllers.update import UpdateController
from vanilla_wow_launcher.services.update_backend import markers

SRC = Path(__file__).resolve().parents[1] / "src" / "vanilla_wow_launcher"

# Quoted marker literal, e.g. ("__DONE__", "") — what workers must not write.
_RAW_LITERAL_RE = re.compile(r"[\"']__[A-Z][A-Z_]+__[\"']")
_RAW_FSTRING_RE = re.compile(r"f[\"']__[A-Z][A-Z_]+_")


def test_every_marker_has_a_handler():
    handlers = UpdateController._MARKER_HANDLERS
    missing = [m for m in markers.ALL if m not in handlers]
    assert not missing, f"markers without a _handle_log entry: {missing}"


def test_handler_table_has_no_stale_entries():
    extra = set(UpdateController._MARKER_HANDLERS) - set(markers.ALL)
    assert not extra, f"handlers for unknown markers: {sorted(extra)}"


def test_no_raw_marker_literals_outside_markers_module():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "markers.py":
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (_RAW_LITERAL_RE, _RAW_FSTRING_RE):
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(SRC)}: {match.group()}")
    assert not offenders, "raw marker literals found:\n" + "\n".join(offenders)


def test_version_helpers_round_trip():
    msg = markers.VERSION_PREFIX + "1.12.1 (5875)"
    assert markers.is_version(msg)
    assert markers.version_of(msg) == "1.12.1 (5875)"
    assert not markers.is_version(markers.DONE)
