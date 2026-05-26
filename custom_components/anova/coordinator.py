"""Support for Anova Coordinators."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from anova_wifi import AnovaApi, APCUpdate, APCWifiDevice
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

REFRESH_INTERVAL = timedelta(seconds=30)

_LOGGER = logging.getLogger(__name__)


@dataclass
class AnovaData:
    """Data for the Anova integration."""

    api_jwt: str
    coordinators: list[AnovaCoordinator]
    api: AnovaApi


type AnovaConfigEntry = ConfigEntry[AnovaData]


def _dig(d: dict, path: list[str], default: Any = None) -> Any:
    """Safe nested getter."""
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _enrich_sensor_from_raw(sensor_obj: Any, raw: dict) -> None:
    """Attach raw WS fields to APCUpdate.sensor in-place (idempotent)."""
    # Mode (raw, 1:1 from API)
    if getattr(sensor_obj, "mode_raw", None) is None:
        sensor_obj.mode_raw = _dig(raw, ["payload", "state", "state", "mode"])

    # Low-water
    loww = _dig(raw, ["payload", "state", "nodes", "lowWater"], {}) or {}
    if getattr(sensor_obj, "low_water_warning", None) is None:
        sensor_obj.low_water_warning = loww.get("warning")
    if getattr(sensor_obj, "low_water_empty", None) is None:
        sensor_obj.low_water_empty = loww.get("empty")

    # Diagnostics
    sysi = _dig(raw, ["payload", "state", "systemInfo"], {}) or {}
    if getattr(sensor_obj, "firmware_version", None) is None:
        sensor_obj.firmware_version = sysi.get("firmwareVersion")
    if getattr(sensor_obj, "hardware_version", None) is None:
        sensor_obj.hardware_version = sysi.get("hardwareVersion")
    if getattr(sensor_obj, "online", None) is None:
        sensor_obj.online = sysi.get("online")


class AnovaCoordinator(DataUpdateCoordinator[APCUpdate]):
    """Anova custom coordinator."""

    config_entry: AnovaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AnovaConfigEntry,
        anova_device: APCWifiDevice,
    ) -> None:
        """Set up Anova Coordinator."""
        super().__init__(
            hass,
            config_entry=config_entry,
            name="Anova Precision Cooker",
            logger=_LOGGER,
        )
        self.device_unique_id = anova_device.cooker_id
        self.anova_device = anova_device
        self.anova_device.set_update_listener(self._handle_update)
        _LOGGER.info(
            "Set update_listener on device %s: %s",
            anova_device.cooker_id,
            self._handle_update,
        )

        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.device_unique_id)},
            name="Anova Precision Cooker",
            manufacturer="Anova",
            model="Precision Cooker",
        )
        self.sensor_data_set: bool = False

        # Periodic refresh so duration sensors (e.g. cook_time_remaining) can
        # recompute against "now" between WS messages.
        config_entry.async_on_unload(
            async_track_time_interval(
                hass, self._async_periodic_refresh, REFRESH_INTERVAL
            )
        )

    @callback
    def _async_periodic_refresh(self, _now: datetime) -> None:
        """Notify listeners so time-dependent sensors recompute."""
        if self.data is not None:
            self.async_update_listeners()

    def _handle_update(self, update: APCUpdate) -> None:
        """Receive device update, enrich sensor with raw payload, propagate.

        anova_wifi invokes this synchronously from inside its async websocket
        handler — i.e. already on the HA event loop — so we can call the
        sync @callback async_set_updated_data directly.
        """
        try:
            # Roh-Payload bestmöglich beschaffen
            raw: dict | None = None

            # 1) Einige anova_wifi-Versionen hängen den letzten WS-Frame ans Device
            for attr in (
                "last_raw_message",
                "last_message",
                "last_state",
                "raw_state",
                "raw",
            ):
                raw = raw or getattr(self.anova_device, attr, None)

            # 2) Manche hängen Rohdaten direkt ans Update
            for attr in ("raw_message", "raw", "payload", "message"):
                cand = getattr(update, attr, None)
                if isinstance(cand, dict):
                    raw = cand
                    break

            # 3) Fallback: bekannte Struktur unter update.state_dict (wenn vorhanden)
            cand = getattr(update, "state_dict", None)
            if isinstance(cand, dict) and "payload" in cand:
                raw = cand

            if isinstance(raw, dict) and getattr(update, "sensor", None) is not None:
                # Attach the raw dict directly so sensor.py can access it via _get(d, ["raw", ...])
                update.sensor.raw = raw  # type: ignore[attr-defined]
                _enrich_sensor_from_raw(update.sensor, raw)
            else:
                _LOGGER.debug("Anova: no raw payload available for enrichment (ok).")

        except Exception as exc:  # noqa: BLE001
            # Defensive: enrichment must never break the update flow.
            _LOGGER.debug("Anova enrich failed: %r", exc)

        self.async_set_updated_data(update)
