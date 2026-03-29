"""
Custom Agent Studio v11 - Refactored
====================================
This is the main entry point for running the Custom Agent Studio.
The application logic is split into modules in the `app/` directory.

Run:
  pip install -r requirements.txt
  python main.py

Open:
  http://127.0.0.1:5000
"""

import asyncio
import signal

from hypercorn.asyncio import serve
from hypercorn.config import Config

# Import order matters: app.config must be initialized before other modules.
import app.config  # noqa: F401
from app import log
from app.server import app
from app.state import state


def _shutdown(*_args):
    """Gracefully shut down the application."""
    log.info("shutdown_signal_received")
    try:
        state.stop()
    finally:
        for handler in list(log.handlers):
            try:
                handler.flush()
            except Exception:
                pass


if __name__ == "__main__":
    log.info("starting_server", extra={"extra": {"host": "0.0.0.0", "port": 5000}})
    print("Starting Custom Agent Studio - v11 (Refactored) - http://127.0.0.1:5000")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    config = Config()
    config.bind = ["0.0.0.0:5000"]
    config.keep_alive_timeout = 120
    asyncio.run(serve(app, config))
