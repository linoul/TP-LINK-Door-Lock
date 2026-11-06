"""TP-LINK 门锁事件平台：开门/关门/门铃/报警/设置变更。"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICES,
    DEFAULT_MODEL,
    DOMAIN,
    LOG_TYPE_DOOR,
    LOG_TYPE_DOORBELL,
    LOG_TYPE_LOCK,
    LOG_TYPE_SETTING,
    TEMPLATE_EVENT_MAP,
    VERIFY_TYPE_LABELS,
)

_LOGGER = logging.getLogger(__name__)


def _devices_of(entry) -> list:
    """条目下的门锁列表（兼容旧的单 device_id 格式）。"""
    devices = entry.data.get(CONF_DEVICES)
    if devices:
        return list(devices)
    return [{"device_id": entry.data["device_id"],
             "device_name": entry.data.get("device_name", "")}]

EVENT_TYPE_LABELS = {
    "unlock": "开门",
    "lock": "上锁",
    "door_alarm": "门异常",
    "doorbell": "门铃",
    "verify_failed": "验证失败",
    "setting_change": "设置变更",
    "loiter": "逗留告警",
    "tamper": "防撬告警",
    "remote_unlock": "远程开锁",
    "low_battery": "低电量",
    "other": "其他",
}

EVENT_TYPES = [
    "unlock",          # 开门/解锁（含人脸/密码/指纹开门成功）
    "lock",            # 关门上锁
    "door_alarm",      # 门异常（门未关/虚掩/未锁好）
    "doorbell",        # 门铃
    "verify_failed",   # 验证失败
    "setting_change",  # 设置变更
    "loiter",          # 逗留 / 有人停留（SL42 Pro 猫眼侦测）
    "tamper",          # 防撬 / 防拆 / 破坏
    "remote_unlock",   # App / 远程开锁
    "low_battery",     # 低电量提醒
    "other",           # 其他
]


def classify_log(log: dict[str, Any]) -> str:
    """结构化 templateId 优先，logTypeId + 内容关键词兜底。

    SL42 Pro 云端日志带 templateId/verifyType，比中文文案稳定；
    未知 templateId 时回退到原有关键词逻辑，保证兼容 SL41 等旧型号。
    """
    template_id = str(log.get("templateId") or "")
    if template_id in TEMPLATE_EVENT_MAP:
        return TEMPLATE_EVENT_MAP[template_id]

    type_id = log.get("logTypeId")
    content = str(log.get("logContent") or "")

    # SL42 Pro 新增告警类型：文案特征比 logTypeId 更明确，先判定，
    # 避免被 logTypeId 的粗分类盖掉。未知类型仍走 other，不臆测。
    if any(k in content for k in ("逗留", "有人停留", "徘徊", "长时间停留")):
        return "loiter"
    if any(k in content for k in ("防撬", "防拆", "撬动", "暴力", "破坏")):
        return "tamper"
    if any(k in content for k in ("低电量", "电量低", "电量不足", "更换电池")):
        return "low_battery"
    if type_id == LOG_TYPE_DOORBELL or "门铃" in content or "呼叫" in content:
        return "doorbell"
    if type_id == LOG_TYPE_DOOR or any(k in content for k in ("门未关", "虚掩", "撬", "防拆")):
        return "door_alarm"
    if type_id == LOG_TYPE_SETTING or any(k in content for k in ("设置", "管理员", "添加", "删除")):
        return "setting_change"
    if "失败" in content:
        return "verify_failed"
    if type_id == LOG_TYPE_LOCK or any(k in content for k in ("上锁", "开锁", "开门", "解锁")):
        if any(k in content for k in ("远程", "APP", "App", "手机开锁")):
            return "remote_unlock"
        # 关门上锁 -> lock；其余含"开" -> unlock
        if "上锁" in content and "开" not in content.replace("开门上锁", ""):
            return "lock"
        if "关门上锁" in content:
            return "lock"
        return "unlock"
    return "other"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    models = data.get("device_models") or {}
    async_add_entities([
        TPLinkLockEventEntity(coordinator, dev["device_id"],
                              dev.get("device_name") or dev["device_id"][-8:],
                              models.get(dev["device_id"]))
        for dev in _devices_of(entry)
    ])


class TPLinkLockEventEntity(EventEntity):
    """门锁事件实体：对应一把门锁的全部日志事件。"""

    _attr_translation_key = "lock_events"
    _attr_has_entity_name = True
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator, device_id: str, device_name: str,
                 model: str | None = None) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="TP-LINK",
            model=model or DEFAULT_MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_unique_id = "%s_events" % device_id
        coordinator.register_log_listener(self._handle_logs)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 重放最近 15 分钟内的最后一条日志，避免事件实体长期"未知"
        last = (self._coordinator.data or {}).get(
            "devices", {}).get(self._device_id, {}).get("last_log")
        if last:
            age = time.time() * 1000 - (last.get("logCreateTime") or 0)
            if 0 <= age < 15 * 60 * 1000:
                self._handle_logs(self._device_id, [last])

    @callback
    def _handle_logs(self, device_id: str, logs: list[dict[str, Any]]) -> None:
        """协调器收到新日志时触发事件（只处理本设备的）。"""
        if device_id != self._device_id:
            return
        for log in logs:
            event_type = classify_log(log)
            _LOGGER.info("门锁事件(%s): [%s] %s", self._device_id[-6:],
                         event_type, log.get("logContent"))
            verify_type = str(log.get("verifyType") or "")
            attrs: dict[str, Any] = {
                "content": log.get("logContent"),
                "logTypeId": log.get("logTypeId"),
                "templateId": log.get("templateId"),
                "verifyType": log.get("verifyType"),
                "verifyTypeLabel": VERIFY_TYPE_LABELS.get(verify_type) or "",
                "logId": log.get("logId"),
            }
            # 逗留/门铃等告警日志带抓拍图与录像元数据，
            # 暴露给自动化做「带图推送」（snapshotUrl 是公网可直取 JPEG）。
            for key in ("snapshotUrl", "baseUrl", "duration", "fileSize"):
                if log.get(key) is not None:
                    attrs[key] = log[key]
            self._trigger_event(event_type, attrs)
        # CoordinatorEntity 之外的普通实体需手动写状态
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # 有过一次成功数据就一直保持可用（失败时显示旧值，而不是变不可用）
        return self._coordinator.data is not None
