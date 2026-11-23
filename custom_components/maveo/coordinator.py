"""Coordinators for the Maveo account, device state and guest keys."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    APIError,
    CloudError,
    Command,
    Device,
    GuestUser,
    MaveoIoTClient,
)
from .api.iot import DOOR_CLOSING, DOOR_OPENING
from .const import (
    ACCOUNT_POLL_INTERVAL,
    DEVICE_POLL_INTERVAL,
    DOMAIN,
    FAST_POLL_INTERVAL,
    FAST_POLL_WINDOW,
    GUEST_POLL_INTERVAL,
    MISSED_POLLS_BEFORE_UNAVAILABLE,
)
from .door import Direction, DoorTracker
from .errors import translated
from .session import MaveoSession

_LOGGER = logging.getLogger(__name__)

_POLL = timedelta(seconds=DEVICE_POLL_INTERVAL)
_FAST_POLL = timedelta(seconds=FAST_POLL_INTERVAL)

_DOOR_COMMANDS = {
    Direction.OPEN: Command.GARAGE_OPEN,
    Direction.CLOSE: Command.GARAGE_CLOSE,
    None: Command.GARAGE_STOP,
}

_READ_COMMANDS = [
    ("firmware", Command.VERSION, "StoA_v"),
    ("light_on", Command.LIGHT_READ, "StoA_l_r"),
    ("serial", Command.SERIAL, "StoA_serial"),
    ("device_name", Command.NAME_READ, "StoA_name_r"),
    ("ttc_minutes", Command.TTC_READ, "StoA_ttc_r"),
    ("buzzer_on", Command.BUZZER_READ, "StoA_buzzer_r"),
    ("gps", Command.GPS_READ, None),
    ("wifi", Command.WIFI_READ, None),
    ("ime", Command.IME_LEARN_READ, None),
]

_EMPTY: dict[str, Any] = {
    "online": False,
    "is_bluefi": None,
    "door_position": None,
    "door_travel": None,
    "light_on": None,
    "firmware": None,
    "serial": None,
    "device_name": None,
    "ttc_minutes": None,
    "buzzer_on": None,
    "gps_lat": None,
    "gps_lng": None,
    "has_gps": False,
    "wifi_ssid": None,
    "wifi_ip": None,
    "wifi_mac": None,
    "wifi_rssi": None,
    "ime_open": None,
    "ime_close": None,
    "ht_sensor_paired": None,
    "ht_temperature": None,
    "ht_humidity": None,
    "ht_battery": None,
    "ht_name": None,
    "ht_manufacturer": None,
    "ht_model": None,
    "ht_serial": None,
    "ht_firmware_rev": None,
    "ht_software_rev": None,
    "ht_hardware_rev": None,
    "ht_last_update": None,
    "weather_temperature": None,
    "weather_humidity": None,
}


def _hundredths(value: object) -> float | None:
    """Convert a reading in 0.01 units; None when the stick sent no number."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value / 100.0
    return None


class Reachability(Enum):
    """Whether a device answered its last poll; changes are logged once."""

    UNKNOWN = auto()
    ONLINE = auto()
    OFFLINE = auto()


class MaveoAccountCoordinator(DataUpdateCoordinator[dict[str, Device]]):
    """Devices on the Maveo account, polled so new and removed sticks are seen."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, session: MaveoSession
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Maveo account",
            update_interval=timedelta(seconds=ACCOUNT_POLL_INTERVAL),
        )
        self._session = session

    async def _async_update_data(self) -> dict[str, Device]:
        await self._session.async_ensure_valid_for_update()
        try:
            devices = await self._session.client.list_devices()
        except APIError as err:
            raise translated(UpdateFailed, "cloud_unreachable", err) from err
        return {d.id: d for d in devices}


class MaveoGuestCoordinator(DataUpdateCoordinator[list[GuestUser]]):
    """Guest keys of a single Maveo device — REST poll."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: MaveoSession,
        device_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Maveo guests {device_id}",
            update_interval=timedelta(seconds=GUEST_POLL_INTERVAL),
        )
        self._session = session
        self.device_id = device_id

    async def _async_update_data(self) -> list[GuestUser]:
        await self._session.async_ensure_valid_for_update()
        try:
            return await self._session.client.list_guest_users(self.device_id)
        except APIError as err:
            raise translated(UpdateFailed, "cloud_unreachable", err) from err


class MaveoDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """State of a single Maveo device — MQTT burst-poll.

    A failed or unanswered poll keeps the last state until
    MISSED_POLLS_BEFORE_UNAVAILABLE regular polls in a row have failed. Then a
    cloud or MQTT failure fails the update, so the coordinator logs it once and
    marks entities unavailable. A device that does not answer is reported as
    offline instead, and that change is logged once each way.

    While the door moves, or for a short window after a door command, only the
    door position is polled, every few seconds, so travels can be timed.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        session: MaveoSession,
        device_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Maveo device {device_id}",
            update_interval=_POLL,
        )
        self._session = session
        self.device_id = device_id
        self._reachability = Reachability.UNKNOWN
        self.door = DoorTracker()
        self._fast_until: datetime | None = None
        self._missed_polls = 0

    async def _async_update_data(self) -> dict[str, Any]:
        await self._session.async_ensure_valid_for_update()

        now = dt_util.utcnow()
        fast = self.data is not None and self.update_interval == _FAST_POLL
        result = dict(self.data) if fast else dict(_EMPTY)
        # The cloud "status" endpoint reports the last AWS IoT lifecycle event
        # (e.g. SERVER_INITIATED_DISCONNECT) and can claim the device is
        # disconnected while it still answers over MQTT. So we don't gate on it;
        # we probe the device directly and treat a real MQTT response as online.
        try:
            result.update(await self._fetch_mqtt_state(position_only=fast))
        except (CloudError, aiohttp.ClientError, TimeoutError, ConnectionError) as err:
            self._set_poll_interval(now, moving=False)
            if (last := self._last_state(fast=fast)) is not None:
                return last
            raise translated(
                UpdateFailed, "device_unreachable", err, device_id=self.device_id
            ) from err

        position = result.get("door_position")
        if position is None and (last := self._last_state(fast=fast)) is not None:
            self._set_poll_interval(now, moving=False)
            return last
        if position is not None:
            self._missed_polls = 0
        result["door_travel"] = self.door.update(position, now)
        moving = position in (DOOR_OPENING, DOOR_CLOSING)
        if moving or result["door_travel"] is not None:
            self._fast_until = None
        self._set_poll_interval(now, moving=moving)

        online = position is not None
        result["online"] = online
        reachability = Reachability.ONLINE if online else Reachability.OFFLINE
        if reachability is not self._reachability:
            if online:
                _LOGGER.info("Maveo device %s is online", self.device_id)
            else:
                _LOGGER.warning("Maveo device %s does not respond", self.device_id)
            self._reachability = reachability
        self._update_device_registry(result)
        return result

    def _last_state(self, *, fast: bool) -> dict[str, Any] | None:
        """Count a missed poll; the last state while it may still be kept."""
        if not fast:
            self._missed_polls += 1
        if self.data is None or not self.data["online"]:
            return None
        if self._missed_polls >= MISSED_POLLS_BEFORE_UNAVAILABLE:
            return None
        return {**self.data, "door_travel": None}

    def _set_poll_interval(self, now: datetime, *, moving: bool) -> None:
        in_window = self._fast_until is not None and now < self._fast_until
        self.update_interval = _FAST_POLL if moving or in_window else _POLL

    def _update_device_registry(self, data: Mapping[str, Any]) -> None:
        """Keep firmware and serial in the device registry current."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_device_by_identifier(
            (DOMAIN, self.device_id), self.config_entry.entry_id
        )
        if device is None:
            return
        sw_version = data.get("firmware") or device.sw_version
        serial_number = data.get("serial") or device.serial_number
        if (sw_version, serial_number) != (device.sw_version, device.serial_number):
            registry.async_update_device(
                device.id, sw_version=sw_version, serial_number=serial_number
            )

    async def _fetch_mqtt_state(self, *, position_only: bool) -> dict[str, Any]:
        updates: dict[str, Any] = {"door_position": None}
        async with MaveoIoTClient(
            self._session.http, self._session.auth, self._session.config, self.device_id
        ) as iot:
            await iot.subscribe()
            await iot.send(Command.STATUS)
            # BlueFi sends a full state dump before StoA_s (which arrives last).
            # Connect stick sends only StoA_s.
            # Any packet before StoA_s means a BlueFi stick.
            is_bluefi = False
            for _ in range(15):
                pkt = await iot.receive(timeout=3.0)
                if not pkt or "json" not in pkt:
                    break
                d = pkt["json"]
                if "StoA_s" in d:
                    updates["door_position"] = d["StoA_s"]
                    break
                is_bluefi = True
            updates["is_bluefi"] = is_bluefi
            if position_only or updates["door_position"] is None:
                return updates

            for key, cmd, response_key in _READ_COMMANDS:
                await iot.send(cmd)
                pkt = await iot.receive(timeout=3.0)
                if not pkt or "json" not in pkt:
                    continue
                d = pkt["json"]

                if key == "gps":
                    if d.get("StoA_gps") == 0:
                        updates["gps_lat"] = d.get("lat")
                        updates["gps_lng"] = d.get("lng")
                        if updates["gps_lat"] is not None:
                            updates["has_gps"] = True
                elif key == "wifi":
                    updates["wifi_ssid"] = d.get("ssid")
                    updates["wifi_ip"] = d.get("ip")
                    updates["wifi_mac"] = d.get("mac")
                    updates["wifi_rssi"] = d.get("rssi")
                elif key == "ime":
                    if "StoA_ime_learn" in d:
                        updates["ime_open"] = bool(d.get("open"))
                        updates["ime_close"] = bool(d.get("close"))
                elif key == "light_on":
                    val = d.get(response_key)
                    if val is not None:
                        updates["light_on"] = bool(val)
                elif key == "buzzer_on":
                    val = d.get(response_key)
                    if val is not None:
                        updates["buzzer_on"] = bool(val)
                elif response_key and response_key in d:
                    updates[key] = d[response_key]

            # H+T sensor — detect presence first, then fetch readings if paired
            await iot.send(Command.SENSOR_PRESENCE)
            pkt = await iot.receive(timeout=3.0)
            if pkt and "json" in pkt:
                d = pkt["json"]
                paired = (
                    d.get("StoA_sensor") == 0
                    and d.get("command") == 0
                    and d.get("error") == 0
                )
                updates["ht_sensor_paired"] = paired
                if paired:
                    # SENSOR_FULL returns readings + metadata in one shot
                    await iot.send(Command.SENSOR_FULL)
                    # Two responses arrive: data packet then {"state":9} — stop at data
                    for _ in range(2):
                        pkt = await iot.receive(timeout=3.0)
                        if pkt and "json" in pkt:
                            d = pkt["json"]
                            if "temperature_val" in d:
                                updates["ht_temperature"] = _hundredths(
                                    d.get("temperature_val")
                                )
                                updates["ht_humidity"] = _hundredths(
                                    d.get("humidity_val")
                                )
                                updates["ht_battery"] = d.get("battery_val")
                                updates["ht_last_update"] = d.get("last_update")
                                updates["ht_name"] = d.get("name") or None
                                updates["ht_manufacturer"] = (
                                    d.get("manufacturer") or None
                                )
                                updates["ht_model"] = d.get("model") or None
                                updates["ht_serial"] = d.get("serial_num") or None
                                updates["ht_firmware_rev"] = (
                                    d.get("firmware_rev") or None
                                )
                                updates["ht_software_rev"] = (
                                    d.get("software_rev") or None
                                )
                                updates["ht_hardware_rev"] = (
                                    d.get("hardware_rev") or None
                                )
                                break

            # Outdoor weather — only meaningful with GPS coordinates
            if updates.get("has_gps"):
                weather_cmd = {
                    "AtoS_weather": 0,
                    "lat": updates["gps_lat"],
                    "lng": updates["gps_lng"],
                }
                await iot.send(weather_cmd)
                pkt = await iot.receive(timeout=3.0)
                if pkt and "json" in pkt:
                    d = pkt["json"]
                    if d.get("StoA_weather") == 0:
                        updates["weather_temperature"] = _hundredths(
                            d.get("temperature")
                        )
                        updates["weather_humidity"] = _hundredths(d.get("humidity"))

        return updates

    async def async_move_door(self, direction: Direction | None) -> None:
        """Open, close or (with None) stop the door, then poll it quickly."""
        await self._async_send(_DOOR_COMMANDS[direction])
        now = dt_util.utcnow()
        if direction is not None:
            self.door.command_sent(direction, now)
        self._fast_until = now + timedelta(seconds=FAST_POLL_WINDOW)
        self.update_interval = _FAST_POLL
        await self.async_request_refresh()

    async def async_send_command(self, command: Mapping[str, Any]) -> None:
        """Send an IoT command then schedule a coordinator refresh."""
        await self._async_send(command)
        await asyncio.sleep(2)
        await self.async_request_refresh()

    async def _async_send(self, command: Mapping[str, Any]) -> None:
        await self._session.async_ensure_valid_for_action()
        try:
            async with MaveoIoTClient(
                self._session.http,
                self._session.auth,
                self._session.config,
                self.device_id,
            ) as iot:
                await iot.subscribe()
                await iot.send(command)
                await iot.receive(timeout=1.0)  # drain any immediate response
        except (CloudError, aiohttp.ClientError, TimeoutError, ConnectionError) as err:
            raise translated(
                HomeAssistantError, "command_failed", err, device_id=self.device_id
            ) from err
