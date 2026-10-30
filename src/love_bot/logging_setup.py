import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # Library exceptions/SQL can contain input, tokens or URLs: do not serialize them.
            "event": record.getMessage() if record.name.startswith("love_bot") else "library_event",
        }
        error_type = getattr(record, "error_type", None)
        if error_type:
            data["error_type"] = error_type
        if record.exc_info:
            data["error_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False)


def configure_logging(level: str):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(handlers=[handler], level=level, force=True)
    for name in ("aiogram", "aiohttp", "sqlalchemy", "asyncpg"):
        logging.getLogger(name).setLevel(logging.WARNING)
