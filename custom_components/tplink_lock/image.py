"""TP-LINK 门锁抓拍图 image 平台。

云端告警日志（逗留 / 门铃等）自带 snapshotUrl，是公网可直取的 JPEG，
签名约 1 小时有效，且每次轮询日志都会重新签发，因此这里由 HA 后端
按需代理下载，不依赖浏览器跨域，也不把临时签名写进实体状态。

注意：baseUrl 指向的录像文件在私有 OSS（403），本实体不尝试播放视频。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICES,
    DEFAULT_MODEL,
    DOMAIN,
    SNAPSHOT_FETCH_TIMEOUT,
)

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
    entities = []
    for dev in _devices_of(entry):
        dev_id = dev["device_id"]
        entities.append(TPLinkSnapshotImage(
            hass, coordinator, dev_id,
            dev.get("device_name") or dev_id[-8:],
            models.get(dev_id), device_info.get(dev_id) or {},
        ))
    _LOGGER.debug("image 平台创建 %d 个实体", len(entities))
    async_add_entities(entities)


class TPLinkSnapshotImage(ImageEntity):
    """最近一次门前抓拍（逗留 / 门铃等带图告警）。"""

    _attr_has_entity_name = True
    _attr_name = "最近门前抓拍"
    _attr_content_type = "image/jpeg"
    _attr_icon = "mdi:image-outline"

    def __init__(self, hass: HomeAssistant, coordinator, device_id: str,
                 device_name: str, model: str | None = None,
                 device_info: dict[str, Any] | None = None) -> None:
        super().__init__(hass)
        self.hass = hass
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_unique_id = "%s_last_snapshot" % device_id
        info = device_info or {}
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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update))

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._coordinator.data is not None

    @property
    def _dev_data(self) -> dict[str, Any]:
        return (self._coordinator.data or {}).get("devices", {}).get(
            self._device_id, {})

    @property
    def _snapshot_log(self) -> dict[str, Any] | None:
        """最近一条带抓拍图的告警日志（recent_logs 已是时间倒序）。"""
        for log in self._dev_data.get("recent_logs") or []:
            if log.get("snapshotUrl"):
                return log
        return None

    @property
    def image_last_updated(self) -> datetime | None:
        """用抓拍事件时间做缓存键：同一事件不重复下载，新事件自动刷新。"""
        log = self._snapshot_log
        ts = (log or {}).get("logCreateTime")
        try:
            return datetime.fromtimestamp(float(ts) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    async def async_image(self) -> bytes | None:
        url = (self._snapshot_log or {}).get("snapshotUrl")
        if not url:
            return None
        return await self.hass.async_add_executor_job(self._fetch, url)

    @staticmethod
    def _fetch(url: str) -> bytes | None:
        """同步下载图片；部分节点证书链不完整时降级一次。"""
        for verify in (True, False):
            try:
                resp = requests.get(url, timeout=SNAPSHOT_FETCH_TIMEOUT,
                                    verify=verify)
                if resp.status_code == 200 and resp.content[:2] == b"\xff\xd8":
                    return resp.content
                _LOGGER.debug("抓拍图返回 %s / %s", resp.status_code,
                              resp.headers.get("Content-Type"))
            except requests.RequestException as err:
                _LOGGER.debug("抓拍图下载失败(verify=%s): %s", verify, err)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        log = self._snapshot_log or {}
        if not log:
            return {"说明": "暂无带抓拍图的告警（逗留 / 门铃等）"}
        return {
            "事件内容": log.get("logContent"),
            "抓拍图地址": log.get("snapshotUrl"),
            "录像地址": log.get("baseUrl"),
            "录像地址说明": "私有 OSS，需云端签名，HA 内不可直接播放",
            "逗留时长毫秒": log.get("duration"),
            "录像大小字节": log.get("fileSize"),
            "logId": log.get("logId"),
            "说明": "图片由 HA 后端代理下载，签名过期前每次轮询会自动更新",
        }
