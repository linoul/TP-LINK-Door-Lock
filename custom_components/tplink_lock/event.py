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
    LOG_TYPE_DOOR,
    LOG_TYPE_DOORBELL,
    LOG_TYPE_LOCK,
    LOG_TYPE_SETTING,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

EVENT_TYPE_LABELS = {
    "unlock": "开门",
    "lock": "上锁",
    "door_alarm": "门异常",
    "doorbell": "门铃",
    "verify_failed": "验证失败",
    "setting_change": "设置变更",
    "other": "其他",
}

EVENT_TYPES = [
    "unlock",          # 开门/解锁（含人脸/密码/指纹开门成功）
    "lock",            # 关门上锁
    "door_alarm",      # 门异常（门未关/虚掩/撬动）
    "doorbell",        # 门铃
    "verify_failed",   # 验证失败
    "setting_change",  # 设置变更
    "other",           # 其他
]


def classify_log(log: dict[str, Any]) -> str:
    """按 logTypeId + 内容关键词归类。"""
    type_id = log.get("logTypeId")
    content = str(log.get("logContent") or "")
    if type_id == LOG_TYPE_DOORBELL or "门铃" in content:
        return "doorbell"
    if type_id == LOG_TYPE_DOOR or any(k in content for k in ("门未关", "虚掩", "撬", "防拆")):
        return "door_alarm"
    if type_id == LOG_TYPE_SETTING or any(k in content for k in ("设置", "管理员", "添加", "删除")):
        return "setting_change"
    if "失败" in content:
        return "verify_failed"
    if type_id == LOG_TYPE_LOCK or any(k in content for k in ("上锁", "开锁", "开门", "解锁")):
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
    async_add_entities([TPLinkLockEventEntity(coordinator, entry)])


class TPLinkLockEventEntity(EventEntity):
    """门锁事件实体：所有门锁日志事件集中在一个实体。"""

    _attr_translation_key = "lock_events"
    _attr_has_entity_name = True
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self._coordinator = coordinator
        self._entry = entry
        device_id = entry.data["device_id"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=coordinator.device_name,
            manufacturer="TP-LINK",
            model="TL-SL41",
            serial_number=device_id,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_unique_id = "%s_events" % device_id
        coordinator.register_log_listener(self._handle_logs)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 重放最近 15 分钟内的最后一条日志，避免事件实体长期"未知"
        last = (self._coordinator.data or {}).get("last_log")
        if last:
            age = time.time() * 1000 - (last.get("logCreateTime") or 0)
            if 0 <= age < 15 * 60 * 1000:
                self._handle_logs([last])

    @callback
    def _handle_logs(self, logs: list[dict[str, Any]]) -> None:
        """协调器收到新日志时触发事件。"""
        for log in logs:
            event_type = classify_log(log)
            _LOGGER.info("门锁事件: [%s] %s", event_type,
                         log.get("logContent"))
            self._trigger_event(
                event_type,
                {
                    "content": log.get("logContent"),
                    "logTypeId": log.get("logTypeId"),
                    "logId": log.get("logId"),
                },
            )
        # CoordinatorEntity 之外的普通实体需手动写状态
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # 有过一次成功数据就一直保持可用（失败时显示旧值，而不是变不可用）
        return self._coordinator.data is not None
