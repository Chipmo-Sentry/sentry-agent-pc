"""Scrub embedded credentials from strings before they hit logs / GUI / heartbeat.

RTSP/HTTP source URLs carry ``scheme://user:pass@host/...``. ffmpeg echoes that
full URL in its stderr error lines, and the backend can echo a submitted
``rtsp_url`` in an error body. Anything that lands in ``last_error``, a log
message, or an exception text must run through :func:`scrub_credentials` first so
camera passwords never leak.
"""

from __future__ import annotations

import re

# Match the ``user:pass@`` (or bare ``user@``) credential block right after a
# URL scheme and replace it with ``***``. ``[^/\s]*@`` is greedy up to the LAST
# ``@`` before the path/whitespace, so a password with a raw (non-encoded) ``@``
# — e.g. ``rtsp://admin:p@ss@host/x`` — is still fully scrubbed (→ ``://***@host/x``)
# rather than leaking the fragment after the first ``@``.
_CRED_RE = re.compile(r"://[^/\s]*@")


def scrub_credentials(s: str | None) -> str:
    """Replace ``scheme://user:pass@`` with ``scheme://***@``. None/empty safe."""
    if not s:
        return ""
    return _CRED_RE.sub("://***@", s)
