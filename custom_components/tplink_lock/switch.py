"""TP-LINK 门锁可写开关（switch 平台）。

字段来自 ShadowPromiseNetUtil.js：
  dev_manager.dev_config.pried_warning_enable  撬门告警
  dev_manager.dev_config.dual_verify_enable    双重验证
  dev_manager.dev_config.face_verify_enable    人脸验证
写入走 /biz/v1/setDeviceDesiredConfig，值为字符串 "1"/"0"。
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import TPLinkLockBaseEntity, devices_of, entity_common


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities = []
    for dev in devices_of(entry):
        common = entity_common(hass, entry, dev)
        entities.extend([
            TPLinkPriedWarningSwitch(**common, suffix="set_pried_warning"),
            TPLinkDualVerifySwitch(**common, suffix="set_dual_verify"),
            TPLinkFaceVerifySwitch(**common, suffix="set_face_verify"),
        ])
    async_add_entities(entities)


class _ConfigSwitch(TPLinkLockBaseEntity, SwitchEntity):
    """dev_config 布尔项的通用 switch 实体。"""

    _attr_icon = "mdi:shield-check-outline"
    _field = ""

    @property
    def is_on(self) -> bool:
        return str(self._dev_config.get(self._field) or "0") not in (
            "0", "false", "off", "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"云端原始值": self._dev_config.get(self._field)}

    async def _set(self, value: str) -> None:
        await self._async_write(
            {"dev_manager": {"dev_config": {self._field: value}}})

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set("1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set("0")


class TPLinkPriedWarningSwitch(_ConfigSwitch):
    _attr_name = "撬门告警"
    _suffix = "set_pried_warning"
    _attr_icon = "mdi:shield-alert"
    _field = "pried_warning_enable"


class TPLinkDualVerifySwitch(_ConfigSwitch):
    _attr_name = "双重验证"
    _suffix = "set_dual_verify"
    _attr_icon = "mdi:two-factor-authentication"
    _field = "dual_verify_enable"


class TPLinkFaceVerifySwitch(_ConfigSwitch):
    _attr_name = "人脸验证"
    _suffix = "set_face_verify"
    _attr_icon = "mdi:face-recognition"
    _field = "face_verify_enable"
