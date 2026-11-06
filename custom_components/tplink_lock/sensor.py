"""TP-LINK 门锁传感器平台（按账号下所有门锁创建实体）。

默认可见性约定（`_enabled_default`）：
  * True —— 门锁状态、电量、事件、计数等「日常要看」的实体，正常显示。
  * False —— 接入后即在实体注册表里禁用（需要时可在设备页手动启用）：
      1) 与可写 select / number / switch 重复的只读镜像，
         如 自动上锁延时、人脸验证间隔、人脸验证模式、待机模式；
      2) 纯诊断信息：固件版本、设备型号、开锁/上锁电机读数、WiFi 细节、响铃计划。
    这些内容在设备页信息卡片或可写实体上已经能看到，不必占列表。
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

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

from .const import (
    CONF_DEVICES,
    DEFAULT_MODEL,
    DOMAIN,
    FACE_DETECT_SENSITIVITY_LABELS,
    FACE_VERIFY_MODE_LABELS,
    LOCK_STATUS_LABELS,
    LOCK_STATUS_WARNING,
    STANDBY_MODE_LABELS,
    TEMPLATE_ID_LABELS,
    TEMPLATE_UNLOCK_METHOD,
    UNLOCK_METHOD_KEYWORDS,
    VERIFY_TYPE_LABELS,
)
from .event import EVENT_TYPE_LABELS, classify_log

_LOGGER = logging.getLogger(__name__)


def _devices_of(entry: ConfigEntry) -> list[dict[str, Any]]:
    """条目下的门锁列表（兼容旧的单 device_id 格式）。"""
    devices = entry.data.get(CONF_DEVICES)
    if devices:
        return list(devices)
    return [{"device_id": entry.data["device_id"],
             "device_name": entry.data.get("device_name", "")}]


def _as_float(value: Any) -> float | None:
    """云端字段可能是 int / str / None，统一转 float。"""
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    """云端布尔字段可能是 true / 1 / "1" / "0"，显式处理避免 bool("0")。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in ("", "none", "null", "unknown"):
        return None
    return text not in ("0", "false", "no", "off", "n")


def _ts_datetime(ts: Any) -> datetime | None:
    """云端毫秒时间戳 -> UTC aware datetime（timestamp sensor 需要）。"""
    try:
        return datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _is_today(ts: Any) -> bool:
    try:
        return datetime.fromtimestamp(float(ts) / 1000).date() == \
            datetime.now().date()
    except (TypeError, ValueError, OSError, OverflowError):
        return False


def unlock_method_of(log: dict[str, Any]) -> str:
    """开锁方式：templateId / verifyType 优先，未知时按日志文案关键词兜底。"""
    verify = str(log.get("verifyType") or "")
    label = VERIFY_TYPE_LABELS.get(verify)
    if label:
        return label
    content = str(log.get("logContent") or "")
    for keyword, name in UNLOCK_METHOD_KEYWORDS:
        if keyword in content:
            return name
    template = str(log.get("templateId") or "")
    if template in TEMPLATE_UNLOCK_METHOD:
        return TEMPLATE_UNLOCK_METHOD[template]
    if log.get("verifyType") not in (None, "", 0, "0"):
        return "验证方式 %s" % verify
    return "其他"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    models = data.get("device_models") or {}
    device_info = data.get("device_info") or {}
    entities: list[TPLinkLockEntity] = []
    for dev in _devices_of(entry):
        dev_id = dev["device_id"]
        name = dev.get("device_name") or dev_id[-8:]
        model = models.get(dev_id)
        info = device_info.get(dev_id) or {}
        common = dict(coordinator=coordinator, device_id=dev_id,
                      device_name=name, model=model, device_info=info)
        entities.extend([
            TPLinkBatterySensor(**common, suffix="battery"),
            TPLinkLockStateSensor(**common, suffix="lock_state"),
            TPLinkLastEventSensor(**common, suffix="last_event"),
            TPLinkLastEventTimeSensor(**common, suffix="last_event_time"),
            TPLinkLastUnlockMethodSensor(**common,
                                         suffix="last_unlock_method"),
            TPLinkLastDoorbellSensor(**common, suffix="last_doorbell_time"),
            TPLinkTodayUnlockCountSensor(**common,
                                         suffix="today_unlock_count"),
            TPLinkTodayEventCountSensor(**common, suffix="today_event_count"),
            TPLinkDeviceStatusSensor(**common, suffix="device_status"),
            TPLinkFirmwareSensor(**common, suffix="firmware"),
            TPLinkModelSensor(**common, suffix="device_model"),
            TPLinkSecondaryBatterySensor(**common, suffix="battery_li"),
            TPLinkUnlockElecSensor(**common, suffix="unlock_elec_val"),
            TPLinkLockElecSensor(**common, suffix="lock_elec_val"),
            # --- App 设置项（dev_config，云端只读）---
            TPLinkAutoLockDelaySensor(**common, suffix="auto_lock_delay"),
            TPLinkFaceVerifyIntervalSensor(**common,
                                           suffix="face_verify_interval"),
            TPLinkFaceVerifyModeSensor(**common, suffix="face_verify_mode"),
            TPLinkFaceSensitivitySensor(**common,
                                        suffix="face_detect_sensitivity"),
            TPLinkStandbyModeSensor(**common, suffix="standby_mode"),
            # --- 逗留侦测 / 录像元数据 ---
            TPLinkLastLoiterTimeSensor(**common, suffix="last_loiter_time"),
            TPLinkTodayLoiterCountSensor(**common, suffix="today_loiter_count"),
            TPLinkLastLoiterDurationSensor(**common,
                                           suffix="last_loiter_duration"),
            TPLinkLastLoiterVideoSizeSensor(**common,
                                            suffix="last_loiter_video_size"),
            # --- 影子系统新增模块：WiFi 状态 / 响铃计划（只读）---
            TPLinkWifiRssiSensor(**common, suffix="wifi_rssi"),
            TPLinkWifiSsidSensor(**common, suffix="wifi_ssid"),
            TPLinkRingScheduleSensor(**common, suffix="ring_schedule"),
        ])
    _LOGGER.debug("sensor 平台创建 %d 个实体", len(entities))
    async_add_entities(entities)


class TPLinkLockEntity(CoordinatorEntity, SensorEntity):
    """公共基类：绑定到具体门锁设备。"""

    _attr_has_entity_name = True
    # 子类必须定义；缺省兜底避免漏传参数时整平台加载失败
    _suffix = "unknown"
    # 默认是否在实体注册表里启用。False = 接入后即为「禁用/隐藏」，
    # 不占用仪表盘和设备页主列表，需要时用户可在设备页手动启用。
    # 用于两类：1) 与可写 select/number/switch 重复的只读镜像；
    #          2) 纯诊断信息（固件、型号、电机读数、WiFi 细节等）。
    _enabled_default = True

    def __init__(self, coordinator, device_id: str, device_name: str,
                 suffix: str = "", model: str | None = None,
                 device_info: dict[str, Any] | None = None) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_info = device_info or {}
        self._attr_entity_registry_enabled_default = self._enabled_default
        # unique_id 是实体挂到设备的必要条件
        self._attr_unique_id = "%s_%s" % (device_id, suffix or self._suffix)
        dev_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="TP-LINK",
            model=model or DEFAULT_MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )
        # 固件/硬件/MAC 交给实体注册流程写入设备页（显式 update_device
        # 对已存在设备有时不生效，这里用标准 DeviceInfo 更可靠）
        sw = self._device_info.get("fwVer")
        if sw:
            dev_info["sw_version"] = str(sw)
        hw = self._device_info.get("hwVer") or self._device_info.get("deviceHwVer")
        if hw:
            dev_info["hw_version"] = str(hw)
        mac = self._device_info.get("deviceMac") or self._device_info.get("mac")
        if mac:
            dev_info["serial_number"] = str(mac)
        self._attr_device_info = dev_info

    @property
    def _dev_data(self) -> dict[str, Any]:
        """本设备在协调器数据里的分片。"""
        return (self.coordinator.data or {}).get("devices", {}).get(
            self._device_id, {})

    @property
    def available(self) -> bool:
        # 有过一次成功数据就一直保持可用（失败时显示旧值，而不是变不可用）
        return self.coordinator.data is not None

    @property
    def _root(self) -> dict[str, Any]:
        """影子配置模块根节点：dev_manager / audio / audio_lib / network。"""
        return (self._dev_data.get("config") or {}).get("config") or {}

    @property
    def _dev_status(self) -> dict[str, Any]:
        """dev_status 原始字典（云端字段，可能随固件变化）。"""
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
    def _dev_info_section(self) -> dict[str, Any]:
        """dev_info：各子模块固件、MAC、二维码等（云端字段为 URL 编码）。"""
        cfg = self._dev_data.get("config") or {}
        try:
            return cfg["config"]["dev_manager"]["dev_info"] or {}
        except (KeyError, TypeError):
            return {}

    @property
    def _recent_logs(self) -> list[dict[str, Any]]:
        return self._dev_data.get("recent_logs") or []

    def _logs_of_type(self, *types: str) -> list[dict[str, Any]]:
        return [log for log in self._recent_logs
                if classify_log(log) in types]

    def _loiter_logs(self) -> list[dict[str, Any]]:
        """逗留侦测日志（templateId=96），按时间倒序。"""
        return self._logs_of_type("loiter")


class TPLinkBatterySensor(TPLinkLockEntity):
    """干电池电量（power_level），与锂电池电量（power_level_li）区分。"""

    _attr_name = "干电池电量"
    _suffix = "battery"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        return _as_float(self._dev_status.get("power_level"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ds = self._dev_status
        return {
            "usb_plugin": ds.get("usb_plugin"),
            "unlock_elec_val": ds.get("unlock_elec_val"),
            "lock_elec_val": ds.get("lock_elec_val"),
        }


class TPLinkLockStateSensor(TPLinkLockEntity):
    """门锁状态（原始值 + 中文说明）。"""

    _attr_name = "门锁状态"
    _suffix = "lock_state"

    @property
    def native_value(self) -> str | None:
        raw = str(self._dev_status.get("lock_status") or "")
        if not raw:
            return None
        return LOCK_STATUS_LABELS.get(raw, "--")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = str(self._dev_status.get("lock_status") or "")
        if not raw:
            return {}
        return {"原始值": raw, "告警": raw in LOCK_STATUS_WARNING}


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
        template_id = str(recent.get("templateId") or "")
        verify_type = str(recent.get("verifyType") or "")
        dt = _ts_datetime(ts)
        attrs = {
            "事件类型": EVENT_TYPE_LABELS.get(etype, etype),
            "原始类型ID": recent.get("logTypeId"),
            "时间": dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else None,
            "logId": recent.get("logId"),
            # SL42 Pro 结构化字段：供自动化精确区分人脸/指纹/逗留等
            "模板ID": recent.get("templateId"),
            "模板类型": TEMPLATE_ID_LABELS.get(template_id),
            "验证方式ID": recent.get("verifyType"),
            "验证方式": VERIFY_TYPE_LABELS.get(verify_type),
        }
        # 逗留/门铃等告警日志会附带抓拍图与录像元数据，直接暴露给自动化
        if recent.get("snapshotUrl"):
            attrs["抓拍图地址"] = recent["snapshotUrl"]
        if recent.get("baseUrl"):
            attrs["录像地址"] = recent["baseUrl"]
        if recent.get("duration") is not None:
            attrs["逗留时长毫秒"] = recent["duration"]
        if recent.get("fileSize") is not None:
            attrs["录像大小字节"] = recent["fileSize"]
        return attrs


class TPLinkLastEventTimeSensor(TPLinkLockEntity):
    """最近一条事件发生时间（timestamp，可用于自动化与相对时间显示）。"""

    _attr_name = "最近事件时间"
    _suffix = "last_event_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        recent = self._dev_data.get("last_log")
        return _ts_datetime(recent.get("logCreateTime")) if recent else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        recent = self._dev_data.get("last_log") or {}
        return {
            "事件类型": EVENT_TYPE_LABELS.get(
                classify_log(recent), classify_log(recent)) if recent else None,
            "内容": recent.get("logContent"),
        }


class TPLinkLastUnlockMethodSensor(TPLinkLockEntity):
    """最近一次开门用的是什么方式（人脸/指纹/密码/门卡/远程等）。"""

    _attr_name = "最近开锁方式"
    _suffix = "last_unlock_method"
    _attr_icon = "mdi:lock-open-variant"

    @property
    def native_value(self) -> str | None:
        logs = self._logs_of_type("unlock", "remote_unlock")
        return unlock_method_of(logs[0]) if logs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._logs_of_type("unlock", "remote_unlock")
        if not logs:
            return {}
        log = logs[0]
        dt = _ts_datetime(log.get("logCreateTime"))
        return {
            "内容": log.get("logContent"),
            "时间": dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt else None,
            "验证方式ID": log.get("verifyType"),
            "模板ID": log.get("templateId"),
        }


class TPLinkLastDoorbellSensor(TPLinkLockEntity):
    """最近一次门铃呼叫时间（App 的访客呼叫功能在 HA 的对应物）。"""

    _attr_name = "最近门铃时间"
    _suffix = "last_doorbell_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:doorbell-video"

    @property
    def native_value(self) -> datetime | None:
        logs = self._logs_of_type("doorbell")
        return _ts_datetime(logs[0].get("logCreateTime")) if logs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._logs_of_type("doorbell")
        if not logs:
            return {}
        return {
            "内容": logs[0].get("logContent"),
            "最近 50 条内门铃次数": len(logs),
        }


class TPLinkTodayUnlockCountSensor(TPLinkLockEntity):
    """今日开门次数（基于云端日志，App 的开门记录）。"""

    _attr_name = "今日开门次数"
    _suffix = "today_unlock_count"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:counter"

    @property
    def native_value(self) -> int:
        return sum(1 for log in self._logs_of_type("unlock", "remote_unlock")
                   if _is_today(log.get("logCreateTime")))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = [log for log in self._logs_of_type("unlock", "remote_unlock")
                if _is_today(log.get("logCreateTime"))]
        return {
            "开锁方式分布": dict(Counter(unlock_method_of(log) for log in logs)),
            "统计样本条数": len(self._recent_logs),
        }


class TPLinkTodayEventCountSensor(TPLinkLockEntity):
    """今日全部事件数与按类型分布。"""

    _attr_name = "今日事件数"
    _suffix = "today_event_count"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-bar"

    @property
    def native_value(self) -> int:
        return sum(1 for log in self._recent_logs
                   if _is_today(log.get("logCreateTime")))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = [log for log in self._recent_logs
                if _is_today(log.get("logCreateTime"))]
        types = Counter(classify_log(log) for log in logs)
        return {
            "类型分布": {EVENT_TYPE_LABELS.get(k, k): v
                     for k, v in sorted(types.items())},
            "最近 5 条": [log.get("logContent") for log in logs[:5]],
            "统计样本条数": len(self._recent_logs),
        }


class TPLinkDeviceStatusSensor(TPLinkLockEntity):
    """设备状态：合并原「设备状态」与「在线/已连接」两个实体。

    state 直接显示 在线 / 离线 / 未知，属性里同时给布尔值与云端原始值，
    供自动化和仪表盘使用；dev_status 全字段继续原样展开，便于发现新能力。
    """

    _attr_name = "设备状态"
    _suffix = "device_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    @property
    def native_value(self) -> str:
        online = _as_bool(self._dev_data.get("online"))
        if online is None:
            return "未知"
        return "在线" if online else "离线"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # 把云端 dev_status 原样展开，固件升级后新增字段会自动出现在这里
        attrs: dict[str, Any] = {
            "在线": _as_bool(self._dev_data.get("online")),
            "online 原始值": self._dev_data.get("online"),
            "dev_status 字段": dict(self._dev_status),
            "设备元信息": dict(self._device_info),
        }
        return attrs


class TPLinkFirmwareSensor(TPLinkLockEntity):
    """固件版本：合并原「固件版本」与「固件明细」两个实体。

    state 是主控固件版本；属性里带设备列表元信息与 dev_info 里的
    前板 / 触摸 / WiFi / 电机板等子模块固件、MAC、二维码。
    """

    _attr_name = "固件版本"
    _suffix = "firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"
    _enabled_default = False  # 设备页已展示固件信息，实体默认隐藏

    @property
    def native_value(self) -> str | None:
        info = self._device_info
        value = info.get("fwVer") or info.get("fwVersion")
        return str(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._device_info
        attrs: dict[str, Any] = {
            "硬件版本": info.get("hwVer") or info.get("deviceHwVer"),
            "设备名称": info.get("deviceName") or info.get("alias"),
            "区域": info.get("region"),
            "MAC": info.get("deviceMac") or info.get("mac"),
        }
        # 子模块固件（dev_info，云端字段是 URL 编码）
        sub = self._dev_info_section
        labels = {
            "fw_major": "主控固件", "fw_front_panel": "前板固件",
            "fw_touch": "触摸固件", "fw_wifi_panel": "WiFi 模组固件",
            "fw_motor_panel": "电机板固件", "hw_version": "子模块硬件版本",
            "device_model": "云端型号", "qr_code": "设备二维码",
        }
        attrs.update({label: unquote(str(sub[key]))
                      for key, label in labels.items()
                      if sub.get(key) is not None})
        return attrs


class TPLinkModelSensor(TPLinkLockEntity):
    """设备型号。"""

    _attr_name = "设备型号"
    _suffix = "device_model"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:identifier"
    _enabled_default = False  # 设备页「型号」已有，实体默认隐藏

    @property
    def native_value(self) -> str | None:
        value = self._device_info.get("deviceModel") or (
            self._attr_device_info or {}).get("model")
        return str(value) if value is not None else DEFAULT_MODEL

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "设备类型": self._device_info.get("deviceType"),
            "设备 ID": self._device_id,
        }


class TPLinkSecondaryBatterySensor(TPLinkLockEntity):
    """锂电池电量（dev_status.power_level_li，与主电量不同的一路读数）。"""

    _attr_name = "锂电池电量"
    _suffix = "battery_li"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        return _as_float(self._dev_status.get("power_level_li"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "原始字段": "power_level_li",
            "主电量": _as_float(self._dev_status.get("power_level")),
            "电量推送类型": self._dev_status.get("pwr_push_type"),
        }


class TPLinkElecValSensor(TPLinkLockEntity):
    """锁体电机读数（云端原始值，单位未公开，用于判断锁体阻力/卡滞）。"""

    _field = ""
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:current-dc"
    _enabled_default = False  # 云端原始读数，仅排障用，默认隐藏

    @property
    def native_value(self) -> float | None:
        return _as_float(self._dev_status.get(self._field))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "原始字段": self._field,
            "说明": "云端原始读数，单位未公开；持续升高可能意味着锁体阻力变大",
        }


class TPLinkUnlockElecSensor(TPLinkElecValSensor):
    _attr_name = "开锁电机读数"
    _suffix = "unlock_elec_val"
    _field = "unlock_elec_val"


class TPLinkLockElecSensor(TPLinkElecValSensor):
    _attr_name = "上锁电机读数"
    _suffix = "lock_elec_val"
    _field = "lock_elec_val"


# ---------------------------------------------------------------------------
# App 设置项（getDeviceConfig -> dev_manager.dev_config）
#
# 这些是 TP-LINK 物联 App「门锁设置」页里的开关与参数。可写版本已做成
# select / number / switch 实体（见 select.py / number.py / switch.py），
# 因此这里的只读版本一律 `_enabled_default = False`，默认隐藏，避免同一项
# 在设备页出现两个实体（上面一个开关、下面一个读数）。
# ---------------------------------------------------------------------------

class TPLinkConfigValueSensor(TPLinkLockEntity):
    """dev_config 数值型设置项。"""

    _field = ""
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cog-outline"

    @property
    def native_value(self) -> float | None:
        return _as_float(self._dev_config.get(self._field))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "原始字段": "dev_config.%s" % self._field,
            "原始值": self._dev_config.get(self._field),
            "来源": "TP-LINK 物联 App 设置（云端只读）",
        }


class TPLinkConfigMappedSensor(TPLinkLockEntity):
    """dev_config 枚举型设置项：数字转中文，未知值保留原始数字。"""

    _field = ""
    _labels: dict[str, str] = {}
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cog-outline"

    @property
    def native_value(self) -> str | None:
        raw = self._dev_config.get(self._field)
        if raw is None or raw == "":
            return None
        key = str(raw)
        return self._labels.get(key, key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "原始字段": "dev_config.%s" % self._field,
            "原始值": self._dev_config.get(self._field),
            "可选值": dict(self._labels),
            "来源": "TP-LINK 物联 App 设置（云端只读）",
        }


class TPLinkAutoLockDelaySensor(TPLinkConfigValueSensor):
    """自动上锁延时（App：自动上锁时间）。"""

    _attr_name = "自动上锁延时（只读）"
    _suffix = "auto_lock_delay"
    _field = "auto_lock_delay"
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:timer-lock-outline"
    _enabled_default = False  # 与可写 number 实体重复，默认隐藏


class TPLinkFaceVerifyIntervalSensor(TPLinkConfigValueSensor):
    """人脸验证间隔（同一张脸两次识别之间的最短间隔）。"""

    _attr_name = "人脸验证间隔（只读）"
    _suffix = "face_verify_interval"
    _field = "face_verify_interval"
    _attr_native_unit_of_measurement = "s"
    _attr_icon = "mdi:face-recognition"
    _enabled_default = False  # 与可写 number 实体重复，默认隐藏


class TPLinkFaceVerifyModeSensor(TPLinkConfigMappedSensor):
    _attr_name = "人脸验证模式（只读）"
    _suffix = "face_verify_mode"
    _field = "face_verify_mode"
    _labels = FACE_VERIFY_MODE_LABELS
    _enabled_default = False  # 与可写 select 实体重复，默认隐藏


class TPLinkFaceSensitivitySensor(TPLinkConfigMappedSensor):
    _attr_name = "人脸检测灵敏度（只读）"
    _suffix = "face_detect_sensitivity"
    _field = "face_detect_sensitivity"
    _labels = FACE_DETECT_SENSITIVITY_LABELS
    _enabled_default = False  # 与可写 select 实体重复，默认隐藏


class TPLinkStandbyModeSensor(TPLinkConfigMappedSensor):
    _attr_name = "待机模式（只读）"
    _suffix = "standby_mode"
    _field = "standby_mode"
    _labels = STANDBY_MODE_LABELS
    _enabled_default = False  # 与可写 select 实体重复，默认隐藏


# 「固件明细」实体已并入 TPLinkFirmwareSensor.extra_state_attributes，
# 保留同一 unique_id（..._firmware），避免设备页出现两个重复固件实体。


# ---------------------------------------------------------------------------
# 逗留侦测 / 录像元数据（templateId=96）
#
# 云端日志自带 snapshotUrl（可直接下载的 JPEG）、baseUrl（私有 OSS，
# 需签名，HA 侧暂时无法直接播放）、duration（毫秒）、fileSize（字节）。
# ---------------------------------------------------------------------------

class TPLinkLastLoiterTimeSensor(TPLinkLockEntity):
    """最近一次「有人在门前逗留」的时间，属性里带抓拍图与录像信息。"""

    _attr_name = "最近门前逗留时间"
    _suffix = "last_loiter_time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:account-clock-outline"

    @property
    def native_value(self) -> datetime | None:
        logs = self._loiter_logs()
        return _ts_datetime(logs[0].get("logCreateTime")) if logs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._loiter_logs()
        if not logs:
            return {}
        log = logs[0]
        return {
            "内容": log.get("logContent"),
            "抓拍图地址": log.get("snapshotUrl"),
            "录像地址": log.get("baseUrl"),
            "录像地址说明": "私有 OSS，需云端签名，HA 内不可直接播放",
            "逗留时长毫秒": log.get("duration"),
            "录像大小字节": log.get("fileSize"),
            "最近 50 条内逗留次数": len(logs),
            "logId": log.get("logId"),
        }


class TPLinkTodayLoiterCountSensor(TPLinkLockEntity):
    """今日门前逗留次数（基于最近 50 条云端日志）。"""

    _attr_name = "今日门前逗留次数"
    _suffix = "today_loiter_count"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:counter"

    @property
    def native_value(self) -> int:
        return sum(1 for log in self._loiter_logs()
                   if _is_today(log.get("logCreateTime")))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "统计样本条数": len(self._recent_logs),
            "说明": "仅统计最近 50 条云端日志，跨天大量事件时可能少计",
        }


class TPLinkLastLoiterDurationSensor(TPLinkLockEntity):
    """最近一次逗留持续时长（云端 duration，毫秒转秒）。"""

    _attr_name = "最近逗留时长"
    _suffix = "last_loiter_duration"
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    @property
    def native_value(self) -> float | None:
        logs = self._loiter_logs()
        ms = _as_float(logs[0].get("duration")) if logs else None
        return None if ms is None else round(ms / 1000, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._loiter_logs()
        return {
            "原始字段": "duration（毫秒）",
            "抓拍图地址": logs[0].get("snapshotUrl") if logs else None,
        }


class TPLinkLastLoiterVideoSizeSensor(TPLinkLockEntity):
    """最近一次逗留录像文件大小（云端 fileSize，字节转 MB）。"""

    _attr_name = "最近逗留录像大小"
    _suffix = "last_loiter_video_size"
    _attr_native_unit_of_measurement = "MB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:video-outline"

    @property
    def native_value(self) -> float | None:
        logs = self._loiter_logs()
        size = _as_float(logs[0].get("fileSize")) if logs else None
        return None if size is None else round(size / 1024 / 1024, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        logs = self._loiter_logs()
        return {
            "原始大小字节": logs[0].get("fileSize") if logs else None,
            "录像地址": logs[0].get("baseUrl") if logs else None,
            "说明": "录像存于 TP-LINK 云端私有 OSS，需 App 内签名后播放",
        }


# ---------------------------------------------------------------------------
# 影子系统新增模块（audio / network），随四模块 getDeviceConfig 一起返回
# ---------------------------------------------------------------------------
class TPLinkWifiRssiSensor(TPLinkLockEntity):
    """WiFi 信号质量（wireless_status.rssi_val）。"""

    _attr_name = "WiFi 信号质量"
    _suffix = "wifi_rssi"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi"
    _enabled_default = False  # 网络细节，默认隐藏

    @property
    def _wireless(self) -> dict[str, Any]:
        return (self._root.get("network") or {}).get("wireless_status") or {}

    @property
    def native_value(self) -> float | None:
        return _as_float(self._wireless.get("rssi_val"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self._wireless
        return {
            "信号格数": w.get("rssi"),
            "SSID": unquote(str(w.get("ssid") or "")),
            "网络时间戳": w.get("nwk_time"),
            "易展3.0": w.get("mesh_3_0_enable"),
            "AP 异常类型": w.get("ap_invalid_type"),
        }


class TPLinkWifiSsidSensor(TPLinkLockEntity):
    """门锁当前接入的 WiFi 名称（云端 URL 编码，此处解码）。"""

    _attr_name = "WiFi 网络"
    _suffix = "wifi_ssid"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi-settings"
    _enabled_default = False  # 网络细节，默认隐藏

    @property
    def native_value(self) -> str | None:
        ssid = ((self._root.get("network") or {}).get("wireless_status") or {})\
            .get("ssid")
        return unquote(str(ssid)) if ssid else None


class TPLinkRingScheduleSensor(TPLinkLockEntity):
    """室内门铃响铃计划（audio.ring_schedule.schedule）。"""

    _attr_name = "响铃计划"
    _suffix = "ring_schedule"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:clock-outline"
    _enabled_default = False  # 只读信息，默认隐藏

    @property
    def _schedule(self) -> list[Any]:
        value = (self._root.get("audio") or {}).get("ring_schedule") or {}
        return value.get("schedule") or []

    @property
    def native_value(self) -> str:
        items = self._schedule
        if not items:
            return "未设置"
        return "全天" if items == ["0000-0000"] else ", ".join(map(str, items))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"原始计划": self._schedule}
