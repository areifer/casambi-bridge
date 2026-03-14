#!/usr/bin/env python3
"""Diagnostic script to dump all Casambi unit info from a BLE network.

Connects to the Casambi network, enumerates all units, and prints
their controls, types, and current state. Saves output to JSON.

Usage:
    python dump_casambi_units.py --address <BLE_MAC> --password <network_password>

Example:
    python dump_casambi_units.py --address AA:BB:CC:DD:EE:FF --password mypassword
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from CasambiBt import Casambi, UnitControlType


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
_LOGGER = logging.getLogger(__name__)


def unit_control_type_name(ct: UnitControlType) -> str:
    """Return the name of a UnitControlType."""
    try:
        return ct.name
    except AttributeError:
        return str(ct)


async def dump_units(address: str, password: str, output_file: str) -> None:
    """Connect to Casambi network and dump all unit information."""
    casa = Casambi()

    _LOGGER.info("Connecting to Casambi network at %s ...", address)
    try:
        await casa.connect(address, password)
    except Exception as e:
        _LOGGER.error("Failed to connect: %s", e)
        sys.exit(1)

    _LOGGER.info("Connected to network: %s (ID: %s)", casa.networkName, casa.networkId)
    _LOGGER.info("Found %d unit(s)", len(casa.units))

    dump_data = {
        "network_name": casa.networkName,
        "network_id": casa.networkId,
        "units": [],
    }

    for unit in casa.units:
        _LOGGER.info("---")
        _LOGGER.info("Unit: %s", unit.name)
        _LOGGER.info("  UUID: %s", unit.uuid)
        _LOGGER.info("  Device ID: %d", unit.deviceId)
        _LOGGER.info("  Address: %s", unit.address)
        _LOGGER.info("  Firmware: %s", unit.firmwareVersion)
        _LOGGER.info("  Online: %s", unit.online)
        _LOGGER.info("  Is On: %s", unit.is_on)
        _LOGGER.info("  Type: %s (model=%s, manufacturer=%s, mode=%s)",
                      unit.unitType.id, unit.unitType.model,
                      unit.unitType.manufacturer, unit.unitType.mode)
        _LOGGER.info("  State Length: %d bytes", unit.unitType.stateLength)

        controls_data = []
        for ctrl in unit.unitType.controls:
            ctrl_info = {
                "type": unit_control_type_name(ctrl.type),
                "type_value": ctrl.type.value if isinstance(ctrl.type.value, int) else str(ctrl.type.value),
                "offset": ctrl.offset,
                "length": ctrl.length,
                "default": ctrl.default,
                "readonly": ctrl.readonly,
                "min": ctrl.min,
                "max": ctrl.max,
            }
            controls_data.append(ctrl_info)
            _LOGGER.info("  Control: type=%s offset=%d length=%d default=%d readonly=%s min=%s max=%s",
                          unit_control_type_name(ctrl.type), ctrl.offset, ctrl.length,
                          ctrl.default, ctrl.readonly, ctrl.min, ctrl.max)

        state_data = None
        if unit.state is not None:
            state_data = {
                "dimmer": unit.state.dimmer,
                "slider": unit.state.slider,
                "onoff": unit.state.onoff,
                "vertical": unit.state.vertical,
                "white": unit.state.white,
                "temperature": unit.state.temperature,
            }
            if unit.state.rgb is not None:
                state_data["rgb"] = list(unit.state.rgb)
            if unit.state.xy is not None:
                state_data["xy"] = list(unit.state.xy)

            _LOGGER.info("  State: dimmer=%s slider=%s onoff=%s vertical=%s",
                          unit.state.dimmer, unit.state.slider,
                          unit.state.onoff, unit.state.vertical)

        # Determine if this looks like a cover device
        control_types = {c.type for c in unit.unitType.controls}
        has_dimmer = UnitControlType.DIMMER in control_types
        has_slider = UnitControlType.SLIDER in control_types
        onoff_count = sum(1 for c in unit.unitType.controls if c.type == UnitControlType.ONOFF)
        is_cover = (has_slider and not has_dimmer) or (onoff_count >= 2 and not has_dimmer)

        _LOGGER.info("  Detected as: %s", "COVER" if is_cover else "LIGHT")

        unit_data = {
            "name": unit.name,
            "uuid": unit.uuid,
            "device_id": unit.deviceId,
            "address": unit.address,
            "firmware_version": unit.firmwareVersion,
            "online": unit.online,
            "is_on": unit.is_on,
            "unit_type": {
                "id": unit.unitType.id,
                "model": unit.unitType.model,
                "manufacturer": unit.unitType.manufacturer,
                "mode": unit.unitType.mode,
                "state_length": unit.unitType.stateLength,
            },
            "controls": controls_data,
            "state": state_data,
            "detected_type": "cover" if is_cover else "light",
        }
        dump_data["units"].append(unit_data)

    # Save to JSON
    with open(output_file, "w") as f:
        json.dump(dump_data, f, indent=2)
    _LOGGER.info("---")
    _LOGGER.info("Dump saved to %s", output_file)

    await casa.disconnect()
    _LOGGER.info("Disconnected.")


def main() -> None:
    """Parse arguments and run the dump."""
    parser = argparse.ArgumentParser(
        description="Dump Casambi BLE network unit information for diagnostics."
    )
    parser.add_argument(
        "--address", required=True, help="BLE MAC address of a Casambi device (e.g. AA:BB:CC:DD:EE:FF)"
    )
    parser.add_argument(
        "--password", required=True, help="Casambi network password"
    )
    parser.add_argument(
        "--output", default="casambi_debug_dump.json", help="Output JSON file (default: casambi_debug_dump.json)"
    )
    args = parser.parse_args()

    asyncio.run(dump_units(args.address, args.password, args.output))


if __name__ == "__main__":
    main()
