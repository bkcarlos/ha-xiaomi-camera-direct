"""Xiaomi cameras owned and managed by Home Assistant."""

import logging
from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .runtime import AccountRequired, CameraRuntime, RuntimeUnavailable

LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CAMERA, Platform.SWITCH]


class CameraCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, runtime):
        super().__init__(
            hass, LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=30)
        )
        self.runtime = runtime

    async def _async_update_data(self):
        try:
            payload = await self.runtime.request("/cameras")
            return {camera["did"]: camera for camera in payload["cameras"]}
        except AccountRequired as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (RuntimeUnavailable, TimeoutError) as err:
            raise UpdateFailed(str(err)) from err


async def async_setup_entry(hass, entry):
    runtime = CameraRuntime(hass, async_get_clientsession(hass), dict(entry.options))
    coordinator = CameraCoordinator(hass, entry, runtime)
    try:
        await runtime.start()
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.unique_id.removesuffix("_lens") not in coordinator.data:
                registry.async_remove(entity.entity_id)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException as err:
        await runtime.stop()
        if isinstance(err, (RuntimeUnavailable, TimeoutError)):
            raise ConfigEntryNotReady(str(err)) from err
        raise

    async def stop(event):
        await runtime.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.runtime.stop()
        return True
    return False


async def _reload(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)
