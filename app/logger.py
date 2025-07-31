"""
Structured JSON logger for observer-agent.
"""

import logging
import sys

from pythonjsonlogger import jsonlogger

_formatter = jsonlogger.JsonFormatter(
    "%(asctime)s %(levelname)s %(name)s %(message)s"
)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_formatter)

logger = logging.getLogger("observer-agent")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)
