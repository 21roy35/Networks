"""Tiny shared runtime state (toggled from Telegram, read by the loops)."""
from __future__ import annotations

import dataclasses
import time


@dataclasses.dataclass
class RunState:
    paused: bool = False
    started_at: float = dataclasses.field(default_factory=time.time)
    sweeps_done: int = 0
    candidates_seen: int = 0
