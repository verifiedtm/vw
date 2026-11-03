"""Opt-in pipeline diagnostics, kept off stdout for JSON CLI callers."""

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
