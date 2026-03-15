"""Support for Casambi cover devices (shutters, blinds, relays)."""

from __future__ import annotations

import logging
import time
from typing import Any, cast

from CasambiBt import Unit, UnitControlType, _operation

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi
from .const import DEFAULT_TRAVEL_TIME, DOMAIN
from .entities import CasambiUnitEntity, TypedEntityDescription

_LOGGER = logging.getLogger(__name__)

# Keywords in model names that indicate a cover/blind device
_COVER_MODEL_KEYWORDS = ("blind", "shutter", "curtain", "roller", "sto", "motor", "cover")


def is_cover_unit(unit: Unit) -> bool:
    """Return True if a unit is a shutter/blind/cover device.

    Cover devices are identified by:
    1. Having SLIDER control without DIMMER (motor/position, not a light)
    2. Having 2+ ONOFF controls without DIMMER (dual relay for up/down)
    3. Model name containing cover-related keywords as a fallback
    """
    controls = unit.unitType.controls
    control_types = {c.type for c in controls}
    has_dimmer = UnitControlType.DIMMER in control_types

    # Pattern 1: has SLIDER but no DIMMER (motor/position control)
    if UnitControlType.SLIDER in control_types and not has_dimmer:
        return True

    # Pattern 2: has 2+ ONOFF but no DIMMER (dual relay for up/down)
    onoff_count = sum(1 for c in controls if c.type == UnitControlType.ONOFF)
    if onoff_count >= 2 and not has_dimmer:
        return True

    # Pattern 3: model name fallback
    model_lower = unit.unitType.model.lower()
    if any(kw in model_lower for kw in _COVER_MODEL_KEYWORDS):
        return True

    return False


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Casambi cover entities from a config entry."""
    casa_api: CasambiApi = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[CasambiCover] = []
    for unit in casa_api.get_units():
        if is_cover_unit(unit):
            entities.append(CasambiCover(casa_api, unit))
            _LOGGER.debug(
                "Adding cover entity for unit: %s (uuid=%s, model=%s)",
                unit.name,
                unit.uuid,
                unit.unitType.model,
            )

    async_add_entities(entities)


class CasambiCover(CoverEntity, CasambiUnitEntity):
    """Represents a Casambi shutter/blind/relay cover device."""

    _attr_device_class = CoverDeviceClass.SHUTTER

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a Casambi cover entity."""
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")

        self._has_slider = unit.unitType.get_control(UnitControlType.SLIDER) is not None
        onoff_controls = [c for c in unit.unitType.controls if c.type == UnitControlType.ONOFF]
        self._has_dual_onoff = len(onoff_controls) >= 2
        # Store the individual ONOFF control descriptors for per-relay bit manipulation.
        # Devices with 4 ONOFF controls have: UP, DOWN (momentary), MAX UP, MAX DOWN (toggle).
        # Momentary controls only move while held, so HA must use MAX UP/MAX DOWN (last two).
        if len(onoff_controls) >= 4:
            self._onoff_controls = onoff_controls[2:4]
            _LOGGER.debug(
                "Unit %s has %d ONOFF controls; using last two (MAX UP/MAX DOWN) at offsets %s",
                unit.name,
                len(onoff_controls),
                [c.offset for c in self._onoff_controls],
            )
        else:
            self._onoff_controls = onoff_controls

        # For time-based position estimation on relay-only devices
        self._travel_time: float = DEFAULT_TRAVEL_TIME
        self._moving_since: float | None = None
        self._moving_direction: str | None = None  # "opening" or "closing"
        self._estimated_position: int | None = None

        self._obj: Unit
        super().__init__(api, desc, unit)

    @property
    def assumed_state(self) -> bool:
        """Return True if the state is estimated (relay-only, no position feedback)."""
        return not self._has_slider

    @property
    def supported_features(self) -> CoverEntityFeature:
        """Return supported features based on available controls."""
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        if self._has_slider:
            features |= CoverEntityFeature.SET_POSITION
        return features

    @property
    def current_cover_position(self) -> int | None:
        """Return current position (0=closed, 100=open)."""
        unit = cast("Unit", self._obj)
        if self._has_slider and unit.state is not None and unit.state.slider is not None:
            # Map Casambi 0-255 to HA 0-100
            return round(unit.state.slider / 255 * 100)
        return self._estimated_position

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed."""
        pos = self.current_cover_position
        if pos is not None:
            return pos == 0
        return None

    @property
    def is_opening(self) -> bool:
        """Return True if the cover is currently opening."""
        return self._moving_direction == "opening"

    @property
    def is_closing(self) -> bool:
        """Return True if the cover is currently closing."""
        return self._moving_direction == "closing"

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        unit = cast("Unit", self._obj)
        if self._has_slider:
            await self._api.casa.setSlider(unit, 255)
        elif self._has_dual_onoff:
            await self._send_relay_state(open_on=True, close_on=False)
        self._start_moving("opening")
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        unit = cast("Unit", self._obj)
        if self._has_slider:
            await self._api.casa.setSlider(unit, 0)
        elif self._has_dual_onoff:
            await self._send_relay_state(open_on=False, close_on=True)
        self._start_moving("closing")
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        unit = cast("Unit", self._obj)
        if self._has_slider:
            # Re-send current position to halt motor
            if unit.state is not None and unit.state.slider is not None:
                await self._api.casa.setSlider(unit, unit.state.slider)
        elif self._has_dual_onoff:
            await self._send_relay_state(open_on=False, close_on=False)
        self._stop_moving()
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set the cover position (0=closed, 100=open)."""
        position = kwargs[ATTR_POSITION]
        unit = cast("Unit", self._obj)
        if self._has_slider:
            casambi_value = round(position / 100 * 255)
            await self._api.casa.setSlider(unit, casambi_value)
            if position > (self.current_cover_position or 0):
                self._start_moving("opening")
            elif position < (self.current_cover_position or 100):
                self._start_moving("closing")
            self.async_write_ha_state()

    async def _send_relay_state(self, open_on: bool, close_on: bool) -> None:
        """Set individual relay states using raw state byte manipulation.

        UnitState only has a single onoff boolean which would set ALL ONOFF
        controls to the same value. For dual-relay covers, we need to set
        each relay independently by building raw state bytes and setting
        individual bits at each control's offset.

        Convention: first ONOFF control = open/up relay,
                    second ONOFF control = close/down relay.
        """
        unit = cast("Unit", self._obj)
        state_bytes = bytearray(unit.unitType.stateLength)

        if len(self._onoff_controls) >= 2:
            # Set the open/up relay bit
            if open_on:
                ctrl = self._onoff_controls[0]
                state_bytes[ctrl.offset // 8] |= 1 << (ctrl.offset % 8)

            # Set the close/down relay bit
            if close_on:
                ctrl = self._onoff_controls[1]
                state_bytes[ctrl.offset // 8] |= 1 << (ctrl.offset % 8)

        _LOGGER.debug(
            "Sending relay state for %s: open=%s close=%s bytes=%s",
            unit.name,
            open_on,
            close_on,
            state_bytes.hex(),
        )
        await self._api.casa._send(  # noqa: SLF001
            unit, bytes(state_bytes), _operation.OpCode.SetState
        )

    def _start_moving(self, direction: str) -> None:
        """Track the start of cover movement for time-based estimation."""
        self._moving_since = time.monotonic()
        self._moving_direction = direction

    def _stop_moving(self) -> None:
        """Stop tracking cover movement and update estimated position."""
        if self._moving_since is not None and self._moving_direction is not None:
            elapsed = time.monotonic() - self._moving_since
            travel_fraction = min(elapsed / self._travel_time, 1.0)

            current = self._estimated_position
            if current is None:
                current = 50  # Assume mid-point if unknown

            if self._moving_direction == "opening":
                self._estimated_position = min(100, current + round(travel_fraction * 100))
            else:
                self._estimated_position = max(0, current - round(travel_fraction * 100))

        self._moving_since = None
        self._moving_direction = None

    @callback
    def _change_callback(self, unit: Unit) -> None:
        """Handle state change from Casambi network."""
        if unit.state and self._has_slider:
            # Clear movement tracking when we get a real position update
            self._moving_since = None
            self._moving_direction = None
        super()._change_callback(unit)
