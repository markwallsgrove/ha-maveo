"""Door travel tracking from polled door positions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .api.iot import DOOR_CLOSED, DOOR_CLOSING, DOOR_OPEN, DOOR_OPENING

# A travel that takes longer than this was not observed from its start.
MAX_TRAVEL = timedelta(minutes=2)


class Direction(StrEnum):
    OPEN = "open"
    CLOSE = "close"


_MOVING = {DOOR_OPENING: Direction.OPEN, DOOR_CLOSING: Direction.CLOSE}
_END = {DOOR_OPEN: Direction.OPEN, DOOR_CLOSED: Direction.CLOSE}


@dataclass(frozen=True)
class Travel:
    """A door travel that ended; `seconds` is None when its start was missed."""

    direction: Direction
    seconds: float | None


class DoorTracker:
    """Turn door position readings into completed travels.

    The door is either resting or moving in one direction since a start time.
    A travel starts when a command is sent or movement is first seen, and ends
    when the door reaches the end position for that direction.
    """

    def __init__(self) -> None:
        self._position: int | None = None
        self._moving: tuple[Direction, datetime] | None = None

    def command_sent(self, direction: Direction, now: datetime) -> None:
        self._moving = (direction, now)

    def update(self, position: int | None, now: datetime) -> Travel | None:
        if position is None:
            return None
        previous, self._position = self._position, position
        if self._moving is not None and now - self._moving[1] > MAX_TRAVEL:
            self._moving = None

        if (direction := _MOVING.get(position)) is not None:
            if self._moving is None or self._moving[0] is not direction:
                self._moving = (direction, now)
            return None

        end = _END.get(position)
        if end is None:
            self._moving = None
            return None
        if previous is None or previous == position:
            return None
        seconds = None
        if self._moving is not None and self._moving[0] is end:
            seconds = (now - self._moving[1]).total_seconds()
        self._moving = None
        return Travel(end, seconds)
