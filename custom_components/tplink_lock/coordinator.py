"""TP-LINK 门锁数据协调器：轮询状态 + 增量日志 -> 触发事件。"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import TPLinkApiError, TPLinkAuthError, TPLinkCloudClient, TPLinkCloudError
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .event import classify_log

_LOGGER = logging.getLogger(__name__)

TODAY_LOG_PAGE_SIZE = 50
TODAY_LOG_MAX_PAGES = 5
TODAY_RECORDS_ATTRIBUTE_LIMIT = 20
TODAY_REFRESH_RETRY_DELAY = timedelta(minutes=5)


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
        self._time_zone = ZoneInfo(hass.config.time_zone)
        self._today_date: date | None = None
        self._today_open_count = 0
        self._today_open_records: list[dict[str, Any]] = []
        self._today_open_log_ids: set[str] = set()
        self._today_records_refreshed_at: str | None = None
        self._next_today_refresh_at: datetime | None = None
        self._unsub_midnight = async_track_time_change(
            hass, self._handle_midnight, hour=0, minute=0, second=0
        )

    async def _async_update_data(self) -> dict[str, Any]:
        self._tick += 1
        tick = self._tick
        # 首次刷新 / 有新日志 / 每 4 个周期：刷新在线状态与电量配置；
        # 其余周期只轮询日志（门锁事件秒级上云，高频日志轮询即近似实时）
        try:
            logs = await self.hass.async_add_executor_job(
                self.client.get_logs, 10)
            current_date = self._now().date()
            if self._should_refresh_today_records(current_date):
                try:
                    await self.async_refresh_today_open_records()
                except (TPLinkApiError, TPLinkAuthError, TPLinkCloudError) as err:
                    self._record_today_refresh_failure(err)
            new_logs = [log for log in logs
                        if log.get("logId") not in self._seen_log_ids]
            self._merge_new_today_open_records(new_logs, current_date)
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
            "today_open_count": self._today_open_count,
            "today_open_records": self._today_open_records,
            "today_records_refreshed_at": self._today_records_refreshed_at,
        }
        if new_logs:
            self._fire_log_events(new_logs)
        return data

    async def async_refresh_today_open_records(self) -> None:
        """从云端重新建立当天开门记录基线。"""
        current_date = self._now().date()
        count, records, log_ids = await self.hass.async_add_executor_job(
            self._get_today_open_summary, current_date)
        self._today_date = current_date
        self._today_open_count = count
        self._today_open_records = records
        self._today_open_log_ids = log_ids
        self._today_records_refreshed_at = self._now().strftime(
            "%Y-%m-%d %H:%M:%S")
        self._next_today_refresh_at = None
        self._publish_today_open_records()

    def _get_today_open_summary(
        self, current_date: date
    ) -> tuple[int, list[dict[str, Any]], set[str]]:
        """读取当天日志页，直到到达前一天或达到安全上限。"""
        start_of_day = int(datetime.combine(
            current_date, time.min, self._time_zone).timestamp() * 1000)
        count = 0
        records: list[dict[str, Any]] = []
        log_ids: set[str] = set()
        start = -1
        last_log_id: str | None = None

        for _ in range(TODAY_LOG_MAX_PAGES):
            logs = self.client.get_log_page(
                limit=TODAY_LOG_PAGE_SIZE,
                start=start,
                end_time=-1,
                last_log_id=last_log_id,
            )
            if not logs:
                break

            reached_previous_day = False
            for log in logs:
                timestamp = self._log_timestamp(log)
                if timestamp is None or timestamp < start_of_day:
                    reached_previous_day = True
                    break
                if self._is_today_open_log(log, current_date):
                    log_id = self._log_id(log)
                    if log_id in log_ids:
                        continue
                    log_ids.add(log_id)
                    count += 1
                    if len(records) < TODAY_RECORDS_ATTRIBUTE_LIMIT:
                        records.append(log)

            if reached_previous_day or len(logs) < TODAY_LOG_PAGE_SIZE:
                break
            last_log = logs[-1]
            timestamp = self._log_timestamp(last_log)
            log_id = last_log.get("logId")
            if timestamp is None or not log_id:
                break
            start = timestamp
            last_log_id = str(log_id)
        else:
            _LOGGER.warning(
                "当天日志达到 %d 页查询上限，今日开门次数可能不完整",
                TODAY_LOG_MAX_PAGES)

        records.sort(key=lambda log: self._log_timestamp(log) or 0, reverse=True)
        return count, records, log_ids

    def _merge_new_today_open_records(
        self, logs: list[dict[str, Any]], current_date: date
    ) -> None:
        """将常规轮询中新出现的当天开门日志合并到缓存。"""
        if self._today_date != current_date:
            return
        changed = False
        for log in logs:
            if not self._is_today_open_log(log, current_date):
                continue
            log_id = self._log_id(log)
            if log_id in self._today_open_log_ids:
                continue
            self._today_open_log_ids.add(log_id)
            self._today_open_count += 1
            self._today_open_records.append(log)
            changed = True
        if changed:
            self._today_open_records.sort(
                key=lambda log: self._log_timestamp(log) or 0, reverse=True)
            self._today_open_records = self._today_open_records[
                :TODAY_RECORDS_ATTRIBUTE_LIMIT]

    def _is_today_open_log(self, log: dict[str, Any], current_date: date) -> bool:
        timestamp = self._log_timestamp(log)
        return (
            timestamp is not None
            and datetime.fromtimestamp(timestamp / 1000, self._time_zone).date()
            == current_date
            and classify_log(log) == "unlock"
        )

    def format_log_time(self, log: dict[str, Any]) -> str | None:
        """按 Home Assistant 配置时区格式化日志时间。"""
        timestamp = self._log_timestamp(log)
        if timestamp is None:
            return None
        try:
            return datetime.fromtimestamp(
                timestamp / 1000, self._time_zone
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return None

    @staticmethod
    def _log_timestamp(log: dict[str, Any]) -> int | None:
        try:
            return int(log.get("logCreateTime"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _log_id(log: dict[str, Any]) -> str:
        log_id = log.get("logId")
        if log_id:
            return str(log_id)
        return "%s:%s" % (log.get("logCreateTime"), log.get("logContent"))

    def _now(self) -> datetime:
        return datetime.now(self._time_zone)

    def _should_refresh_today_records(self, current_date: date) -> bool:
        """判断当天基线是否尚未建立，或失败后的重试时间是否已到。"""
        return (
            self._today_date != current_date
            and (
                self._next_today_refresh_at is None
                or self._now() >= self._next_today_refresh_at
            )
        )

    def _record_today_refresh_failure(self, err: Exception) -> None:
        """今日统计失败不能影响门锁主轮询，稍后再尝试建立基线。"""
        self._next_today_refresh_at = self._now() + TODAY_REFRESH_RETRY_DELAY
        _LOGGER.warning(
            "刷新今日开门记录失败，将在 %s 后重试: %s",
            self._next_today_refresh_at.strftime("%H:%M:%S"),
            err,
        )

    def _publish_today_open_records(self) -> None:
        if self.data is None:
            return
        data = dict(self.data)
        data.update({
            "today_open_count": self._today_open_count,
            "today_open_records": self._today_open_records,
            "today_records_refreshed_at": self._today_records_refreshed_at,
        })
        self.async_set_updated_data(data)

    @callback
    def _handle_midnight(self, now: datetime) -> None:
        """零点先清空显示，再异步从云端建立新一天的基线。"""
        self._today_date = now.astimezone(self._time_zone).date()
        self._today_open_count = 0
        self._today_open_records = []
        self._today_open_log_ids = set()
        self._today_records_refreshed_at = now.astimezone(
            self._time_zone).strftime("%Y-%m-%d %H:%M:%S")
        self._next_today_refresh_at = None
        self._publish_today_open_records()
        self.hass.async_create_task(self._async_refresh_today_open_records_safe())

    async def _async_refresh_today_open_records_safe(self) -> None:
        try:
            await self.async_refresh_today_open_records()
        except (TPLinkApiError, TPLinkAuthError, TPLinkCloudError) as err:
            self._record_today_refresh_failure(err)

    def shutdown(self) -> None:
        """取消零点定时器。"""
        self._unsub_midnight()

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
