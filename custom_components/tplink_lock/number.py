"""TP-LINK 门锁可写数值设置（number 平台）。

字段来自 ShadowPromiseNetUtil.js 的 setAutoLockDelay：
  dev_manager.dev_config.auto_lock_delay  自动上锁延时（秒）
  dev_manager.dev_config.face_verify_interval  人脸验证间隔（秒）
写入走 /biz/v1/setDeviceDesiredConfig。云端字段为字符串。
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity
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
            TPLinkAutoLockDelayNumber(**common, suffix="set_auto_lock_delay"),
            TPLinkFaceVerifyIntervalNumber(
                **common, suffix="set_face_verify_interval"),
        ])
    async_add_entities(entities)


class _ConfigNumber(TPLinkLockBaseEntity, NumberEntity):
    """dev_config 数值项的通用 number 实体。"""

    _attr_native_min_value = 0
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:timer-outline"
    _field = ""

    @property
    def native_value(self) -> float | None:
        raw = self._dev_config.get(self._field)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"云端原始值": self._dev_config.get(self._field)}

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(
            {"dev_manager": {"dev_config": {self._field: str(int(value))}}})


class TPLinkAutoLockDelayNumber(_ConfigNumber):
    _attr_name = "自动上锁延时"
    _suffix = "set_auto_lock_delay"
    _field = "auto_lock_delay"


class TPLinkFaceVerifyIntervalNumber(_ConfigNumber):
    _attr_name = "人脸验证间隔"
    _suffix = "set_face_verify_interval"
    _field = "face_verify_interval"
