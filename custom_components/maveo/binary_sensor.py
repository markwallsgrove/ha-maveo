"""Maveo connectivity binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .data import MaveoConfigEntry, MaveoDevice
from .entity import MaveoEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MaveoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a connectivity sensor for each stick."""

    @callback
    def _add(device: MaveoDevice) -> None:
        async_add_entities([MaveoConnectivity(device)])

    entry.async_on_unload(entry.runtime_data.async_subscribe_devices(_add))


class MaveoConnectivity(MaveoEntity, BinarySensorEntity):
    """Whether the stick answers over the cloud; off while it does not respond."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "connectivity"

    def __init__(self, device: MaveoDevice) -> None:
        """Attach the sensor to the stick's device."""
        super().__init__(device, "connectivity")

    @property
    def available(self) -> bool:
        """Available while polls succeed, so it can report the stick offline."""
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        """Whether the stick answered the last poll."""
        return bool(self.data.get("online"))
