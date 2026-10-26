"""TP-LINK Door Lock 集成入口。"""
from __future__ import annotations

import logging
import traceback

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .api import TPLinkAuthError, TPLinkCloudClient, TPLinkCloudError
from .const import (
    CONF_DEVICES,
    CONF_DEVICE_ID,
    CONF_TERMINAL_UUID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import TPLinkLockCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "event"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _attach_file_logger(hass: HomeAssistant) -> None:
    """仅当 configuration.yaml 开启本集成 debug 时，才写日志文件。"""
    lg = logging.getLogger("custom_components.tplink_lock")
    if not lg.isEnabledFor(logging.DEBUG):
        return
    try:
        path = hass.config.path("tplink_lock_debug.log")
        if not any(isinstance(h, logging.FileHandler) for h in lg.handlers):
            h = logging.FileHandler(path, encoding="utf-8")
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"))
            lg.addHandler(h)
        lg.debug("=== debug file logger attached: %s ===", path)
    except Exception:
        _LOGGER.warning("诊断日志不可写: %s", traceback.format_exc())


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """yaml 配置不支持，仅 config entry。"""
    _attach_file_logger(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """从 config entry 初始化。"""
    _attach_file_logger(hass)
    _LOGGER.debug("async_setup_entry 开始 entry_id=%s data_keys=%s",
                  entry.entry_id, list(entry.data))
    # 旧版本迁移：单 device_id -> devices 列表（多门锁支持）
    if CONF_DEVICES not in entry.data:
        new_data = dict(entry.data)
        new_data[CONF_DEVICES] = [{
            "device_id": entry.data[CONF_DEVICE_ID],
            "device_name": entry.data.get("device_name", ""),
        }]
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.info("已迁移旧条目格式：单设备 -> 多设备列表")
        # 更新后 entry 对象可能失效，重新取
        entry = hass.config_entries.async_get_entry(entry.entry_id) or entry
    try:
        return await _async_setup_entry(hass, entry)
    except ConfigEntryNotReady as err:
        _LOGGER.warning("ConfigEntryNotReady: %s\n%s", err, traceback.format_exc())
        raise
    except Exception:
        _LOGGER.error("async_setup_entry 失败:\n%s", traceback.format_exc())
        raise


async def _async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    devices = entry.data[CONF_DEVICES]
    client = TPLinkCloudClient(
        username=entry.data["username"],
        password=entry.data["password"],
        terminal_uuid=entry.data[CONF_TERMINAL_UUID],
        device_id=devices[0]["device_id"],
    )

    # 启动时先登录一次，尽早暴露配置错误
    try:
        await hass.async_add_executor_job(client.login)
        _LOGGER.debug("启动登录成功")
    except TPLinkAuthError as err:
        raise ConfigEntryNotReady("TP-LINK 登录失败: %s" % err) from err
    except TPLinkCloudError as err:
        _LOGGER.warning("TP-LINK 云端暂不可达（将自动重试）: %s", err)

    poll_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
    coordinator = TPLinkLockCoordinator(hass, client, devices, poll_interval)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("首次刷新完成, 设备=%s",
                  list(coordinator.data.get("devices", {}))
                  if coordinator.data else None)

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug("平台加载完成")

    async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """选项变化时重载，使新的轮询间隔生效。"""
        await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载。"""
    _LOGGER.debug("async_unload_entry")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
