"""TP-LINK 门锁二进制传感器：上锁、告警、充电、在线、低电量。"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ALARM_EVENT_TYPES,
    CONF_DEVICES,
    DEFAULT_MODEL,
    DOMAIN,
    LOCK_STATUS_LABELS,
    LOCK_STATUS_LOCKED,
    LOCK_STATUS_WARNING,
    LOW_BATTERY_THRESHOLD,
    RECENT_ALARM_WINDOW_MINUTES,
)
from .event import classify_log
from .sensor import _as_bool, _as_float

_LOGGER = logging.getLogger(__name__)


def _devices_of(entry: ConfigEntry) -> list[dict[str, Any]]:
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
    models = data.get("device_models") or {}
    device_info = data.get("device_info") or {}
    entities: list[TPLinkBinaryEntity] = []
    for dev in _devices_of(entry):
        dev_id = dev["device_id"]
        common = dict(
            coordinator=coordinator,
            device_id=dev_id,
            device_name=dev.get("device_name") or dev_id[-8:],
            model=models.get(dev_id),
            device_info=device_info.get(dev_id) or {},
        )
        entities.extend([
            TPLinkLockedBinary(**common, suffix="is_locked"),
            TPLinkDoorAlarmBinary(**common, suffix="door_alarm"),
            TPLinkChargingBinary(**common, suffix="charging"),
            TPLinkLowBatteryBinary(**common, suffix="low_battery"),
            TPLinkRecentAlarmBinary(**common, suffix="recent_alarm"),
            # --- App 设置项开关状态（dev_config，云端只读）---
            TPLinkFaceVerifyBinary(**common, suffix="face_verify_enabled"),
            TPLinkDualVerifyBinary(**common, suffix="dual_verify_enabled"),
            TPLinkPriedWarningBinary(**common, suffix="pried_warning_enabled"),
            # --- 逗留侦测 ---
            TPLinkRecentLoiterBinary(**common, suffix="recent_loiter"),
        ])
    _LOGGER.debug("binary_sensor 平台创建 %d 个实体", len(entities))
    async_add_entities(entities)


class TPLinkBinaryEntity(CoordinatorEntity, BinarySensorEntity):
    """公共基类：与 sensor 平台使用同一 identifiers，保证挂到同一设备。"""

    _attr_has_entity_name = True
    _suffix = "unknown"
    # 见 sensor.py 同名说明：False = 接入后默认禁用，可在设备页手动启用
    _enabled_default = True

    def __init__(self, coordinator, device_id: str, device_name: str,
                 suffix: str = "", model: str | None = None,
                 device_info: dict[str, Any] | None = None) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        info = device_info or {}
        self._attr_entity_registry_enabled_default = self._enabled_default
        self._attr_unique_id = "%s_%s" % (device_id, suffix or self._suffix)
        dev_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="TP-LINK",
            model=model or DEFAULT_MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )
        sw = info.get("fwVer")
        if sw:
            dev_info["sw_version"] = str(sw)
        hw = info.get("hwVer") or info.get("deviceHwVer")
        if hw:
            dev_info["hw_version"] = str(hw)
        mac = info.get("deviceMac") or info.get("mac")
        if mac:
            dev_info["serial_number"] = str(mac)
        self._attr_device_info = dev_info

    @property
    def _dev_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("devices", {}).get(
            self._device_id, {})

    @property
    def _dev_status(self) -> dict[str, Any]:
        cfg = self._dev_data.get("config") or {}
        try:
            return cfg["config"]["dev_manager"]["dev_status"] or {}
        except (KeyError, TypeError):
            return {}

    @property
    def _dev_config(self) -> dict[str, Any]:
        """dev_config：App 里的门锁设置项（云端只读）。"""
        cfg = self._dev_data.get("config") or {}
        try:
            return cfg["config"]["dev_manager"]["dev_config"] or {}
        except (KeyError, TypeError):
            return {}

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def _lock_raw(self) -> str:
        return str(self._dev_status.get("lock_status") or "")


class TPLinkLockedBinary(TPLinkBinaryEntity):
    """已上锁（lock_status=2）。不用 LOCK device_class，避免 on/off 语义反转。"""

    _attr_name = "已上锁"
    _suffix = "is_locked"
    _attr_icon = "mdi:lock"

    @property
    def is_on(self) -> bool | None:
        raw = self._lock_raw
        if not raw:
            return None
        return raw == LOCK_STATUS_LOCKED

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "门锁状态": LOCK_STATUS_LABELS.get(self._lock_raw, "--"),
            "原始值": self._lock_raw or None,
        }


class TPLinkDoorAlarmBinary(TPLinkBinaryEntity):
    """门异常：破坏 / 门未关 / 虚掩 / 未锁好（App isWarning 的四种状态）。"""

    _attr_name = "门锁告警"
    _suffix = "door_alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self) -> bool | None:
        raw = self._lock_raw
        if not raw:
            return None
        return raw in LOCK_STATUS_WARNING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "门锁状态": LOCK_STATUS_LABELS.get(self._lock_raw, "--"),
            "原始值": self._lock_raw or None,
        }


class TPLinkChargingBinary(TPLinkBinaryEntity):
    """USB 供电 / 充电中。"""

    _attr_name = "充电中"
    _suffix = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool | None:
        return _as_bool(self._dev_status.get("usb_plugin"))


# 「在线 / 已连接」实体已并入 sensor.设备状态：
# state = 在线 / 离线 / 未知，属性含布尔「在线」，自动化改用 state 触发。


class TPLinkLowBatteryBinary(TPLinkBinaryEntity):
    """低电量告警（默认阈值 20%）。"""

    _attr_name = "低电量"
    _suffix = "low_battery"
    _attr_device_class = BinarySensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool | None:
        value = _as_float(self._dev_status.get("power_level"))
        return None if value is None else value < LOW_BATTERY_THRESHOLD

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "电量": _as_float(self._dev_status.get("power_level")),
            "阈值": LOW_BATTERY_THRESHOLD,
        }


class TPLinkConfigSwitchBinary(TPLinkBinaryEntity):
    """dev_config 布尔设置项：App 里开关的当前状态（只读）。

    与可写 switch 实体（撬门告警 / 双重验证 / 人脸验证）一一对应，
    属于重复项，默认禁用；需要的用户可在设备页启用。
    """

    _field = ""
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _enabled_default = False

    @property
    def is_on(self) -> bool | None:
        return _as_bool(self._dev_config.get(self._field))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "原始字段": "dev_config.%s" % self._field,
            "原始值": self._dev_config.get(self._field),
            "说明": "对应可写 switch 实体的只读镜像，默认隐藏",
        }


class TPLinkFaceVerifyBinary(TPLinkConfigSwitchBinary):
    _attr_name = "人脸验证已开启"
    _suffix = "face_verify_enabled"
    _field = "face_verify_enable"
    _attr_icon = "mdi:face-recognition"


class TPLinkDualVerifyBinary(TPLinkConfigSwitchBinary):
    _attr_name = "双重验证已开启"
    _suffix = "dual_verify_enabled"
    _field = "dual_verify_enable"
    _attr_icon = "mdi:two-factor-authentication"


class TPLinkPriedWarningBinary(TPLinkConfigSwitchBinary):
    _attr_name = "撬门告警已开启"
    _suffix = "pried_warning_enabled"
    _field = "pried_warning_enable"
    _attr_icon = "mdi:shield-lock-outline"


class TPLinkRecentAlarmBinary(TPLinkBinaryEntity):
    """近期告警：窗口期内出现门异常/防撬/逗留/验证失败/低电量事件即置 on。

    App 的告警是瞬态推送，HA 里转成可持续状态便于仪表盘与自动化；
    窗口过后随下一次轮询自动复位，无需手动清理。
    """

    _attr_name = "近期告警"
    _suffix = "recent_alarm"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle-outline"

    def _alarm_logs(self) -> list[dict[str, Any]]:
        cutoff = (time.time() - RECENT_ALARM_WINDOW_MINUTES * 60) * 1000
        return [log for log in (self._dev_data.get("recent_logs") or [])
                if classify_log(log) in ALARM_EVENT_TYPES
                and (log.get("logCreateTime") or 0) >= cutoff]

    @property
    def is_on(self) -> bool | None:
        if self._dev_data.get("recent_logs") is None:
            return None
        return bool(self._alarm_logs())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._alarm_logs()
        return {
            "窗口分钟": RECENT_ALARM_WINDOW_MINUTES,
            "告警条数": len(logs),
            "最近告警": [log.get("logContent") for log in logs[:5]],
            "告警类型": [classify_log(log) for log in logs[:5]],
        }


class TPLinkRecentLoiterBinary(TPLinkBinaryEntity):
    """近期门前逗留：窗口期内出现逗留侦测事件即置 on。

    对应 App 的「逗留侦测」推送，转成 HA 里可持续判读的状态，
    并带上该次抓拍图地址，便于自动化推送带图通知。
    """

    _attr_name = "近期门前逗留"
    _suffix = "recent_loiter"
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_icon = "mdi:account-clock-outline"

    def _loiter_logs(self) -> list[dict[str, Any]]:
        cutoff = (time.time() - RECENT_ALARM_WINDOW_MINUTES * 60) * 1000
        return [log for log in (self._dev_data.get("recent_logs") or [])
                if classify_log(log) == "loiter"
                and (log.get("logCreateTime") or 0) >= cutoff]

    @property
    def is_on(self) -> bool | None:
        if self._dev_data.get("recent_logs") is None:
            return None
        return bool(self._loiter_logs())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._loiter_logs()
        return {
            "窗口分钟": RECENT_ALARM_WINDOW_MINUTES,
            "逗留条数": len(logs),
            "抓拍图地址": logs[0].get("snapshotUrl") if logs else None,
            "逗留时长毫秒": logs[0].get("duration") if logs else None,
            "录像大小字节": logs[0].get("fileSize") if logs else None,
        }
