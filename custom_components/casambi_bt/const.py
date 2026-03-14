"""Constants for the Casambi Bluetooth integration."""

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "casambi_bt"

PLATFORMS = [Platform.BINARY_SENSOR, Platform.COVER, Platform.LIGHT, Platform.SCENE, Platform.NUMBER]

CONF_IMPORT_GROUPS: Final = "import_groups"
CONF_TRAVEL_TIME: Final = "travel_time"
DEFAULT_TRAVEL_TIME: Final = 30  # seconds for full open/close travel
