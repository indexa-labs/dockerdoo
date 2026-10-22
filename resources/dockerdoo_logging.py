"""Write Odoo's log to stdout, one JSON object per record, for hosts that read structured logs such as Railway.

dockerdoo_logging.json wires this in; set ODOO_LOG_CONFIG to its path to use it. See "Logging" in the README.
"""

import json
import logging
import sys
from datetime import datetime, timezone

# Railway reads debug, info, warn, and error; the other Python level names are those in lower case.
LEVELS = {"WARNING": "warn", "CRITICAL": "error"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": LEVELS.get(record.levelname, record.levelname.lower()),
            "logger": record.name,
            "dbname": getattr(record, "dbname", None),
            # Formatter.format appends the traceback to the message, which keeps a record on one line.
            "message": super().format(record),
        })


def stdout_handler():
    """Odoo returns from init_logger right after it loads a log configuration, which skips the logger levels that
    ODOO_LOG_LEVEL and ODOO_LOG_HANDLER set, and the INFO default. Set them here, as init_logger would."""
    from odoo import tools
    from odoo.netsvc import DEFAULT_LOG_CONFIGURATION, PSEUDOCONFIG_MAPPER

    for item in (*DEFAULT_LOG_CONFIGURATION, *PSEUDOCONFIG_MAPPER.get(tools.config["log_level"], ()), *tools.config["log_handler"]):
        name, level = item.strip().split(":")
        logging.getLogger(name).setLevel(getattr(logging, level, logging.INFO))
    return logging.StreamHandler(sys.stdout)
