"""TP-LINK 门锁可写设置项（select 平台）。

字段与取值来自 APK 内 RN 源码 ShadowPromiseNetUtil.js：
  audio.lock_volume_cfg.alarm_volume / notice_volume  0高 1中 2低 3静音
  audio.ring.ring_volume_back                        0静音 1低 2高 3中
  audio.ring.ring_id_back / ring_id_pre              铃声库 ID
  dev_manager.dev_config.face_verify_mode / face_detect_sensitivity / standby_mode
写入统一走 /biz/v1/setDeviceDesiredConfig。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    FACE_DETECT_SENSITIVITY_LABELS,
    FACE_VERIFY_MODE_LABELS,
    STANDBY_MODE_LABELS,
)
from .entity import TPLinkLockBaseEntity, devices_of, entity_common

ALARM_VOLUME_LABELS = {"0": "高", "1": "中", "2": "低", "3": "静音"}
RING_VOLUME_LABELS = {"0": "静音", "1": "低", "2": "高", "3": "中"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities = []
    for dev in devices_of(entry):
        common = entity_common(hass, entry, dev)
        entities.extend([
            TPLinkAlarmVolumeSelect(**common, suffix="set_alarm_volume"),
            TPLinkNoticeVolumeSelect(**common, suffix="set_notice_volume"),
            TPLinkRingVolumeSelect(**common, suffix="set_ring_volume_back"),
            TPLinkIndoorRingSelect(**common, suffix="set_ring_id_back"),
            TPLinkOutdoorRingSelect(**common, suffix="set_ring_id_pre"),
            TPLinkFaceVerifyModeSelect(**common, suffix="set_face_verify_mode"),
            TPLinkFaceSensitivitySelect(**common,
                                        suffix="set_face_detect_sensitivity"),
            TPLinkStandbyModeSelect(**common, suffix="set_standby_mode"),
        ])
    async_add_entities(entities)


class _MappedSelect(TPLinkLockBaseEntity, SelectEntity):
    """固定「云端值 -> 中文标签」映射的通用 select。"""

    _attr_icon = "mdi:cog-outline"
    _module = ""
    _section = ""
    _field = ""
    _labels: dict[str, str] = {}

    @property
    def options(self) -> list[str]:
        return list(self._labels.values())

    def _raw(self) -> str:
        section = (self._root.get(self._module) or {}).get(self._section) or {}
        return str(section.get(self._field) or "")

    @property
    def current_option(self) -> str | None:
        return self._labels.get(self._raw())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"云端原始值": self._raw()}

    async def async_select_option(self, option: str) -> None:
        value = next((k for k, v in self._labels.items() if v == option), None)
        if value is None:
            raise HomeAssistantError("无效选项: %s" % option)
        await self._async_write(
            {self._module: {self._section: {self._field: value}}})


class TPLinkAlarmVolumeSelect(_MappedSelect):
    _attr_name = "报警音量"
    _suffix = "set_alarm_volume"
    _attr_icon = "mdi:volume-high"
    _module, _section, _field = "audio", "lock_volume_cfg", "alarm_volume"
    _labels = ALARM_VOLUME_LABELS


class TPLinkNoticeVolumeSelect(_MappedSelect):
    _attr_name = "开锁提示音音量"
    _suffix = "set_notice_volume"
    _attr_icon = "mdi:volume-medium"
    _module, _section, _field = "audio", "lock_volume_cfg", "notice_volume"
    _labels = ALARM_VOLUME_LABELS


class TPLinkRingVolumeSelect(_MappedSelect):
    _attr_name = "室内响铃音量"
    _suffix = "set_ring_volume_back"
    _attr_icon = "mdi:bell-ring"
    _module, _section, _field = "audio", "ring", "ring_volume_back"
    _labels = RING_VOLUME_LABELS


class TPLinkFaceVerifyModeSelect(_MappedSelect):
    _attr_name = "人脸验证模式"
    _suffix = "set_face_verify_mode"
    _attr_icon = "mdi:face-recognition"
    _module, _section, _field = "dev_manager", "dev_config", "face_verify_mode"
    _labels = FACE_VERIFY_MODE_LABELS


class TPLinkFaceSensitivitySelect(_MappedSelect):
    _attr_name = "人脸检测灵敏度"
    _suffix = "set_face_detect_sensitivity"
    _attr_icon = "mdi:tune"
    _module, _section = "dev_manager", "dev_config"
    _field = "face_detect_sensitivity"
    _labels = FACE_DETECT_SENSITIVITY_LABELS


class TPLinkStandbyModeSelect(_MappedSelect):
    _attr_name = "待机模式"
    _suffix = "set_standby_mode"
    _attr_icon = "mdi:sleep"
    _module, _section, _field = "dev_manager", "dev_config", "standby_mode"
    _labels = STANDBY_MODE_LABELS


class _RingIdSelect(TPLinkLockBaseEntity, SelectEntity):
    """铃声选择：选项来自设备影子 audio_lib 铃声库（动态）。"""

    _attr_icon = "mdi:music-note"
    _lib_key = ""
    _field = ""

    def _ring_list(self) -> list[tuple[str, str]]:
        lib = self._audio_lib.get(self._lib_key) or []
        out: list[tuple[str, str]] = []
        for item in lib if isinstance(lib, list) else []:
            if not isinstance(item, dict):
                continue
            for entry in item.values():
                if isinstance(entry, dict) and entry.get("id"):
                    out.append((str(entry["id"]),
                                unquote(str(entry.get("name")
                                            or entry["id"]))))
        return out

    def _raw(self) -> str:
        return str((self._audio.get("ring") or {}).get(self._field) or "")

    @property
    def options(self) -> list[str]:
        names = [name for _, name in self._ring_list()]
        # 首次加载前铃声库可能为空，给一个占位避免出现「无选项」警告
        return names or [self._raw() or "加载中"]

    @property
    def current_option(self) -> str | None:
        raw = self._raw()
        return next((name for rid, name in self._ring_list() if rid == raw),
                    None)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"铃声ID": self._raw(),
                "铃声库": self._lib_key,
                "可选数量": len(self._ring_list())}

    async def async_select_option(self, option: str) -> None:
        ring_id = next((rid for rid, name in self._ring_list()
                        if name == option), None)
        if ring_id is None:
            raise HomeAssistantError("无效铃声: %s" % option)
        await self._async_write({"audio": {"ring": {self._field: ring_id}}})


class TPLinkIndoorRingSelect(_RingIdSelect):
    _attr_name = "室内铃声"
    _suffix = "set_ring_id_back"
    _lib_key = "audio_lib_dev_back"
    _field = "ring_id_back"


class TPLinkOutdoorRingSelect(_RingIdSelect):
    _attr_name = "室外铃声"
    _suffix = "set_ring_id_pre"
    _lib_key = "audio_lib_dev_pre"
    _field = "ring_id_pre"
