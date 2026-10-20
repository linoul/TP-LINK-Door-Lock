"""TP-LINK 门锁数据协调器：轮询状态 + 增量日志 -> 触发事件。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import TPLinkApiError, TPLinkAuthError, TPLinkCloudClient, TPLinkCloudError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class TPLinkLockCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """每 30 秒拉一次门锁状态与最新日志。"""

    def __init__(self, hass: HomeAssistant, client: TPLinkCloudClient,
                 device_name: str, poll_interval: int = DEFAULT_SCAN_INTERVAL) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self.client = client
        self.device_name = device_name
        self._seen_log_ids: set[str] = set()
        self._listeners_for_logs: list = []
        self._tick = 0
        self._base_interval = poll_interval

    async def _async_update_data(self) -> dict[str, Any]:
        self._tick += 1
        tick = self._tick
        # 首次刷新 / 有新日志 / 每 4 个周期：刷新在线状态与电量配置；
        # 其余周期只轮询日志（门锁事件秒级上云，高频日志轮询即近似实时）
        try:
            logs = await self.hass.async_add_executor_job(
                self.client.get_logs, 10)
            new_logs = [log for log in logs
                        if log.get("logId") not in self._seen_log_ids]
            first = self.data is None or not self.data.get("config")
            if first or new_logs or self._tick % 4 == 0:
                online_raw = await self.hass.async_add_executor_job(
                    self.client.get_online_status)
                config = await self.hass.async_add_executor_job(
                    self.client.get_device_config)
                # 接口返回 {"online": 0/1}，此处统一提取成 bool/int
                online = (online_raw or {}).get("online")
            else:
                online = self.data.get("online")
                config = self.data.get("config")
        except TPLinkAuthError as err:
            _LOGGER.warning("tick#%d 认证失败: %s", tick, err)
            self._backoff()
            raise UpdateFailed("认证失败: %s" % err) from err
        except (TPLinkApiError, TPLinkCloudError) as err:
            _LOGGER.warning("tick#%d 轮询失败: %s", tick, err)
            self._backoff()
            raise UpdateFailed(str(err)) from err
        _LOGGER.debug("tick#%d OK logs=%d 新=%d 间隔=%ss", tick, len(logs),
                      len(new_logs), self.update_interval.total_seconds())
        # 成功即恢复正常间隔
        self.update_interval = timedelta(seconds=self._base_interval)

        for log in logs:
            self._seen_log_ids.add(log.get("logId"))
        # 防集合无限增长
        if len(self._seen_log_ids) > 500:
            self._seen_log_ids = set(list(self._seen_log_ids)[-200:])

        if new_logs:
            _LOGGER.debug("新日志 %d 条: %s", len(new_logs),
                          [log.get("logContent") for log in new_logs])

        # get_logs 返回 desc（最新在前），故取 [0] 作为"最近一条"
        last_log = new_logs[0] if new_logs else self.data.get("last_log") if self.data else None
        data = {
            "online": online,
            "config": config,
            "new_logs": new_logs,
            "last_log": last_log,
        }
        if new_logs:
            self._fire_log_events(new_logs)
        return data

    def _backoff(self) -> None:
        """失败时指数退避（最长 5 分钟），成功后恢复，避免打爆云端。"""
        cur = self.update_interval.total_seconds() or self._base_interval
        self.update_interval = timedelta(seconds=min(cur * 2, 300))
        _LOGGER.warning("轮询失败，下次重试间隔调整为 %s 秒",
                        self.update_interval.total_seconds())

    @callback
    def _fire_log_events(self, logs: list[dict[str, Any]]) -> None:
        """把新日志分发给已注册的事件实体。"""
        for listener in list(self._listeners_for_logs):
            try:
                listener(logs)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("事件分发失败")

    @callback
    def dispatch_new_logs(self, logs: list[dict[str, Any]]) -> None:
        """在协调器更新完成后分发新日志。"""
        if logs:
            self._fire_log_events(logs)

    @callback
    def register_log_listener(self, listener) -> None:
        """事件实体注册回调（entity 为 first 时重放最近日志）。"""
        self._listeners_for_logs.append(listener)
