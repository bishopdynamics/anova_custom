"""Binary sensors for the Anova integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)

from .entity import AnovaDescriptionEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from anova_wifi import APCUpdateSensor
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .coordinator import AnovaConfigEntry


@dataclass(frozen=True, kw_only=True)
class AnovaBinaryEntityDescription(BinarySensorEntityDescription):
    """Describes an Anova binary sensor."""

    value_fn: Callable[[APCUpdateSensor], bool | None]


BINARY_DESCRIPTIONS: list[AnovaBinaryEntityDescription] = [
    AnovaBinaryEntityDescription(
        key="low_water_warning",
        translation_key="low_water_warning",
        value_fn=lambda d: getattr(d, "low_water_warning", None),
    ),
    AnovaBinaryEntityDescription(
        key="low_water_empty",
        translation_key="low_water_empty",
        value_fn=lambda d: getattr(d, "low_water_empty", None),
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AnovaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Anova binary sensors."""
    for coordinator in entry.runtime_data.coordinators:
        async_add_entities(
            AnovaLowWaterBinary(coordinator, desc) for desc in BINARY_DESCRIPTIONS
        )


class AnovaLowWaterBinary(AnovaDescriptionEntity, BinarySensorEntity):
    """A low-water binary sensor for Anova devices."""

    entity_description: AnovaBinaryEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return the binary state, or None when unknown."""
        value = self.entity_description.value_fn(self.coordinator.data.sensor)
        return None if value is None else bool(value)
