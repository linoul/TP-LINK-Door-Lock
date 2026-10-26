"""TP-LINK 门锁数据协调器：账号级轮询所有门锁 -> 按设备分发事件。"""
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
    """账号级协调器：串行轮询账号下所有门锁的状态与日志。

    data 结构::

        {
          "devices": {device_id: {"online", "config", "last_log",
                                  "device_name"}},
          "new_logs": {device_id: [log, ...]},   # 本轮新日志（事件分发用）
        }

    多设备共享同一个已登录 client（同终端 UUID，各自 login 会互踢），
    串行切换 client.device_id 后调用；HA 的 update 是顺序 await，无并发问题。
    """

    def __init__(self, hass: HomeAssistant, client: TPLinkCloudClient,
                 devices: list[dict[str, Any]],
                 poll_interval: int = DEFAULT_SCAN_INTERVAL) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self.client = client
        self.devices = devices
        self._seen_log_ids: dict[str, set] = {}
        self._listeners_for_logs: list = []
        self._tick = 0
        self._base_interval = poll_interval

    async def _async_update_data(self) -> dict[str, Any]:
        self._tick += 1
        tick = self._tick
        first = self.data is None
        devices_data: dict[str, Any] = {}
        new_logs_map: dict[str, list] = {}

        # 首次刷新 / 有新日志 / 每 4 个周期：刷新在线状态与电量配置；
        # 其余周期只轮询日志（门锁事件秒级上云，高频日志轮询即近似实时）
        try:
            for dev in self.devices:
                device_id = dev["device_id"]
                self.client.device_id = device_id
                logs = await self.hass.async_add_executor_job(
                    self.client.get_logs, 10)
                seen = self._seen_log_ids.setdefault(device_id, set())
                new_logs = [log for log in logs
                            if log.get("logId") not in seen]
                for log in logs:
                    seen.add(log.get("logId"))

                prev = (self.data or {}).get("devices", {}).get(device_id, {})
                if first or new_logs or tick % 4 == 0 or not prev.get("config"):
                    online_raw = await self.hass.async_add_executor_job(
                        self.client.get_online_status)
                    config = await self.hass.async_add_executor_job(
                        self.client.get_device_config)
                    online = (online_raw or {}).get("online")
                else:
                    online = prev.get("online")
                    config = prev.get("config")

                # get_logs 返回 desc（最新在前），故取 [0] 作为"最近一条"
                last_log = new_logs[0] if new_logs else prev.get("last_log")
                devices_data[device_id] = {
                    "online": online,
                    "config": config,
                    "last_log": last_log,
                    "device_name": dev.get("device_name") or device_id[-8:],
                }
                if new_logs:
                    new_logs_map[device_id] = new_logs
                    _LOGGER.debug("设备 %s 新日志 %d 条: %s", device_id[-6:],
                                  len(new_logs),
                                  [l.get("logContent") for l in new_logs])
        except TPLinkAuthError as err:
            _LOGGER.warning("tick#%d 认证失败: %s", tick, err)
            self._backoff()
            raise UpdateFailed("认证失败: %s" % err) from err
        except (TPLinkApiError, TPLinkCloudError) as err:
            _LOGGER.warning("tick#%d 轮询失败: %s", tick, err)
            self._backoff()
            raise UpdateFailed(str(err)) from err
        _LOGGER.debug("tick#%d OK 设备=%d 新日志设备=%d 间隔=%ss", tick,
                      len(devices_data), len(new_logs_map),
                      self.update_interval.total_seconds())
        # 成功即恢复正常间隔
        self.update_interval = timedelta(seconds=self._base_interval)

        # 防 seen 集合无限增长（按设备各自裁剪）
        total = sum(len(s) for s in self._seen_log_ids.values())
        if total > 500:
            for did in list(self._seen_log_ids):
                self._seen_log_ids[did] = set(
                    list(self._seen_log_ids[did])[-200:])

        # 按设备分发事件
        for device_id, logs in new_logs_map.items():
            self._fire_log_events(device_id, logs)

        return {"devices": devices_data, "new_logs": new_logs_map}

    def _backoff(self) -> None:
        """失败时指数退避（最长 5 分钟），成功后恢复，避免打爆云端。"""
        cur = self.update_interval.total_seconds() or self._base_interval
        self.update_interval = timedelta(seconds=min(cur * 2, 300))
        _LOGGER.warning("轮询失败，下次重试间隔调整为 %s 秒",
                        self.update_interval.total_seconds())

    @callback
    def _fire_log_events(self, device_id: str,
                         logs: list[dict[str, Any]]) -> None:
        """把某设备的新日志分发给注册的事件实体（带 device_id）。"""
        for listener in list(self._listeners_for_logs):
            try:
                listener(device_id, logs)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("事件分发失败")

    @callback
    def register_log_listener(self, listener) -> None:
        """事件实体注册回调。

        listener 签名：listener(device_id, logs)。
        """
        self._listeners_for_logs.append(listener)
