"""Maveo sensor entities — device sensors and one sensor per guest key."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import time
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import GuestUser
from .coordinator import MaveoGuestCoordinator
from .data import MaveoConfigEntry, MaveoDevice
from .door import Direction, Travel
from .entity import MaveoEntity, maveo_device_info

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MaveoSensorDescription(SensorEntityDescription):
    """A stick sensor and where it reads its value."""

    data_key: str
    availability_key: str | None = None  # coordinator data key that must be truthy


DEVICE_SENSORS: tuple[MaveoSensorDescription, ...] = (
    MaveoSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        data_key="firmware",
    ),
    MaveoSensorDescription(
        key="ttc",
        translation_key="ttc",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="ttc_minutes",
    ),
    MaveoSensorDescription(
        key="rssi",
        translation_key="rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        data_key="wifi_rssi",
    ),
    MaveoSensorDescription(
        key="buzzer",
        translation_key="buzzer",
        device_class=SensorDeviceClass.ENUM,
        options=["on", "off"],
        entity_category=EntityCategory.DIAGNOSTIC,
        data_key="buzzer_on",
    ),
    MaveoSensorDescription(
        key="stick_type",
        translation_key="stick_type",
        device_class=SensorDeviceClass.ENUM,
        options=["bluefi", "connect"],
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        data_key="is_bluefi",
    ),
    # Outdoor weather (requires GPS to be configured on the stick)
    MaveoSensorDescription(
        key="weather_temperature",
        translation_key="weather_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="weather_temperature",
        availability_key="has_gps",
    ),
    MaveoSensorDescription(
        key="weather_humidity",
        translation_key="weather_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="weather_humidity",
        availability_key="has_gps",
    ),
    # H+T sensor status entity (always visible, metadata as attributes)
    MaveoSensorDescription(
        key="ht_sensor",
        translation_key="ht_sensor",
        device_class=SensorDeviceClass.ENUM,
        options=["paired", "not_paired"],
        entity_category=EntityCategory.DIAGNOSTIC,
        data_key="ht_sensor_paired",
    ),
    # Indoor H+T sensor readings (requires sensor to be paired)
    MaveoSensorDescription(
        key="indoor_temperature",
        translation_key="indoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="ht_temperature",
        availability_key="ht_sensor_paired",
    ),
    MaveoSensorDescription(
        key="indoor_humidity",
        translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        data_key="ht_humidity",
        availability_key="ht_sensor_paired",
    ),
    MaveoSensorDescription(
        key="indoor_battery",
        translation_key="indoor_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        data_key="ht_battery",
        availability_key="ht_sensor_paired",
    ),
)

type StatValue = int | float | None


def _duration(direction: Direction) -> Callable[[StatValue, Travel], StatValue]:
    def apply(value: StatValue, travel: Travel) -> StatValue:
        if travel.direction is direction and travel.seconds is not None:
            return travel.seconds
        return value

    return apply


def _count_opens(value: StatValue, travel: Travel) -> StatValue:
    return (value or 0) + 1 if travel.direction is Direction.OPEN else value


@dataclass(frozen=True, kw_only=True)
class MaveoDoorStatDescription(SensorEntityDescription):
    """A door travel statistic and how a travel changes it."""

    apply: Callable[[StatValue, Travel], StatValue]
    initial: StatValue = None


DOOR_STATS: tuple[MaveoDoorStatDescription, ...] = (
    MaveoDoorStatDescription(
        key="open_duration",
        translation_key="open_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        apply=_duration(Direction.OPEN),
    ),
    MaveoDoorStatDescription(
        key="close_duration",
        translation_key="close_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        apply=_duration(Direction.CLOSE),
    ),
    MaveoDoorStatDescription(
        key="open_count",
        translation_key="open_count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        apply=_count_opens,
        initial=0,
    ),
)

# Coordinator keys exposed as attributes of the H+T sensor status entity.
_HT_ATTRIBUTES = (
    ("ht_temperature", "temperature"),
    ("ht_humidity", "humidity"),
    ("ht_battery", "battery"),
    ("ht_last_update", "last_update"),
    ("ht_name", "name"),
    ("ht_manufacturer", "manufacturer"),
    ("ht_model", "model"),
    ("ht_serial", "serial"),
    ("ht_firmware_rev", "firmware_rev"),
    ("ht_software_rev", "software_rev"),
    ("ht_hardware_rev", "hardware_rev"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaveoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors for each stick and its guest keys."""

    @callback
    def _add(device: MaveoDevice) -> None:
        async_add_entities(MaveoDeviceSensor(device, desc) for desc in DEVICE_SENSORS)
        async_add_entities(MaveoDoorStatSensor(device, desc) for desc in DOOR_STATS)
        _track_guests(hass, entry, device, async_add_entities)

    entry.async_on_unload(entry.runtime_data.async_subscribe_devices(_add))


@callback
def _track_guests(
    hass: HomeAssistant,
    entry: MaveoConfigEntry,
    device: MaveoDevice,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a sensor per guest key and remove those whose key is gone.

    Removal checks the entity registry, so keys revoked while Home Assistant
    was stopped are cleaned up too.
    """
    tracked: set[str] = set()
    coordinator = device.guests
    prefix = _guest_unique_id(device.info.id, "")

    @callback
    def _on_update() -> None:
        if not coordinator.last_update_success:
            return
        current = {g.user_id: g for g in coordinator.data or []}
        new = current.keys() - tracked
        if new:
            async_add_entities(MaveoGuestSensor(device, coordinator, current[user_id]) for user_id in new)
            tracked.update(new)
        registry = er.async_get(hass)
        for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
            if not reg_entry.unique_id.startswith(prefix):
                continue
            user_id = reg_entry.unique_id.removeprefix(prefix)
            if user_id not in current:
                registry.async_remove(reg_entry.entity_id)
                tracked.discard(user_id)

    device.async_on_remove(coordinator.async_add_listener(_on_update))
    _on_update()


def _guest_unique_id(device_id: str, user_id: str) -> str:
    return f"{device_id}_guest_{user_id}"


class MaveoDeviceSensor(MaveoEntity, SensorEntity):
    """A single device sensor."""

    entity_description: MaveoSensorDescription

    def __init__(self, device: MaveoDevice, description: MaveoSensorDescription) -> None:
        """Attach the sensor to the stick's device."""
        super().__init__(device, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Available while the stick answers and has this reading."""
        av_key = self.entity_description.availability_key
        return super().available and (av_key is None or bool(self.data.get(av_key)))

    @property
    def native_value(self) -> Any:
        """The reading, with on/off values as translated states."""
        key = self.entity_description.key
        value = self.data.get(self.entity_description.data_key)
        if value is None:
            return None
        if key == "buzzer":
            return "on" if value else "off"
        if key == "stick_type":
            return "bluefi" if value else "connect"
        if key == "ht_sensor":
            return "paired" if value else "not_paired"
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra details for the WiFi signal and indoor sensor."""
        key = self.entity_description.key
        if key == "rssi":
            return {
                attr: self.data[src]
                for src, attr in (
                    ("wifi_ssid", "ssid"),
                    ("wifi_ip", "ip"),
                    ("wifi_mac", "mac"),
                )
                if self.data.get(src) is not None
            }
        if key == "ht_sensor":
            return {attr: self.data[src] for src, attr in _HT_ATTRIBUTES if self.data.get(src) is not None}
        return None


class MaveoDoorStatSensor(MaveoEntity, RestoreSensor):
    """A door travel statistic, kept across restarts."""

    entity_description: MaveoDoorStatDescription

    def __init__(self, device: MaveoDevice, description: MaveoDoorStatDescription) -> None:
        """Attach the statistic to the stick's device."""
        super().__init__(device, description.key)
        self.entity_description = description
        self._value: StatValue = description.initial

    async def async_added_to_hass(self) -> None:
        """Restore the value from before the restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and isinstance(last.native_value, int | float):
            self._value = last.native_value

    @property
    def native_value(self) -> StatValue:
        """The statistic."""
        return self._value

    @callback
    def _handle_coordinator_update(self) -> None:
        travel = self.data.get("door_travel")
        if isinstance(travel, Travel):
            self._value = self.entity_description.apply(self._value, travel)
        super()._handle_coordinator_update()


class MaveoGuestSensor(CoordinatorEntity[MaveoGuestCoordinator], SensorEntity):
    """Remaining validity of one guest key. The token is never exposed."""

    _attr_has_entity_name = True
    _attr_translation_key = "guest"

    def __init__(self, device: MaveoDevice, coordinator: MaveoGuestCoordinator, guest: GuestUser) -> None:
        """Attach the guest key to the stick's device."""
        super().__init__(coordinator)
        self.user_id = guest.user_id
        self._guest = guest
        self._attr_unique_id = _guest_unique_id(device.info.id, guest.user_id)
        self._attr_translation_placeholders = {"guest": guest.user_id[:8]}
        self._attr_device_info = maveo_device_info(device)

    @property
    def available(self) -> bool:
        """Available while the key exists."""
        return super().available and any(g.user_id == self.user_id for g in self.coordinator.data or [])

    @callback
    def _handle_coordinator_update(self) -> None:
        """Keep the key's latest details."""
        self._guest = next((g for g in self.coordinator.data or [] if g.user_id == self.user_id), self._guest)
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> str:
        """Time left, or `expired`."""
        guest = self._guest
        if not guest.ttl.isdigit():
            return "expired"
        remaining = int(guest.ttl) - int(time.time())
        if remaining <= 0:
            return "expired"
        hours, rem = divmod(remaining, 3600)
        minutes = rem // 60
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The key's user ID, access level and whether it was used."""
        guest = self._guest
        attrs: dict[str, Any] = {
            "user_id": guest.user_id,
            "rights": "admin" if guest.rights == "1" else "restricted",
            "claimed": guest.is_claimed,
        }
        if guest.nametag1:
            attrs["app_name"] = guest.nametag1
        if guest.nametag2:
            attrs["os"] = guest.nametag2
        if guest.nametag3:
            attrs["locale"] = guest.nametag3
        if guest.ttl.isdigit():
            attrs["expires_at"] = datetime.fromtimestamp(int(guest.ttl), tz=UTC).isoformat()
        return attrs
