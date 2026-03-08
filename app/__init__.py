import json
import logging
import sys
import time

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "t": int(time.time() * 1000),
            "level": record.levelname,
            "msg": record.getMessage(),
            "name": record.name,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        
        # When using log.info(..., extra={"foo": "bar"}), 
        # the key "foo" is added directly to the record.__dict__
        # We exclude standard logging attributes to find the "extra" ones.
        standard_attrs = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process"
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs:
                payload[key] = value
                
        return json.dumps(payload, ensure_ascii=False)

log = logging.getLogger("agent_studio")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JsonFormatter())
log.addHandler(_handler)

_file_handler = logging.FileHandler("debug.log", encoding="utf-8")
_file_handler.setFormatter(JsonFormatter())
log.addHandler(_file_handler)

# Third-party AutoGen internals can emit noisy cancellation tracebacks when a run is stopped.
# Keep those logs quiet; surfaced app-level events are logged through `agent_studio`.
logging.getLogger("autogen_core").setLevel(logging.CRITICAL)
logging.getLogger("autogen_agentchat").setLevel(logging.CRITICAL)
