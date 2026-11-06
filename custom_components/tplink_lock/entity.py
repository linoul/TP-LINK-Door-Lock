"""TP-LINK 门锁实体公共基类（可写平台 select / number / switch 共用）。

刻意与 sensor.py 的 TPLinkLockEntity 保持独立，避免改动现有只读实体
的继承链；设备信息、unique_id、配置读取路径与写接口封装集中在这里。
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICES, DEFAULT_MODEL, DOMAIN


def devices_of(entry: ConfigEntry) -> list[dict[str, Any]]:
    """条目下的门锁列表（兼容旧的单 device_id 格式）。"""
    devices = entry.data.get(CONF_DEVICES)
    if devices:
        return list(devices)
    return [{"device_id": entry.data["device_id"],
             "device_name": entry.data.get("device_name", "")}]


def entity_common(hass, entry: ConfigEntry,
                  dev: dict[str, Any]) -> dict[str, Any]:
    """构造各平台传给实体构造函数的公共关键字参数。"""
    data = hass.data[DOMAIN][entry.entry_id]
    dev_id = dev["device_id"]
    return {
        "coordinator": data["coordinator"],
        "device_id": dev_id,
        "device_name": dev.get("device_name") or dev_id[-8:],
        "model": (data.get("device_models") or {}).get(dev_id),
        "device_info": (data.get("device_info") or {}).get(dev_id) or {},
    }


class TPLinkLockBaseEntity(CoordinatorEntity):
    """可写实体的公共基类。"""

    _attr_has_entity_name = True
    _suffix = "unknown"

    def __init__(self, coordinator, device_id: str, device_name: str,
                 suffix: str = "", model: str | None = None,
                 device_info: dict[str, Any] | None = None) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._device_info = device_info or {}
        self._attr_unique_id = "%s_%s" % (device_id, suffix or self._suffix)
        info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=device_name,
            manufacturer="TP-LINK",
            model=model or DEFAULT_MODEL,
            entry_type=DeviceEntryType.SERVICE,
        )
        sw = self._device_info.get("fwVer")
        if sw:
            info["sw_version"] = str(sw)
        hw = (self._device_info.get("hwVer")
              or self._device_info.get("deviceHwVer"))
        if hw:
            info["hw_version"] = str(hw)
        mac = self._device_info.get("deviceMac") or self._device_info.get("mac")
        if mac:
            info["serial_number"] = str(mac)
        self._attr_device_info = info

    # ------------------------------------------------------------ 数据访问
    @property
    def _dev_data(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("devices", {}).get(
            self._device_id, {})

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def _root(self) -> dict[str, Any]:
        """getDeviceConfig 返回 result.config，即模块根节点。"""
        return ((self._dev_data.get("config") or {}).get("config") or {})

    @property
    def _dev_config(self) -> dict[str, Any]:
        return (self._root.get("dev_manager") or {}).get("dev_config") or {}

    @property
    def _audio(self) -> dict[str, Any]:
        return self._root.get("audio") or {}

    @property
    def _network(self) -> dict[str, Any]:
        return self._root.get("network") or {}

    @property
    def _audio_lib(self) -> dict[str, Any]:
        return self._root.get("audio_lib") or {}

    # ------------------------------------------------------------ 写入
    async def _async_write(self, config: dict[str, Any]) -> None:
        """下发影子配置并刷新；异常转成 HA 前端可见的错误。"""
        try:
            await self.coordinator.async_set_device_config(
                self._device_id, config)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError("写入门锁设置失败: %s" % err) from err
