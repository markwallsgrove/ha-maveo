"""Devices joining or leaving the account, and stale guest keys."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.maveo.const import (
    ACCOUNT_POLL_INTERVAL,
    DEVICE_POLL_INTERVAL,
    DOMAIN,
    GUEST_POLL_INTERVAL,
)

from .fakes import DEVICE_ID, FakeMaveoApi, FakeStick

SECOND_ID = "2002"


async def _advance(hass: HomeAssistant, seconds: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


def _device(
    hass: HomeAssistant, entry: ConfigEntry, device_id: str
) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, device_id), entry.entry_id
    )


async def test_new_stick_is_added_without_reload(
    hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi
) -> None:
    cloud.devices.append({"id": SECOND_ID, "name": "Shed", "devicetype": 1})
    await _advance(hass, ACCOUNT_POLL_INTERVAL + 1)

    device = _device(hass, entry, SECOND_ID)
    assert device is not None
    assert device.name == "Shed"
    entity_id = er.async_get(hass).async_get_entity_id(
        "cover", DOMAIN, f"{SECOND_ID}_cover"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "closed"


async def test_stick_missing_from_one_poll_is_kept(
    hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi
) -> None:
    saved = list(cloud.devices)
    cloud.devices.clear()
    await _advance(hass, ACCOUNT_POLL_INTERVAL + 1)
    assert _device(hass, entry, DEVICE_ID) is not None

    cloud.devices.extend(saved)
    await _advance(hass, 2 * ACCOUNT_POLL_INTERVAL + 1)
    cloud.devices.clear()
    await _advance(hass, 3 * ACCOUNT_POLL_INTERVAL + 1)
    assert _device(hass, entry, DEVICE_ID) is not None


async def test_removed_stick_is_removed(
    hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi
) -> None:
    cloud.devices.clear()
    await _advance(hass, ACCOUNT_POLL_INTERVAL + 1)
    await _advance(hass, 2 * ACCOUNT_POLL_INTERVAL + 1)

    assert _device(hass, entry, DEVICE_ID) is None
    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


async def test_removed_stick_is_no_longer_polled(
    hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi
) -> None:
    cloud.guests.append(
        {"userid": "u-1", "token": "t", "rights": "0", "ttl": "4102444800"}
    )
    await _advance(hass, GUEST_POLL_INTERVAL + 1)
    guest_entity = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{DEVICE_ID}_guest_u-1"
    )
    assert guest_entity is not None

    cloud.devices.clear()
    await _advance(hass, ACCOUNT_POLL_INTERVAL + 1)
    await _advance(hass, 2 * ACCOUNT_POLL_INTERVAL + 1)
    cloud.requests.clear()

    await _advance(hass, 2 * ACCOUNT_POLL_INTERVAL + GUEST_POLL_INTERVAL + 1)
    assert "list_user" not in cloud.commands()
    assert hass.states.get(guest_entity) is None


async def _remove_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: ConfigEntry,
    device_id: str,
) -> bool:
    """Remove a device from the entry the way the device page does."""
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": device_id,
        }
    )
    response = await client.receive_json()
    return bool(response["success"])


async def test_device_on_account_cannot_be_removed(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cloud: FakeMaveoApi,
    hass_ws_client: WebSocketGenerator,
) -> None:
    device = _device(hass, entry, DEVICE_ID)
    assert device is not None
    assert not await _remove_device(hass, hass_ws_client, entry, device.id)


async def test_device_gone_from_account_can_be_removed(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cloud: FakeMaveoApi,
    hass_ws_client: WebSocketGenerator,
) -> None:
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "9999")}
    )
    assert await _remove_device(hass, hass_ws_client, entry, stale.id)


async def test_revoked_guest_sensor_is_removed(
    hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi
) -> None:
    cloud.guests.append(
        {"userid": "u-1", "token": "t", "rights": "0", "ttl": "4102444800"}
    )
    await _advance(hass, GUEST_POLL_INTERVAL + 1)
    registry = er.async_get(hass)
    unique_id = f"{DEVICE_ID}_guest_u-1"
    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is not None

    cloud.guests.clear()
    await _advance(hass, 2 * GUEST_POLL_INTERVAL + 1)
    assert registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None


async def test_guest_revoked_while_unloaded_is_removed(
    hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi
) -> None:
    cloud.guests.append(
        {"userid": "u-1", "token": "t", "rights": "0", "ttl": "4102444800"}
    )
    await _advance(hass, GUEST_POLL_INTERVAL + 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    cloud.guests.clear()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    unique_id = f"{DEVICE_ID}_guest_u-1"
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, unique_id) is None


async def test_firmware_update_reaches_device_registry(
    hass: HomeAssistant, entry: ConfigEntry, stick: FakeStick
) -> None:
    stick.replies["AtoS_v"] = {"StoA_v": "1.3.0"}
    await _advance(hass, DEVICE_POLL_INTERVAL + 1)
    device = _device(hass, entry, DEVICE_ID)
    assert device is not None
    assert device.sw_version == "1.3.0"
    assert device.serial_number == "SN0001"
