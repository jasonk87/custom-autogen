import json
import queue
import threading
import uuid
from dataclasses import dataclass
from dataclasses import field
from typing import Any, Callable, List, Optional, Tuple

from app.config import WORKSPACE_DIR


class ReplayEventBuffer:
    """Thread-safe event history that lets reconnecting clients replay missed SSE messages."""

    def __init__(self) -> None:
        self._items: List[str] = []
        self._condition = threading.Condition()

    def put(self, item: str) -> None:
        with self._condition:
            self._items.append(item)
            self._condition.notify_all()

    def read_after(self, last_event_id: int, timeout: float = 2.0) -> List[Tuple[int, str]]:
        with self._condition:
            if len(self._items) <= last_event_id:
                self._condition.wait(timeout)
            return [
                (index, item)
                for index, item in enumerate(self._items[last_event_id:], start=last_event_id + 1)
            ]

    def copy_without_done(self) -> "ReplayEventBuffer":
        copy = ReplayEventBuffer()
        with self._condition:
            for item in self._items:
                if item != "[DONE]":
                    copy.put(item)
        return copy

    def chat_transcript(self, max_chars: int = 16000) -> str:
        lines = []
        with self._condition:
            items = list(self._items)
        for item in items:
            if item == "[DONE]":
                continue
            try:
                payload = json.loads(item)
            except Exception:
                continue
            if payload.get("type") != "chat":
                continue
            sender = str(payload.get("sender") or "Unknown")
            message = str(payload.get("message") or "").strip()
            if message:
                lines.append(f"{sender}: {message}")
        return "\n\n".join(lines)[-max_chars:]


@dataclass
class AppState:
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    user_input_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    coach_input_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    manual_next_q: "queue.Queue[Optional[str]]" = field(default_factory=queue.Queue)
    is_running: bool = False
    active_workspace: str = WORKSPACE_DIR
    active_workspace_session: Optional[str] = None
    active_workspace_persistent: bool = False
    current_run_id: Optional[str] = None
    current_client_run_token: Optional[str] = None
    current_output_buffer: Optional[ReplayEventBuffer] = None
    current_target: Optional[Callable[..., Any]] = None
    current_args: Optional[Tuple[Any, ...]] = None
    stopped_by_user: bool = False

    def start(
        self,
        target,
        args,
        client_run_token: Optional[str] = None,
        output_buffer: Optional[ReplayEventBuffer] = None,
    ) -> None:
        if self.is_running and self.thread and self.thread.is_alive():
            raise RuntimeError("An agent run is already active. Stop it before starting another.")
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
        self.current_client_run_token = client_run_token
        self.current_output_buffer = output_buffer
        self.current_target = target
        self.current_args = args
        self.stopped_by_user = False
        self.thread = threading.Thread(target=target, args=args, daemon=True)
        self.thread.start()

    def reconnect_buffer(self, client_run_token: Optional[str]) -> Optional[ReplayEventBuffer]:
        if client_run_token and client_run_token == self.current_client_run_token:
            return self.current_output_buffer
        return None

    def finish(self) -> None:
        if self.thread is not threading.current_thread():
            return
        self.is_running = False

    def resumable(self, client_run_token: Optional[str]) -> bool:
        return bool(
            client_run_token
            and client_run_token == self.current_client_run_token
            and self.current_output_buffer is not None
            and self.current_target is not None
            and self.current_args is not None
            and self.stopped_by_user
            and not self.is_running
        )

    def prepare_resume(
        self,
        client_run_token: Optional[str],
    ) -> Tuple[Callable[..., Any], Tuple[Any, ...], ReplayEventBuffer, str]:
        if not self.resumable(client_run_token):
            raise RuntimeError("No stopped simulation is available to resume.")
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        if self.thread and self.thread.is_alive():
            raise RuntimeError("The previous simulation is still stopping. Try again shortly.")

        assert self.current_target is not None
        assert self.current_args is not None
        assert self.current_output_buffer is not None
        output_buffer = self.current_output_buffer.copy_without_done()
        return (
            self.current_target,
            self.current_args,
            output_buffer,
            self.current_output_buffer.chat_transcript(),
        )

    def stop(self) -> None:
        self.is_running = False
        self.stopped_by_user = True
        self.stop_event.set()
        for q in (self.user_input_q, self.coach_input_q, self.manual_next_q):
            try:
                q.put_nowait(None)
            except Exception:
                pass

state = AppState()
