"""TP-LINK Door Lock 集成入口。"""
from __future__ import annotations

import logging
import traceback
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .api import TPLinkAuthError, TPLinkCloudClient, TPLinkCloudError
from .const import (
    ATTR_DATE,
    ATTR_END_TIME,
    ATTR_LAST_LOG_ID,
    ATTR_LIMIT,
    ATTR_START,
    CONF_DEVICE_ID,
    CONF_TERMINAL_UUID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_QUERY_LOGS,
)
from .coordinator import TPLinkLockCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "event"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

QUERY_LOGS_SCHEMA = vol.Schema({
    vol.Required(CONF_DEVICE_ID): cv.string,
    vol.Optional(ATTR_DATE): cv.string,
    vol.Optional(ATTR_END_TIME): vol.Coerce(int),
    vol.Optional(ATTR_LAST_LOG_ID): cv.string,
    vol.Optional(ATTR_LIMIT, default=50): vol.All(
        vol.Coerce(int), vol.Range(min=1, max=100)),
    vol.Optional(ATTR_START, default=-1): vol.Coerce(int),
})


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
    if not hass.services.has_service(DOMAIN, SERVICE_QUERY_LOGS):
        async def async_query_logs(call: ServiceCall) -> dict:
            return await _async_handle_query_logs(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_QUERY_LOGS,
            async_query_logs,
            schema=QUERY_LOGS_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    return True


async def _async_handle_query_logs(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    """查询一页设备日志，并返回下一页所需的游标。"""
    device_id = call.data[CONF_DEVICE_ID]
    client = None
    for entry_data in hass.data.get(DOMAIN, {}).values():
        candidate = entry_data.get("client")
        if candidate and candidate.device_id == device_id:
            client = candidate
            break
    if client is None:
        raise HomeAssistantError("未找到已配置的 TP-LINK 门锁设备")

    end_time = call.data.get(ATTR_END_TIME)
    date_text = call.data.get(ATTR_DATE)
    if date_text:
        if end_time is not None:
            raise HomeAssistantError("date 与 end_time 不能同时使用")
        try:
            selected_date = datetime.strptime(date_text, "%Y-%m-%d").date()
            next_day = selected_date + timedelta(days=1)
            end_time = int(datetime.combine(
                next_day, time.min, ZoneInfo(hass.config.time_zone)
            ).timestamp() * 1000)
        except ValueError as err:
            raise HomeAssistantError("date 必须为 YYYY-MM-DD") from err

    logs = await hass.async_add_executor_job(
        client.get_log_page,
        call.data[ATTR_LIMIT],
        call.data[ATTR_START],
        end_time,
        call.data.get(ATTR_LAST_LOG_ID),
    )
    next_page: dict[str, Any] | None = None
    if logs:
        last_log = logs[-1]
        start = last_log.get("logCreateTime")
        last_log_id = last_log.get("logId")
        if start is not None and last_log_id:
            next_page = {
                ATTR_START: start,
                ATTR_LAST_LOG_ID: last_log_id,
            }
    timezone = ZoneInfo(hass.config.time_zone)
    formatted_logs: list[dict[str, Any]] = []
    for log in logs:
        formatted_log = dict(log)
        timestamp = formatted_log.get("logCreateTime")
        if timestamp is not None:
            try:
                formatted_log["logCreateTimeFormatted"] = datetime.fromtimestamp(
                    int(timestamp) / 1000, timezone
                ).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OSError):
                formatted_log["logCreateTimeFormatted"] = None
        formatted_logs.append(formatted_log)
    return {
        "count": len(formatted_logs),
        "logs": formatted_logs,
        "next_page": next_page,
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """从 config entry 初始化。"""
    _attach_file_logger(hass)
    _LOGGER.debug("async_setup_entry 开始 entry_id=%s data_keys=%s",
                  entry.entry_id, list(entry.data))
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

    client = TPLinkCloudClient(
        username=entry.data["username"],
        password=entry.data["password"],
        terminal_uuid=entry.data[CONF_TERMINAL_UUID],
        device_id=entry.data[CONF_DEVICE_ID],
    )

    # 启动时先登录一次，尽早暴露配置错误
    try:
        await hass.async_add_executor_job(client.login)
        _LOGGER.debug("启动登录成功")
    except TPLinkAuthError as err:
        raise ConfigEntryNotReady("TP-LINK 登录失败: %s" % err) from err
    except TPLinkCloudError as err:
        _LOGGER.warning("TP-LINK 云端暂不可达（将自动重试）: %s", err)

    device_name = entry.data.get("device_name") or entry.data[CONF_DEVICE_ID][-8:]
    poll_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
    coordinator = TPLinkLockCoordinator(hass, client, device_name, poll_interval)
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("首次刷新完成, data keys=%s",
                  list(coordinator.data) if coordinator.data else None)

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
