import sys
import logging
from pythonjsonlogger import jsonlogger

formatter = jsonlogger.JsonFormatter(
    '%(asctime)s %(name)s %(levelname)s %(message)s'
)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(formatter)

logger = logging.getLogger("observer-agent")
logger.addHandler(handler)
logger.setLevel(logging.INFO)
