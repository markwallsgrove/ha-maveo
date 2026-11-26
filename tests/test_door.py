"""Door travel tracking: durations and completed openings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, strategies as st

from custom_components.maveo.api.iot import (
    DOOR_CLOSED,
    DOOR_CLOSING,
    DOOR_OPEN,
    DOOR_OPENING,
    DOOR_POSITION_NAMES,
    DOOR_STOPPED,
)
from custom_components.maveo.door import MAX_TRAVEL, Direction, DoorTracker, Travel

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_first_reading_is_not_a_travel() -> None:
    assert DoorTracker().update(DOOR_OPEN, T0) is None


def test_observed_opening_is_timed() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_CLOSED, _at(0))
    assert tracker.update(DOOR_OPENING, _at(30)) is None
    assert tracker.update(DOOR_OPENING, _at(32)) is None
    assert tracker.update(DOOR_OPEN, _at(44)) == Travel(Direction.OPEN, 14)


def test_command_time_starts_the_clock() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_OPEN, _at(0))
    tracker.command_sent(Direction.CLOSE, _at(10))
    assert tracker.update(DOOR_OPEN, _at(11)) is None
    assert tracker.update(DOOR_CLOSING, _at(13)) is None
    assert tracker.update(DOOR_CLOSED, _at(25)) == Travel(Direction.CLOSE, 15)


def test_missed_movement_counts_without_duration() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_CLOSED, _at(0))
    assert tracker.update(DOOR_OPEN, _at(30)) == Travel(Direction.OPEN, None)


def test_stopped_door_is_not_timed() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_CLOSED, _at(0))
    tracker.update(DOOR_OPENING, _at(2))
    tracker.update(DOOR_STOPPED, _at(4))
    assert tracker.update(DOOR_OPEN, _at(30)) == Travel(Direction.OPEN, None)


def test_stale_command_is_discarded() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_OPEN, _at(0))
    tracker.command_sent(Direction.OPEN, _at(1))
    tracker.update(DOOR_OPEN, _at(2))
    tracker.update(DOOR_CLOSED, _at(600))
    tracker.update(DOOR_CLOSED, _at(700))
    later = 700 + MAX_TRAVEL.total_seconds()
    assert tracker.update(DOOR_OPEN, _at(later)) == Travel(Direction.OPEN, None)


def test_unknown_reading_keeps_state() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_CLOSED, _at(0))
    tracker.update(DOOR_OPENING, _at(2))
    assert tracker.update(None, _at(4)) is None
    assert tracker.update(DOOR_OPEN, _at(6)) == Travel(Direction.OPEN, 4)


@given(
    st.lists(
        st.tuples(
            st.one_of(st.none(), st.sampled_from(sorted(DOOR_POSITION_NAMES))),
            st.floats(min_value=0, max_value=600),
        ),
        max_size=40,
    )
)
def test_travels_only_end_at_a_new_resting_position(
    readings: list[tuple[int | None, float]],
) -> None:
    tracker = DoorTracker()
    now = 0.0
    last: int | None = None
    for position, step in readings:
        now += step
        travel = tracker.update(position, _at(now))
        if travel is not None:
            end = DOOR_OPEN if travel.direction is Direction.OPEN else DOOR_CLOSED
            assert position == end
            assert last is not None
            assert last != end
            assert travel.seconds is None or (0 <= travel.seconds <= MAX_TRAVEL.total_seconds())
        if position is not None:
            last = position


def test_durations_are_whole_seconds() -> None:
    tracker = DoorTracker()
    tracker.update(DOOR_CLOSED, _at(0))
    tracker.command_sent(Direction.OPEN, _at(1))
    travel = tracker.update(DOOR_OPEN, _at(18.277691))
    assert travel == Travel(Direction.OPEN, 17)
    assert travel is not None
    assert isinstance(travel.seconds, int)
