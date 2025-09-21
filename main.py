"""
Custom Agent Studio v10 — Refactored
====================================
This is the main entry point for running the Custom Agent Studio.
The application logic is now split into multiple modules inside the `app/` directory.

Run:
  pip install flask httpx werkzeug
  python main.py

Open:
  http://127.0.0.1:8080
"""

import signal
import asyncio
from hypercorn.config import Config
from hypercorn.asyncio import serve

# Initialize application modules. The order of these imports matters.
# `app.config` must be first.
import app.config
from app import log
from app.server import app
from app.state import state


def _shutdown(*_args):
    """Gracefully shuts down the application."""
    log.info("shutdown_signal_received")
    # The try/finally block ensures that we attempt to flush logs even if state.stop() fails.
    try:
        state.stop()
    finally:
        for h in list(log.handlers):
            try:
                h.flush()
            except Exception:
                pass


if __name__ == "__main__":
    log.info("starting_server", extra={"extra": {"host": "0.0.0.0", "port": 8080}})
    print("Starting Custom Agent Studio — v10 (Refactored) — http://127.0.0.1:8080")

    # Register shutdown signals
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Create and run the server with hypercorn
    config = Config()
    config.bind = ["0.0.0.0:8080"]
    asyncio.run(serve(app, config))
