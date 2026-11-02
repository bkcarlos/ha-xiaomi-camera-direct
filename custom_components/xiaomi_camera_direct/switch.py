"""Lens power in Home Assistant."""

from homeassistant.components.switch import SwitchEntity

from .entity import XiaomiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(LensSwitch(entry.runtime_data, did) for did in entry.runtime_data.data)


class LensSwitch(XiaomiEntity, SwitchEntity):
    _attr_translation_key = "lens"

    def __init__(self, coordinator, did):
        super().__init__(coordinator, did)
        self._attr_unique_id = did + "_lens"

    @property
    def is_on(self):
        return self.camera_data.get("powered_on") is True

    async def _set(self, value):
        await self.coordinator.runtime.request(
            f"/power/{self.did}", method="POST", data={"value": value}
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)
