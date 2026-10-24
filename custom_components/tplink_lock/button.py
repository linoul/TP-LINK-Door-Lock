"""TP-LINK 门锁按钮平台。"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import TPLinkApiError, TPLinkAuthError, TPLinkCloudError
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([TPLinkRefreshTodayOpenRecordsButton(coordinator, entry)])


class TPLinkRefreshTodayOpenRecordsButton(CoordinatorEntity, ButtonEntity):
    """从云端重新拉取当天开门记录。"""

    _attr_name = "刷新今日开门记录"
    _attr_icon = "mdi:refresh"
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        device_id = entry.data["device_id"]
        self._attr_unique_id = "%s_refresh_today_open_records" % device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.device_name,
            manufacturer="TP-LINK",
            model="TL-SL41",
            serial_number=device_id,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        try:
            await self.coordinator.async_refresh_today_open_records()
        except (TPLinkApiError, TPLinkAuthError, TPLinkCloudError) as err:
            raise HomeAssistantError("刷新今日开门记录失败: %s" % err) from err
