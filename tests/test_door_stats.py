"""Door travel statistics and fast polling while the door moves."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.maveo.api.iot import DOOR_OPEN, DOOR_OPENING
from custom_components.maveo.const import DEVICE_POLL_INTERVAL, DOMAIN

from .fakes import DEVICE_ID, FakeStick


def _state(hass: HomeAssistant, platform: str, key: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{DEVICE_ID}_{key}"
    )
    assert entity_id is not None, f"no {platform} entity {key}"
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def _tick(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _door(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call(
        "cover",
        service,
        {"entity_id": _state(hass, "cover", "cover").entity_id},
        blocking=True,
    )


async def test_open_and_close_from_home_assistant_are_timed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: ConfigEntry
) -> None:
    await _door(hass, "open_cover")
    assert _state(hass, "cover", "cover").state == "opening"
    await _tick(hass, freezer, 2)
    await _tick(hass, freezer, 2)
    assert _state(hass, "cover", "cover").state == "open"

    await _door(hass, "close_cover")
    await _tick(hass, freezer, 2)
    await _tick(hass, freezer, 3)
    assert _state(hass, "cover", "cover").state == "closed"

    assert float(_state(hass, "sensor", "open_duration").state) == 4.0
    assert float(_state(hass, "sensor", "close_duration").state) == 5.0
    assert _state(hass, "sensor", "open_count").state == "1"


async def test_door_moved_by_remote_is_timed_from_first_sighting(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: ConfigEntry,
    stick: FakeStick,
) -> None:
    stick.move(DOOR_OPENING, DOOR_OPEN)
    await _tick(hass, freezer, DEVICE_POLL_INTERVAL)
    assert _state(hass, "cover", "cover").state == "opening"
    await _tick(hass, freezer, 2)
    await _tick(hass, freezer, 2)

    assert _state(hass, "cover", "cover").state == "open"
    assert float(_state(hass, "sensor", "open_duration").state) == 4.0
    assert _state(hass, "sensor", "open_count").state == "1"


async def test_polling_slows_down_once_the_door_rests(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: ConfigEntry,
    stick: FakeStick,
) -> None:
    await _door(hass, "open_cover")
    await _tick(hass, freezer, 2)
    await _tick(hass, freezer, 2)
    reads = stick.status_reads()

    for _ in range(DEVICE_POLL_INTERVAL // 2 - 1):
        await _tick(hass, freezer, 2)
    assert stick.status_reads() == reads
    await _tick(hass, freezer, 2)
    assert stick.status_reads() == reads + 1


async def test_ignored_command_stops_fast_polling(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: ConfigEntry,
    stick: FakeStick,
) -> None:
    stick.travel_reads = 0
    stick.replies["AtoS_s"] = {"StoA_s": DOOR_OPEN}
    stick.motion = [DOOR_OPEN]
    await _tick(hass, freezer, DEVICE_POLL_INTERVAL)
    opens = _state(hass, "sensor", "open_count").state
    await _door(hass, "open_cover")
    for _ in range(15):
        await _tick(hass, freezer, 2)
    reads = stick.status_reads()

    for _ in range(DEVICE_POLL_INTERVAL // 2):
        await _tick(hass, freezer, 2)
    assert stick.status_reads() == reads + 1
    assert _state(hass, "sensor", "open_count").state == opens


async def test_statistics_survive_a_reload(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: ConfigEntry
) -> None:
    await _door(hass, "open_cover")
    await _tick(hass, freezer, 2)
    await _tick(hass, freezer, 2)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert float(_state(hass, "sensor", "open_duration").state) == 4.0
    assert _state(hass, "sensor", "open_count").state == "1"
    assert _state(hass, "sensor", "close_duration").state == "unknown"


async def test_failing_stick_stops_fast_polling(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: ConfigEntry,
    stick: FakeStick,
) -> None:
    await _door(hass, "open_cover")
    stick.connack_code = 5
    for _ in range(15):
        await _tick(hass, freezer, 2)
    attempts = len(stick.connect_headers)

    for _ in range(DEVICE_POLL_INTERVAL // 2):
        await _tick(hass, freezer, 2)
    assert len(stick.connect_headers) == attempts + 1
