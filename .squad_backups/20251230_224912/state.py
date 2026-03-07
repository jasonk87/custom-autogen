import queue
import threading
from dataclasses import dataclass, field
from typing import Optional, List, Set, Any

@dataclass
class AppState:
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    user_input_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    manual_next_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    is_running: bool = False
    current_session_name: str = "default"
    
    # Persistence & Broadcasting
    history: List[str] = field(default_factory=list)
    queues: Set["queue.Queue[str]"] = field(default_factory=set)
    
    # Synchronized Setup Config
    config: dict = field(default_factory=lambda: {
        "goal": "",
        "model": "qwen2.5:14b", 
        "managers": "RoundRobin",
        "agents": [],
        "turns": 60,
        "turns": 60,
        "temperature": 0.3,
        "current_tab": "tab-setup"
    })
    
    # Real-time Drafts
    drafts: dict = field(default_factory=dict)

    def start(self, target, args) -> None:
        self.stop_event.clear()
        self.user_input_q = queue.Queue()
        self.manual_next_q = queue.Queue()
        self.history.clear() 
        self.is_running = True
        self.thread = threading.Thread(target=target, args=args, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.is_running:
            self.stop_event.set()
            # Unblock any waiting input queues
            try:
                self.user_input_q.put(None)
                self.manual_next_q.put(None)
            except Exception:
                pass
            if self.thread:
                self.thread.join(timeout=2.0)
            self.is_running = False
            self.thread = None
            
            # Notify all listeners of stop
            self.publish_message('[DONE]')

    def publish_message(self, message: str) -> None:
        """Store in history and broadcast to all listed queues."""
        self.history.append(message)
        # Iterate over a copy since queues might be removed concurrently
        for q in list(self.queues):
            try:
                q.put_nowait(message)
            except Exception:
                pass

state = AppState()
