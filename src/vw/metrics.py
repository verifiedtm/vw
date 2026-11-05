"""Opt-in pipeline diagnostics, kept off stdout for JSON CLI callers.

VW_FRAME_DIAGNOSTICS emits JSON to stderr. Python callers may attach an INFO
handler to logging.getLogger("vw.performance"). Events describe frame-index
memory/disk hits, invalid cache entries, probes, reference hashing, verified seeks,
fallback reasons, normalization reuse and decode cost. ffmpeg decoded-frame counts
come from its debug summary; unavailable counts are null, never estimated.

frame_hash_timing reports selected stream, index/hash time bases, count and
range. Hash time base is null if ffmpeg omits it. Timestamp mismatch reasons
include expected/actual counts and the first differing decoded frame's index and
index-tick timestamps. Cached failures retain their original diagnostic text;
collecting metrics does not alter cache identity or verification policy.
"""

import json
import logging
import os
import sys

logger = logging.getLogger("vw.performance")


def enabled():
    return bool(os.environ.get("VW_FRAME_DIAGNOSTICS")) or logger.isEnabledFor(
        logging.INFO
    )


def emit(event, **values):
    record = {"event": event, **values}
    logger.info(json.dumps(record, sort_keys=True))
    if os.environ.get("VW_FRAME_DIAGNOSTICS"):
        print(json.dumps(record, sort_keys=True), file=sys.stderr)
