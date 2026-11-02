"""Account login and camera selection entirely inside Home Assistant."""

import asyncio
from urllib.parse import parse_qs, urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_CAMERAS, CONF_QUALITY, DOMAIN
from .runtime import CameraRuntime, RuntimeUnavailable


def camera_schema(cameras, defaults=None):
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_CAMERAS, default=defaults.get(CONF_CAMERAS, [])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[{"value": d["did"], "label": d["name"]} for d in cameras],
                    multiple=True,
                )
            ),
            vol.Required(
                CONF_QUALITY, default=defaults.get(CONF_QUALITY, "low")
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["low", "high"],
                    translation_key="quality",
                )
            ),
        }
    )


class XiaomiCameraFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self.runtime = None
        self.cameras = []
        self.auth_url = None
        self.expiry = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return XiaomiOptionsFlow()

    @callback
    def async_remove(self):
        if self.expiry:
            self.expiry.cancel()
        if self.runtime:
            self.hass.async_create_task(self.runtime.stop())

    async def _start(self):
        if self.runtime is None or self.runtime.closed:
            self.runtime = CameraRuntime(self.hass, async_get_clientsession(self.hass))
            # Also clean up abandoned forms that the frontend never explicitly aborts.
            self.expiry = asyncio.get_running_loop().call_later(600, self.async_remove)
        await self.runtime.start()

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("xiaomi_camera_local")
        if self.context.get("source") != "reauth":
            self._abort_if_unique_id_configured()
        try:
            await self._start()
            health = await self.runtime.request("/health")
            if health["linked"]:
                return await self.async_step_cameras()
            return await self.async_step_authorize()
        except (RuntimeUnavailable, TimeoutError):
            return self.async_abort(reason="runtime_unavailable")

    async def async_step_reauth(self, entry_data):
        return await self.async_step_authorize()

    async def async_step_authorize(self, user_input=None):
        errors = {}
        try:
            await self._start()
            if user_input:
                query = parse_qs(urlparse(user_input["redirect_url"].strip()).query)
                if not query.get("code") or not query.get("state"):
                    errors["base"] = "invalid_redirect"
                else:
                    await self.runtime.request(
                        "/link/complete",
                        method="POST",
                        data={
                            "code": query["code"][0],
                            "state": query["state"][0],
                        },
                    )
                    if self.context.get("source") == "reauth":
                        await self.runtime.stop()
                        return self.async_update_reload_and_abort(self._get_reauth_entry())
                    return await self.async_step_cameras()
            if self.auth_url is None:
                self.auth_url = (await self.runtime.request("/link/begin", method="POST"))["url"]
        except (RuntimeUnavailable, TimeoutError):
            errors["base"] = "cannot_connect"
            self.auth_url = None
        return self.async_show_form(
            step_id="authorize",
            errors=errors,
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            description_placeholders={"authorize_url": self.auth_url or ""},
        )

    async def async_step_cameras(self, user_input=None):
        if not self.cameras:
            self.cameras = (await self.runtime.request("/cameras"))["cameras"]
        if not self.cameras:
            return self.async_abort(reason="no_cameras")
        errors = {}
        if user_input:
            valid = {d["did"] for d in self.cameras}
            chosen = user_input.get(CONF_CAMERAS, [])
            if chosen and set(chosen) <= valid:
                await self.runtime.stop()
                return self.async_create_entry(title="Xiaomi Camera", data={}, options=user_input)
            errors["base"] = "choose_camera"
        return self.async_show_form(
            step_id="cameras", data_schema=camera_schema(self.cameras), errors=errors
        )


class XiaomiOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None):
        # Query inventory through the existing worker; never create a second
        # account session just to render the options form.
        runtime = self.config_entry.runtime_data.runtime
        try:
            cameras = (await runtime.request("/cameras?all=1"))["cameras"]
        except (RuntimeUnavailable, TimeoutError):
            return self.async_abort(reason="runtime_unavailable")
        errors = {}
        if user_input:
            valid = {d["did"] for d in cameras}
            chosen = user_input.get(CONF_CAMERAS, [])
            if chosen and set(chosen) <= valid:
                return self.async_create_entry(data=user_input)
            errors["base"] = "choose_camera"
        return self.async_show_form(
            step_id="init",
            data_schema=camera_schema(cameras, self.config_entry.options),
            errors=errors,
        )
