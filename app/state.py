import queue
import threading
from dataclasses import dataclass
from typing import Optional

@dataclass
class AppState:
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = threading.Event()
    user_input_q: "queue.Queue[Optional[str]]" = queue.Queue()
    manual_next_q: "queue.Queue[Optional[str]]" = queue.Queue()
    is_running: bool = False

    def start(self, target, args) -> None:
        self.stop()
        if self.thread and self.thread.is_alive():
            try:
                self.thread.join(timeout=2.0)
            except Exception:
                pass
        
        self.stop_event.clear()
        self.user_input_q = queue.Queue()
        self.manual_next_q = queue.Queue()
        self.is_running = True
        self.thread = threading.Thread(target=target, args=args, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.is_running = False
        self.stop_event.set()
        for q in (self.user_input_q, self.manual_next_q):
            try:
                q.put_nowait(None)
            except Exception:
                pass

state = AppState()
