"""The Anova integration."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from anova_wifi import (
    AnovaApi,
    APCWifiDevice,
    InvalidLogin,
    NoDevicesFound,
    WebsocketFailure,
)
from homeassistant.const import CONF_DEVICES, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import AnovaCoordinator, AnovaData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from .coordinator import AnovaConfigEntry

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

_LOGGER = logging.getLogger(__name__)

# Service constants
SERVICE_START_COOK = "start_cook"
SERVICE_STOP_COOK = "stop_cook"
SERVICE_SET_TEMPERATURE = "set_temperature"
SERVICE_SET_TIMER = "set_timer"

ATTR_DEVICE_ID = "device_id"
ATTR_TARGET_TEMPERATURE = "target_temperature"
ATTR_TIMER_MINUTES = "timer_minutes"

SERVICE_START_COOK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_TARGET_TEMPERATURE): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=95)
        ),
        vol.Optional(ATTR_TIMER_MINUTES, default=0): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=6000)
        ),
    }
)

SERVICE_STOP_COOK_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
    }
)

SERVICE_SET_TEMPERATURE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_TARGET_TEMPERATURE): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=95)
        ),
    }
)

SERVICE_SET_TIMER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_TIMER_MINUTES): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=6000)
        ),
    }
)


def _get_cooker_id_from_device_id(hass: HomeAssistant, device_id: str) -> str | None:
    """Get the Anova cooker_id from a HA device_id."""
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    if device is None:
        return None
    # The identifier is (DOMAIN, cooker_id)
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN:
            return identifier[1]
    return None


def _get_api_for_cooker(hass: HomeAssistant, cooker_id: str) -> AnovaApi | None:
    """Find the API instance that has this cooker."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            api = entry.runtime_data.api
            if api.websocket_handler and cooker_id in api.websocket_handler.devices:
                return api
    return None


async def async_setup_entry(hass: HomeAssistant, entry: AnovaConfigEntry) -> bool:
    """Set up Anova from a config entry."""
    api = AnovaApi(
        aiohttp_client.async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    try:
        await api.authenticate()
    except InvalidLogin:
        _LOGGER.exception(
            "Login was incorrect — please log back in through the config flow."
        )
        return False
    if not api.jwt:
        raise ConfigEntryNotReady("Authentication succeeded but no JWT was returned")
    try:
        await api.create_websocket()
    except NoDevicesFound as err:
        # Can later setup successfully and spawn a repair.
        raise ConfigEntryNotReady(
            "No devices were found on the websocket, perhaps you don't have any devices on this account?"
        ) from err
    except WebsocketFailure as err:
        raise ConfigEntryNotReady("Failed connecting to the websocket.") from err
    # Create a coordinator per device, if the device is offline, no data will be on the
    # websocket, and the coordinator should auto mark as unavailable. But as long as
    # the websocket successfully connected, config entry should setup.
    devices: list[APCWifiDevice] = []
    if TYPE_CHECKING:
        # api.websocket_handler can't be None after successfully creating the
        # websocket client
        assert api.websocket_handler is not None
    devices = list(api.websocket_handler.devices.values())
    _LOGGER.info("Found %d devices: %s", len(devices), [d.cooker_id for d in devices])
    coordinators = [AnovaCoordinator(hass, entry, device) for device in devices]
    _LOGGER.info("Created %d coordinators", len(coordinators))
    entry.runtime_data = AnovaData(api_jwt=api.jwt, coordinators=coordinators, api=api)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services (only once for the domain)
    if not hass.services.has_service(DOMAIN, SERVICE_START_COOK):
        _async_register_services(hass)

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register Anova services."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_COOK,
        functools.partial(_handle_start_cook, hass),
        schema=SERVICE_START_COOK_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_COOK,
        functools.partial(_handle_stop_cook, hass),
        schema=SERVICE_STOP_COOK_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TEMPERATURE,
        functools.partial(_handle_set_temperature, hass),
        schema=SERVICE_SET_TEMPERATURE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TIMER,
        functools.partial(_handle_set_timer, hass),
        schema=SERVICE_SET_TIMER_SCHEMA,
    )


async def _handle_start_cook(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle start_cook service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    target_temp = call.data[ATTR_TARGET_TEMPERATURE]
    timer_minutes = call.data.get(ATTR_TIMER_MINUTES, 0)
    timer_seconds = timer_minutes * 60

    cooker_id = _get_cooker_id_from_device_id(hass, device_id)
    if cooker_id is None:
        _LOGGER.error("Could not find Anova device for device_id: %s", device_id)
        return

    api = _get_api_for_cooker(hass, cooker_id)
    if api is None or api.websocket_handler is None:
        _LOGGER.error("Could not find API for cooker: %s", cooker_id)
        return

    success = await api.websocket_handler.start_cook(
        cooker_id=cooker_id,
        target_temperature=target_temp,
        timer_seconds=timer_seconds,
    )
    if success:
        _LOGGER.info(
            "Started cooking on %s: %.1f°C, timer: %d min",
            cooker_id,
            target_temp,
            timer_minutes,
        )
    else:
        _LOGGER.error("Failed to start cooking on %s", cooker_id)


async def _handle_stop_cook(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle stop_cook service call."""
    device_id = call.data[ATTR_DEVICE_ID]

    cooker_id = _get_cooker_id_from_device_id(hass, device_id)
    if cooker_id is None:
        _LOGGER.error("Could not find Anova device for device_id: %s", device_id)
        return

    api = _get_api_for_cooker(hass, cooker_id)
    if api is None or api.websocket_handler is None:
        _LOGGER.error("Could not find API for cooker: %s", cooker_id)
        return

    success = await api.websocket_handler.stop_cook(cooker_id=cooker_id)
    if success:
        _LOGGER.info("Stopped cooking on %s", cooker_id)
    else:
        _LOGGER.error("Failed to stop cooking on %s", cooker_id)


async def _handle_set_temperature(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle set_temperature service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    target_temp = call.data[ATTR_TARGET_TEMPERATURE]

    cooker_id = _get_cooker_id_from_device_id(hass, device_id)
    if cooker_id is None:
        _LOGGER.error("Could not find Anova device for device_id: %s", device_id)
        return

    api = _get_api_for_cooker(hass, cooker_id)
    if api is None or api.websocket_handler is None:
        _LOGGER.error("Could not find API for cooker: %s", cooker_id)
        return

    success = await api.websocket_handler.set_target_temperature(
        cooker_id=cooker_id, target_temperature=target_temp
    )
    if success:
        _LOGGER.info("Set temperature on %s to %.1f°C", cooker_id, target_temp)
    else:
        _LOGGER.error("Failed to set temperature on %s", cooker_id)


async def _handle_set_timer(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle set_timer service call."""
    device_id = call.data[ATTR_DEVICE_ID]
    timer_minutes = call.data[ATTR_TIMER_MINUTES]
    timer_seconds = timer_minutes * 60

    cooker_id = _get_cooker_id_from_device_id(hass, device_id)
    if cooker_id is None:
        _LOGGER.error("Could not find Anova device for device_id: %s", device_id)
        return

    api = _get_api_for_cooker(hass, cooker_id)
    if api is None or api.websocket_handler is None:
        _LOGGER.error("Could not find API for cooker: %s", cooker_id)
        return

    success = await api.websocket_handler.set_timer(
        cooker_id=cooker_id, timer_seconds=timer_seconds
    )
    if success:
        _LOGGER.info("Set timer on %s to %d minutes", cooker_id, timer_minutes)
    else:
        _LOGGER.error("Failed to set timer on %s", cooker_id)


async def async_unload_entry(hass: HomeAssistant, entry: AnovaConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Disconnect from WS
        await entry.runtime_data.api.disconnect_websocket()
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: AnovaConfigEntry) -> bool:
    """Migrate entry."""
    _LOGGER.debug("Migrating from version %s:%s", entry.version, entry.minor_version)

    if entry.version > 1:
        # This means the user has downgraded from a future version
        return False

    if entry.version == 1 and entry.minor_version == 1:
        new_data = {**entry.data}
        if CONF_DEVICES in new_data:
            new_data.pop(CONF_DEVICES)

        hass.config_entries.async_update_entry(entry, data=new_data, minor_version=2)

    _LOGGER.debug(
        "Migration to version %s:%s successful", entry.version, entry.minor_version
    )

    return True
