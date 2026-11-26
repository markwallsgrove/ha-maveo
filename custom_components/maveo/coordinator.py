"""Coordinators for the Maveo account, device state and guest keys."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import Enum, auto
import logging
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
_REPLY_TIMEOUT = 3.0
# The most packets a BlueFi sends in reply to STATUS before StoA_s
_STATUS_PACKETS = 15

_DOOR_COMMANDS = {
    Direction.OPEN: Command.GARAGE_OPEN,
    Direction.CLOSE: Command.GARAGE_CLOSE,
    None: Command.GARAGE_STOP,
}

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


type _Parser = Callable[[Mapping[str, Any]], dict[str, Any]]


def _value(key: str, response_key: str) -> _Parser:
    """Parse a reply that carries one value under `response_key`."""

    def parse(reply: Mapping[str, Any]) -> dict[str, Any]:
        return {key: reply[response_key]} if response_key in reply else {}

    return parse


def _flag(key: str, response_key: str) -> _Parser:
    """Parse a reply that carries one on/off value under `response_key`."""

    def parse(reply: Mapping[str, Any]) -> dict[str, Any]:
        value = reply.get(response_key)
        return {} if value is None else {key: bool(value)}

    return parse


def _gps(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the stick's location; StoA_gps is 0 when it has one."""
    if reply.get("StoA_gps") != 0:
        return {}
    lat = reply.get("lat")
    return {"gps_lat": lat, "gps_lng": reply.get("lng"), "has_gps": lat is not None}


def _wifi(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the stick's WiFi connection."""
    return {
        "wifi_ssid": reply.get("ssid"),
        "wifi_ip": reply.get("ip"),
        "wifi_mac": reply.get("mac"),
        "wifi_rssi": reply.get("rssi"),
    }


def _ime(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Parse whether the open and close travel have been learned."""
    if "StoA_ime_learn" not in reply:
        return {}
    return {"ime_open": bool(reply.get("open")), "ime_close": bool(reply.get("close"))}


def _indoor_reading(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the indoor sensor's readings and details."""
    return {
        "ht_temperature": _hundredths(reply.get("temperature_val")),
        "ht_humidity": _hundredths(reply.get("humidity_val")),
        "ht_battery": reply.get("battery_val"),
        "ht_last_update": reply.get("last_update"),
        "ht_name": reply.get("name") or None,
        "ht_manufacturer": reply.get("manufacturer") or None,
        "ht_model": reply.get("model") or None,
        "ht_serial": reply.get("serial_num") or None,
        "ht_firmware_rev": reply.get("firmware_rev") or None,
        "ht_software_rev": reply.get("software_rev") or None,
        "ht_hardware_rev": reply.get("hardware_rev") or None,
    }


def _weather(reply: Mapping[str, Any]) -> dict[str, Any]:
    """Parse the outdoor weather at the stick's location."""
    if reply.get("StoA_weather") != 0:
        return {}
    return {
        "weather_temperature": _hundredths(reply.get("temperature")),
        "weather_humidity": _hundredths(reply.get("humidity")),
    }


_READ_COMMANDS: list[tuple[Mapping[str, int], _Parser]] = [
    (Command.VERSION, _value("firmware", "StoA_v")),
    (Command.LIGHT_READ, _flag("light_on", "StoA_l_r")),
    (Command.SERIAL, _value("serial", "StoA_serial")),
    (Command.NAME_READ, _value("device_name", "StoA_name_r")),
    (Command.TTC_READ, _value("ttc_minutes", "StoA_ttc_r")),
    (Command.BUZZER_READ, _flag("buzzer_on", "StoA_buzzer_r")),
    (Command.GPS_READ, _gps),
    (Command.WIFI_READ, _wifi),
    (Command.IME_LEARN_READ, _ime),
]


async def _ask(iot: MaveoIoTClient, command: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Send a command and return the JSON reply; None when none arrives."""
    await iot.send(command)
    return _json(await iot.receive())


def _json(packet: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The JSON body of a packet; None for a missing or non-JSON packet."""
    if packet is None:
        return None
    body: Mapping[str, Any] | None = packet.get("json")
    return body


async def _read_status(iot: MaveoIoTClient) -> dict[str, Any]:
    """Read the door position and whether the stick is a BlueFi.

    A BlueFi stick sends a full state dump before StoA_s, which arrives last;
    a Connect stick sends only StoA_s.
    """
    await iot.send(Command.STATUS)
    is_bluefi = False
    for _ in range(_STATUS_PACKETS):
        reply = _json(await iot.receive())
        if reply is None:
            break
        if "StoA_s" in reply:
            return {"door_position": reply["StoA_s"], "is_bluefi": is_bluefi}
        is_bluefi = True
    return {"door_position": None, "is_bluefi": is_bluefi}


async def _read_indoor_sensor(iot: MaveoIoTClient) -> dict[str, Any]:
    """Read whether the indoor sensor is paired, and its readings when it is."""
    reply = await _ask(iot, Command.SENSOR_PRESENCE)
    if reply is None:
        return {}
    paired = reply.get("StoA_sensor") == 0 and reply.get("command") == 0 and reply.get("error") == 0
    if not paired:
        return {"ht_sensor_paired": False}
    # SENSOR_FULL sends the readings, then {"state": 9}
    await iot.send(Command.SENSOR_FULL)
    for _ in range(2):
        reading = _json(await iot.receive())
        if reading is not None and "temperature_val" in reading:
            return {"ht_sensor_paired": True, **_indoor_reading(reading)}
    return {"ht_sensor_paired": True}


class Reachability(Enum):
    """Whether a device answered its last poll; changes are logged once."""

    UNKNOWN = auto()
    ONLINE = auto()
    OFFLINE = auto()


class MaveoAccountCoordinator(DataUpdateCoordinator[dict[str, Device]]):
    """Devices on the Maveo account, polled so new and removed sticks are seen."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, session: MaveoSession) -> None:
        """Poll the account's sticks for `entry`."""
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
        """Poll the guest keys of one stick."""
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
        """Poll the state of one stick."""
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
            raise translated(UpdateFailed, "device_unreachable", err, device_id=self.device_id) from err

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
        device = registry.async_get_device_by_identifier((DOMAIN, self.device_id), self.config_entry.entry_id)
        if device is None:
            return
        sw_version = data.get("firmware") or device.sw_version
        serial_number = data.get("serial") or device.serial_number
        if (sw_version, serial_number) != (device.sw_version, device.serial_number):
            registry.async_update_device(device.id, sw_version=sw_version, serial_number=serial_number)

    async def _fetch_mqtt_state(self, *, position_only: bool) -> dict[str, Any]:
        async with MaveoIoTClient(
            self._session.http,
            self._session.auth,
            self._session.config,
            self.device_id,
            reply_timeout=_REPLY_TIMEOUT,
        ) as iot:
            await iot.subscribe()
            updates = await _read_status(iot)
            if position_only or updates["door_position"] is None:
                return updates
            for command, parse in _READ_COMMANDS:
                if (reply := await _ask(iot, command)) is not None:
                    updates |= parse(reply)
            updates |= await _read_indoor_sensor(iot)
            # Outdoor weather is only known for a stick with a location
            if updates.get("has_gps"):
                weather = {"AtoS_weather": 0, "lat": updates["gps_lat"], "lng": updates["gps_lng"]}
                if (reply := await _ask(iot, weather)) is not None:
                    updates |= _weather(reply)
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
                reply_timeout=1.0,
            ) as iot:
                await iot.subscribe()
                await iot.send(command)
                await iot.receive()  # drain any immediate response
        except (CloudError, aiohttp.ClientError, TimeoutError, ConnectionError) as err:
            raise translated(HomeAssistantError, "command_failed", err, device_id=self.device_id) from err
