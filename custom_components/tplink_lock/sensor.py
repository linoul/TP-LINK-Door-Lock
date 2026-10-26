"""TP-LINK 门锁传感器平台（按账号下所有门锁创建实体）。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, DOMAIN
from .event import EVENT_TYPE_LABELS, classify_log

_LOGGER = logging.getLogger(__name__)


def _devices_of(entry: ConfigEntry) -> list[dict[str, Any]]:
    """条目下的门锁列表（兼容旧的单 device_id 格式）。"""
    devices = entry.data.get(CONF_DEVICES)
    if devices:
        return list(devices)
    return [{"device_id": entry.data["device_id"],
             "device_name": entry.data.get("device_name", "")}]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    entities = []
    for dev in _devices_of(entry):
        name = dev.get("device_name") or dev["device_id"][-8:]
        entities.extend([
            TPLinkBatterySensor(coordinator, dev["device_id"], name,
                                "battery"),
            TPLinkLockStateSensor(coordinator, dev["device_id"], name,
                                  "lock_state"),
            TPLinkLastEventSensor(coordinator, dev["device_id"], name,
                                  "last_event"),
        ])
    _LOGGER.debug("sensor 平台创建 %d 个实体", len(entities))
    async_add_entities(entities)


class TPLinkLockEntity(CoordinatorEntity, SensorEntity):
    """公共基类：绑定到具体门锁设备。"""

    _attr_has_entity_name = True
    # 子类必须定义；缺省兜底避免漏传参数时整平台加载失败
    _suffix = "unknown"

    def __init__(self, coordinator, device_id: str, device_name: str,
                 suffix: str = "") -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        # unique_id 是实体挂到设备的必要条件
        self._attr_unique_id = "%s_%s" % (device_id, suffix or self._suffix)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="TP-LINK",
            model="TL-SL41",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def _dev_data(self) -> dict[str, Any]:
        """本设备在协调器数据里的分片。"""
        return (self.coordinator.data or {}).get("devices", {}).get(
            self._device_id, {})

    @property
    def available(self) -> bool:
        # 有过一次成功数据就一直保持可用（失败时显示旧值，而不是变不可用）
        return self.coordinator.data is not None


class TPLinkBatterySensor(TPLinkLockEntity):
    """门锁电量。"""

    _attr_name = "电量"
    _suffix = "battery"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        cfg = self._dev_data.get("config") or {}
        try:
            return int(cfg["config"]["dev_manager"]["dev_status"]["power_level"])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self._dev_data.get("config") or {}
        try:
            ds = cfg["config"]["dev_manager"]["dev_status"]
            return {
                "usb_plugin": ds.get("usb_plugin"),
                "unlock_elec_val": ds.get("unlock_elec_val"),
                "lock_elec_val": ds.get("lock_elec_val"),
            }
        except (KeyError, TypeError):
            return {}


class TPLinkLockStateSensor(TPLinkLockEntity):
    """门锁状态（原始值 + 中文说明）。"""

    _attr_name = "门锁状态"
    _suffix = "lock_state"

    # 权威映射：反编译 SmartLockStatus.getLockStatusDesc() + res 字符串
    LOCK_STATUS = {
        "0": "正常",
        "1": "门锁破坏",
        "2": "已上锁",
        "3": "门未关",
        "4": "门虚掩",
        "5": "已开锁",
        "7": "门未锁好",
    }
    # App isWarning(): 1/3/4/7 视为告警态
    WARNING_STATUS = {"1", "3", "4", "7"}

    @property
    def native_value(self) -> str | None:
        cfg = self._dev_data.get("config") or {}
        try:
            raw = str(cfg["config"]["dev_manager"]["dev_status"]["lock_status"])
        except (KeyError, TypeError):
            return None
        return self.LOCK_STATUS.get(raw, "--")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self._dev_data.get("config") or {}
        try:
            raw = str(cfg["config"]["dev_manager"]["dev_status"]["lock_status"])
        except (KeyError, TypeError):
            return {}
        return {"原始值": raw, "告警": raw in self.WARNING_STATUS}


class TPLinkLastEventSensor(TPLinkLockEntity):
    """最近一条事件：状态直接显示事件内容（谁、用什么方式开门等）。"""

    _attr_name = "最近事件"
    _suffix = "last_event"

    @property
    def native_value(self) -> str | None:
        recent = self._dev_data.get("last_log")
        if not recent:
            return None
        return recent.get("logContent") or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        recent = self._dev_data.get("last_log")
        if not recent:
            return {}
        etype = classify_log(recent)
        ts = recent.get("logCreateTime")
        return {
            "事件类型": EVENT_TYPE_LABELS.get(etype, etype),
            "原始类型ID": recent.get("logTypeId"),
            "时间": datetime.fromtimestamp(ts / 1000).strftime(
                "%Y-%m-%d %H:%M:%S") if ts else None,
            "logId": recent.get("logId"),
        }
