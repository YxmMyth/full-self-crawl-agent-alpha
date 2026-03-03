"""
Management: State management, checkpoints, and progress tracking.

Merged from old project's state_manager.py, checkpoint.py, progress_tracker.py.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("management.state")


@dataclass
class CrawlState:
    """Current state of a crawl task."""
    task_id: str
    status: str = "pending"  # pending, running, completed, failed
    current_url: str = ""
    pages_visited: list[str] = field(default_factory=list)
    data_collected: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    started_at: str = ""
    updated_at: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "current_url": self.current_url,
            "pages_visited": self.pages_visited,
            "data_collected_count": len(self.data_collected),
            "error_count": len(self.errors),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }


class StateManager:
    """Manage crawl task state with checkpoint support.

    Provides:
    - In-memory state tracking
    - Checkpoint save/load for crash recovery
    - Progress events
    """

    def __init__(self, checkpoint_dir: str = "./states"):
        self._states: dict[str, CrawlState] = {}
        self._checkpoint_dir = Path(checkpoint_dir)
        self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._events: list[dict] = []

    def create(self, task_id: str, url: str = "") -> CrawlState:
        """Create a new task state."""
        state = CrawlState(
            task_id=task_id,
            current_url=url,
            started_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        self._states[task_id] = state
        return state

    def get(self, task_id: str) -> CrawlState | None:
        return self._states.get(task_id)

    def update(self, task_id: str, **kwargs) -> CrawlState | None:
        """Update state fields."""
        state = self._states.get(task_id)
        if not state:
            return None
        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
        state.updated_at = datetime.now().isoformat()
        return state

    def add_data(self, task_id: str, records: list[dict]) -> None:
        """Append extracted data to state."""
        state = self._states.get(task_id)
        if state:
            state.data_collected.extend(records)
            state.updated_at = datetime.now().isoformat()

    def add_error(self, task_id: str, error: str) -> None:
        state = self._states.get(task_id)
        if state:
            state.errors.append(error)

    def record_page_visit(self, task_id: str, url: str) -> None:
        state = self._states.get(task_id)
        if state and url not in state.pages_visited:
            state.pages_visited.append(url)

    # --- Checkpoints ---

    def save_checkpoint(self, task_id: str) -> str:
        """Save state to disk for crash recovery."""
        state = self._states.get(task_id)
        if not state:
            raise ValueError(f"No state for task {task_id}")

        path = self._checkpoint_dir / f"{task_id}.json"
        data = {
            "state": state.to_dict(),
            "data": state.data_collected,
            "errors": state.errors,
            "pages": state.pages_visited,
            "checkpoint_time": datetime.now().isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.debug(f"Checkpoint saved: {path}")
        return str(path)

    def load_checkpoint(self, task_id: str) -> CrawlState | None:
        """Load state from checkpoint file."""
        path = self._checkpoint_dir / f"{task_id}.json"
        if not path.exists():
            return None

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        state_data = data.get("state", {})
        state = CrawlState(
            task_id=task_id,
            status=state_data.get("status", "pending"),
            current_url=state_data.get("current_url", ""),
            pages_visited=data.get("pages", []),
            data_collected=data.get("data", []),
            errors=data.get("errors", []),
            started_at=state_data.get("started_at", ""),
            updated_at=state_data.get("updated_at", ""),
        )
        self._states[task_id] = state
        logger.info(f"Loaded checkpoint for task {task_id}")
        return state

    # --- Events ---

    def record_event(self, task_id: str, event_type: str, **details) -> None:
        """Record a progress event."""
        self._events.append({
            "task_id": task_id,
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            **details,
        })

    def get_events(self, task_id: str) -> list[dict]:
        return [e for e in self._events if e["task_id"] == task_id]
