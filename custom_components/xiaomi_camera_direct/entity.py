"""Shared device identity."""

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class XiaomiEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, did):
        super().__init__(coordinator)
        self.did = did
        camera = coordinator.data[did]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, did)},
            name=camera["name"],
            manufacturer=camera.get("manufacturer", "Xiaomi"),
            model=camera["model"],
        )

    @property
    def camera_data(self):
        return self.coordinator.data.get(self.did, {})

    @property
    def available(self):
        return super().available and bool(self.camera_data.get("online"))
