"""Stable domain types shared by catalog commands and services."""

from dataclasses import dataclass
from enum import Enum


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    STALE = "stale"


class MediaKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    OTHER = "other"


@dataclass(frozen=True)
class ScanSummary:
    discovered: int
    completed: int
    failed: int

    @property
    def remaining(self) -> int:
        return max(0, self.discovered - self.completed - self.failed)
