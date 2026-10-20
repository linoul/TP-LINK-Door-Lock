"""TP-LINK 门锁配置流：第一步登录，第二步从设备列表中选择门锁。"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .api import TPLinkAuthError, TPLinkCloudClient, TPLinkCloudError
from .const import CONF_DEVICE_ID, CONF_TERMINAL_UUID, DOMAIN

_LOGGER = logging.getLogger(__name__)

LOCK_DEVICE_TYPE = "SMARTHOME.SMARTLOCK"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("username"): str,
        vol.Required("password"): str,
    }
)


def _lock_label(lock: dict[str, Any]) -> str:
    """下拉框显示名：别名优先，重复时加 MAC 尾缀区分。"""
    return str(lock.get("alias") or lock.get("deviceName") or lock["deviceId"])


class TPLinkLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理配置流。"""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._terminal_uuid: str = ""
        self._locks: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第一步：账号密码登录，列出账号下的门锁。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input["username"].strip()
            password = user_input["password"]
            # 按账号派生的固定终端 UUID：同一账号反复尝试向导/添加多把锁
            # 都复用同一终端，避免触发 -23027 终端数量上限（App 端互踢仅限同终端）
            terminal_uuid = str(
                uuid.uuid5(uuid.NAMESPACE_URL,
                           "%s:%s" % (DOMAIN, username.lower())))
            client = TPLinkCloudClient(
                username=username,
                password=password,
                terminal_uuid=terminal_uuid,
            )
            try:
                await self.hass.async_add_executor_job(client.login)
                devices = await self.hass.async_add_executor_job(
                    client.get_device_list)
            except TPLinkAuthError as err:
                if "上限" in str(err):
                    errors["base"] = "terminal_limit"
                else:
                    errors["base"] = "invalid_auth"
                _LOGGER.warning("登录验证失败: %s", err)
            except TPLinkCloudError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("连接失败: %s", err)
            else:
                locks = [
                    d for d in devices
                    if d.get("deviceType") == LOCK_DEVICE_TYPE
                ]
                if not locks:
                    errors["base"] = "no_locks"
                    _LOGGER.info("账号 %s 下未发现门锁, 共 %d 台设备: %s",
                                 username, len(devices),
                                 [d.get("deviceType") for d in devices])
                else:
                    _LOGGER.info("账号 %s 下发现 %d 台门锁: %s",
                                 username, len(locks),
                                 [(d.get("alias"), d["deviceId"]) for d in locks])
                    self._username = username
                    self._password = password
                    self._terminal_uuid = terminal_uuid
                    self._locks = locks
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """第二步：从账号下的门锁列表中选择。"""
        # 下拉框：value = deviceId, label = 别名；别名重复时加尾缀
        options: dict[str, str] = {}
        seen: dict[str, int] = {}
        for lock in self._locks:
            label = _lock_label(lock)
            seen[label] = seen.get(label, 0) + 1
        used: dict[str, int] = {}
        for lock in self._locks:
            label = _lock_label(lock)
            if seen[label] > 1:
                n = used.get(label, 0) + 1
                used[label] = n
                label = "%s（%s）" % (label, lock["deviceId"][-6:])
            options[lock["deviceId"]] = label

        if user_input is not None:
            device_id = user_input["device_id"]
            lock = next(d for d in self._locks if d["deviceId"] == device_id)
            await self.async_set_unique_id("%s_%s" % (DOMAIN, device_id))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input.get("device_name")
                or _lock_label(lock)
                or "TP-LINK 门锁",
                data={
                    "username": self._username,
                    "password": self._password,
                    CONF_DEVICE_ID: device_id,
                    "device_name": user_input.get("device_name")
                    or _lock_label(lock),
                    CONF_TERMINAL_UUID: self._terminal_uuid,
                },
            )

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): vol.In(options),
                    vol.Optional("device_name"): str,
                }
            ),
            description_placeholders={
                "locks": "\n".join(
                    "- %s（%s…%s）" % (_lock_label(d), d["deviceId"][:8],
                                      d["deviceId"][-4:])
                    for d in self._locks
                ),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TPLinkLockOptionsFlow()


class TPLinkLockOptionsFlow(config_entries.OptionsFlow):
    """选项流：修改扫描间隔。"""

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional("scan_interval", default=30): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=600)),
            }),
        )
