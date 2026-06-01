import queue
import threading
import uuid
from dataclasses import dataclass
from dataclasses import field
from typing import Optional

from app.config import WORKSPACE_DIR


@dataclass
class AppState:
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    user_input_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    coach_input_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    manual_next_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    is_running: bool = False
    active_workspace: str = WORKSPACE_DIR
    current_run_id: Optional[str] = None

    def start(self, target, args) -> None:
        self.stop()
        if self.thread and self.thread.is_alive():
            try:
                self.thread.join(timeout=2.0)
            except Exception:
                pass
        if self.thread and self.thread.is_alive():
            raise RuntimeError("The previous agent run is still stopping. Try again shortly.")
        
        self.stop_event.clear()
        self.user_input_q = queue.Queue()
        self.coach_input_q = queue.Queue()
        self.manual_next_q = queue.Queue()
        self.is_running = True
        self.current_run_id = str(uuid.uuid4())
        self.thread = threading.Thread(target=target, args=args, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.is_running = False
        self.stop_event.set()
        for q in (self.user_input_q, self.coach_input_q, self.manual_next_q):
            try:
                q.put_nowait(None)
            except Exception:
                pass

state = AppState()
