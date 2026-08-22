"""Worker→controller control markers for the client-update lifecycle.

Workers push ``(marker, tag)`` tuples through their log queue;
``UpdateController._handle_log`` dispatches them to state transitions. The
strings are a wire format shared by the worker engines and the controller —
always refer to them via these constants. A source-completeness test
(``tests/test_markers.py``) forbids raw marker literals anywhere else.
"""

DONE = "__DONE__"
ERROR = "__ERROR__"
MANIFEST_AVAILABLE = "__MANIFEST_AVAILABLE__"
MANIFEST_UNAVAILABLE = "__MANIFEST_UNAVAILABLE__"
UP_TO_DATE = "__UP_TO_DATE__"
UPDATE_NEEDED = "__UPDATE_NEEDED__"
DIFF_TREE = "__DIFF_TREE__"

TORRENT_REACHABLE = "__TORRENT_REACHABLE__"
TORRENT_UNREACHABLE = "__TORRENT_UNREACHABLE__"
TORRENT_CORRUPT = "__TORRENT_CORRUPT__"
TORRENT_STALLED = "__TORRENT_STALLED__"
TORRENT_SESSION_ERROR = "__TORRENT_SESSION_ERROR__"
TORRENT_DISK_ERROR = "__TORRENT_DISK_ERROR__"
TORRENT_VERIFY_FAILED = "__TORRENT_VERIFY_FAILED__"
TORRENT_DIFF = "__TORRENT_DIFF__"
TORRENT_UP_TO_DATE = "__TORRENT_UP_TO_DATE__"
TORRENT_RECOVERY_DONE = "__TORRENT_RECOVERY_DONE__"

# Not a fixed marker: emitted as VERSION_PREFIX + "<version string>".
VERSION_PREFIX = "__VERSION__"

ALL = (
    DONE,
    ERROR,
    MANIFEST_AVAILABLE,
    MANIFEST_UNAVAILABLE,
    UP_TO_DATE,
    UPDATE_NEEDED,
    DIFF_TREE,
    TORRENT_REACHABLE,
    TORRENT_UNREACHABLE,
    TORRENT_CORRUPT,
    TORRENT_STALLED,
    TORRENT_SESSION_ERROR,
    TORRENT_DISK_ERROR,
    TORRENT_VERIFY_FAILED,
    TORRENT_DIFF,
    TORRENT_UP_TO_DATE,
    TORRENT_RECOVERY_DONE,
)


def is_version(msg: str) -> bool:
    """Whether a queued message carries a client-version payload."""
    return msg.startswith(VERSION_PREFIX)


def version_of(msg: str) -> str:
    """The version string carried by a ``__VERSION__…`` message."""
    return msg[len(VERSION_PREFIX) :]
