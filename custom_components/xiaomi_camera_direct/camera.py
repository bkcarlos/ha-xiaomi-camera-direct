"""Direct native video into Home Assistant's stream component."""

from homeassistant.components.camera import Camera, CameraEntityFeature

from .entity import XiaomiEntity
from .runtime import RuntimeUnavailable


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(XiaomiCamera(entry.runtime_data, did) for did in entry.runtime_data.data)


class XiaomiCamera(XiaomiEntity, Camera):
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_name = None

    def __init__(self, coordinator, did):
        XiaomiEntity.__init__(self, coordinator, did)
        Camera.__init__(self)
        self._attr_unique_id = did

    @property
    def is_on(self):
        return self.camera_data.get("powered_on") is not False

    async def stream_source(self):
        await self.coordinator.runtime.start()
        return self.coordinator.runtime.url(f"/stream/{self.did}", stream=True)

    async def async_camera_image(self, width=None, height=None):
        if not self.is_on:
            return None
        try:
            return await self.coordinator.runtime.request(f"/snapshot/{self.did}", binary=True)
        except RuntimeUnavailable:
            return None
