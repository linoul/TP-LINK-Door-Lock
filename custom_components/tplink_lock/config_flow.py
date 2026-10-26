"""TP-LINK 门锁配置流：登录 -> （可选）短信验证 -> 选择门锁。"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .api import (
    TPLinkAuthError,
    TPLinkCloudClient,
    TPLinkCloudError,
    TPLinkNeedSmsError,
    TPLinkSmsCodeError,
)
from .const import (
    CONF_DEVICES,
    CONF_DEVICE_ID,
    CONF_TERMINAL_UUID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

LOCK_DEVICE_TYPE = "SMARTHOME.SMARTLOCK"
SMS_RESEND_COOLDOWN = 60  # 秒

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required("username"): str,
        vol.Required("password"): str,
    }
)


def _terminal_uuid_for(username: str) -> str:
    """按账号派生的固定终端 UUID：同一账号反复尝试向导/添加多把锁
    都复用同一终端，避免触发 -23027 终端数量上限。"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          "%s:%s" % (DOMAIN, username.lower())))


def _lock_label(lock: dict[str, Any]) -> str:
    """下拉框显示名：别名优先。"""
    return str(lock.get("alias") or lock.get("deviceName") or lock["deviceId"])


def _devices_of_entry(entry) -> list[dict[str, Any]]:
    """条目已配置的门锁列表（兼容旧的单 device_id 格式）。"""
    devices = entry.data.get(CONF_DEVICES)
    if devices:
        return list(devices)
    return [{"device_id": entry.data["device_id"],
             "device_name": entry.data.get("device_name", "")}]


def _other_entry_device_ids(hass, entry) -> set[str]:
    """本域下其它条目已经占用的门锁，防止同一把锁被两个条目重复轮询。"""
    used: set[str] = set()
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        used.update(d["device_id"] for d in _devices_of_entry(other))
    return used


def _title_for(devices: list[dict[str, Any]]) -> str:
    """多锁条目标题：第一把锁名 + 等 N 把。"""
    first = devices[0].get("device_name") or devices[0]["device_id"][-8:]
    if len(devices) > 1:
        return "%s 等 %d 把" % (first, len(devices))
    return first


class TPLinkLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理配置流。"""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._terminal_uuid: str = ""
        self._locks: list[dict[str, Any]] = []
        self._sms_sent_at: float = 0.0

    # ------------------------------------------------------------ 第一步：登录
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """账号密码登录，列出账号下的门锁。"""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input["username"].strip()
            password = user_input["password"]
            terminal_uuid = _terminal_uuid_for(username)
            client = TPLinkCloudClient(
                username=username,
                password=password,
                terminal_uuid=terminal_uuid,
            )
            try:
                await self.hass.async_add_executor_job(client.login)
                devices = await self.hass.async_add_executor_job(
                    client.get_device_list)
            except TPLinkNeedSmsError:
                # 账号开启了终端绑定：转入短信验证步骤（进入时自动发码）
                _LOGGER.info("账号 %s 开启了终端绑定，转入短信验证", username)
                self._username = username
                self._password = password
                self._terminal_uuid = terminal_uuid
                return await self.async_step_sms()
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
                    _LOGGER.info("账号 %s 下未发现门锁, 共 %d 台设备",
                                 username, len(devices))
                else:
                    _LOGGER.info("账号 %s 下发现 %d 台门锁", username, len(locks))
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

    # ------------------------------------------------------- 第二步：短信验证
    async def async_step_sms(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """终端绑定短信验证（仅账号开启登录安全设置时出现）。

        进入本步自动发送一次验证码；用户输入后带码重新登录。
        """
        errors: dict[str, str] = {}
        client = TPLinkCloudClient(
            username=self._username,
            password=self._password,
            terminal_uuid=self._terminal_uuid,
        )

        # 无输入 = 首次进入：自动发送验证码
        if user_input is None:
            try:
                await self.hass.async_add_executor_job(
                    client.send_terminal_verify_code)
                self._sms_sent_at = time.monotonic()
                _LOGGER.info("终端绑定验证码已发送")
            except TPLinkAuthError as err:
                # 密码错误 / 账号未开启绑定等：无法在本步恢复，回登录步
                _LOGGER.warning("发送验证码失败: %s", err)
                errors["base"] = "invalid_auth"
                return self.async_show_form(
                    step_id="user",
                    data_schema=STEP_USER_SCHEMA,
                    errors=errors,
                )
            except TPLinkCloudError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("发送验证码失败: %s", err)

        if user_input is not None:
            code = str(user_input.get("sms_code") or "").strip()
            resend = bool(user_input.get("resend"))
            if resend:
                # 重发不需要填码；冷却期内拒绝
                waited = time.monotonic() - self._sms_sent_at
                if waited < SMS_RESEND_COOLDOWN:
                    errors["base"] = "sms_cooldown"
                else:
                    try:
                        await self.hass.async_add_executor_job(
                            client.send_terminal_verify_code)
                        self._sms_sent_at = time.monotonic()
                        errors["base"] = "sms_resent"
                        _LOGGER.info("验证码已重新发送")
                    except TPLinkCloudError as err:
                        errors["base"] = "cannot_connect"
                        _LOGGER.warning("重新发送失败: %s", err)
            elif not code:
                errors["sms_code"] = "required"
            else:
                try:
                    await self.hass.async_add_executor_job(client.login, code)
                except TPLinkSmsCodeError:
                    errors["sms_code"] = "invalid_code"
                    _LOGGER.info("用户提交的验证码错误")
                except TPLinkAuthError as err:
                    errors["base"] = "invalid_auth"
                    _LOGGER.warning("带码登录失败: %s", err)
                except TPLinkCloudError as err:
                    errors["base"] = "cannot_connect"
                    _LOGGER.warning("带码登录失败: %s", err)
                else:
                    # 验证通过：该终端已绑定，继续列门锁
                    try:
                        devices = await self.hass.async_add_executor_job(
                            client.get_device_list)
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
                        else:
                            self._locks = locks
                            return await self.async_step_device()

        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema(
                {
                    # 可选：勾选"重新发送"时无需填码
                    vol.Optional("sms_code", default=""): str,
                    vol.Optional("resend", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "cooldown": str(SMS_RESEND_COOLDOWN),
            },
        )

    # ------------------------------------------------------- 第三步：选择门锁
    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """从账号下的门锁列表中选择（可多选，一个条目管多把锁）。"""
        options = {d["deviceId"]: _lock_label(d) for d in self._locks}

        if user_input is not None:
            chosen = list(user_input.get(CONF_DEVICES) or [])
            if not chosen:
                return self.async_show_form(
                    step_id="device",
                    data_schema=vol.Schema(
                        {vol.Required(CONF_DEVICES): cv.multi_select(options)}),
                    errors={CONF_DEVICES: "required"},
                    description_placeholders={"locks": self._locks_text()},
                )
            devices = []
            for did in chosen:
                lock = next(d for d in self._locks if d["deviceId"] == did)
                devices.append({
                    "device_id": did,
                    "device_name": _lock_label(lock),
                })
            # unique_id 用第一把锁（防重复添加）
            await self.async_set_unique_id(
                "%s_%s" % (DOMAIN, chosen[0]))
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=_title_for(devices),
                data={
                    "username": self._username,
                    "password": self._password,
                    CONF_DEVICES: devices,
                    CONF_TERMINAL_UUID: self._terminal_uuid,
                },
            )

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICES): cv.multi_select(options),
                }
            ),
            description_placeholders={"locks": self._locks_text()},
        )

    def _locks_text(self) -> str:
        return "\n".join(
            "- %s（%s…%s）" % (_lock_label(d), d["deviceId"][:8],
                              d["deviceId"][-4:])
            for d in self._locks
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TPLinkLockOptionsFlow(config_entry)


class TPLinkLockOptionsFlow(config_entries.OptionsFlow):
    """选项流：菜单 -> 轮询间隔 / 修改登录信息 / 切换门锁。"""

    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        # 兼容两种注入方式：构造器传参（旧）与框架设置 self.config_entry（新）
        self._entry_arg = config_entry
        self._locks: list[dict[str, Any]] = []

    @property
    def _entry(self) -> ConfigEntry:
        return self._entry_arg or self.config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        """选项菜单。"""
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "interval": "轮询间隔",
                "login": "修改登录信息（账号/密码）",
                "add_device": "添加门锁",
                "remove_device": "去除门锁",
            },
        )

    # ------------------------------------------------------------ 轮询间隔
    async def async_step_interval(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        entry = self._entry
        return self.async_show_form(
            step_id="interval",
            data_schema=vol.Schema({
                vol.Optional(
                    "scan_interval",
                    default=entry.options.get("scan_interval",
                                              DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=600)),
            }),
        )

    # ------------------------------------------------------------ 登录信息
    async def async_step_login(self, user_input=None) -> FlowResult:
        """修改账号密码（换账号时自动派生新终端 UUID）。"""
        errors: dict[str, str] = {}
        entry = self._entry
        if user_input is not None:
            username = user_input["username"].strip()
            password = user_input["password"]
            # 账号变更则换终端 UUID；同账号沿用原终端
            if username.lower() != entry.data["username"].lower():
                terminal_uuid = _terminal_uuid_for(username)
            else:
                terminal_uuid = entry.data[CONF_TERMINAL_UUID]
            client = TPLinkCloudClient(username, password, terminal_uuid)
            try:
                await self.hass.async_add_executor_job(client.login)
            except TPLinkNeedSmsError:
                # 新账号首次登录需短信验证，选项流内不支持，
                # 引导用户删除条目后走配置向导（内含短信步骤）
                errors["base"] = "need_sms_reconfig"
                _LOGGER.info("选项流改账号需短信验证: %s", username)
            except TPLinkAuthError as err:
                if "上限" in str(err):
                    errors["base"] = "terminal_limit"
                else:
                    errors["base"] = "invalid_auth"
                _LOGGER.warning("选项流登录失败: %s", err)
            except TPLinkCloudError as err:
                errors["base"] = "cannot_connect"
                _LOGGER.warning("选项流连接失败: %s", err)
            else:
                new_data = dict(entry.data)
                new_data["username"] = username
                new_data["password"] = password
                new_data[CONF_TERMINAL_UUID] = terminal_uuid
                self.hass.config_entries.async_update_entry(
                    entry, data=new_data)
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(
                    title="", data=dict(entry.options))

        return self.async_show_form(
            step_id="login",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "username",
                        default=entry.data["username"],
                    ): str,
                    vol.Required("password"): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------ 添加门锁
    async def async_step_add_device(self, user_input=None) -> FlowResult:
        """把账号下尚未添加的门锁加入本条目（可多选）。"""
        errors: dict[str, str] = {}
        entry = self._entry

        if user_input is not None:
            chosen = list(user_input.get(CONF_DEVICES) or [])
            if not chosen:
                errors[CONF_DEVICES] = "required"
            else:
                current = _devices_of_entry(entry)
                have = {d["device_id"] for d in current}
                for did in chosen:
                    if did in have:
                        continue
                    lock = next(d for d in self._locks
                                if d["deviceId"] == did)
                    current.append({"device_id": did,
                                    "device_name": _lock_label(lock)})
                new_data = dict(entry.data)
                new_data[CONF_DEVICES] = current
                self.hass.config_entries.async_update_entry(
                    entry, data=new_data, title=_title_for(current))
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(
                    title="", data=dict(entry.options))

        if not self._locks:
            client = TPLinkCloudClient(
                entry.data["username"],
                entry.data["password"],
                entry.data[CONF_TERMINAL_UUID],
            )
            try:
                # 关键：选项流里的 client 是新建的，必须先登录拿 token，
                # 否则 getDeviceList 会以 -20104（参数/会话无效）失败
                _LOGGER.debug("选项流：登录云端以拉取设备列表")
                await self.hass.async_add_executor_job(client.login)
                devices = await self.hass.async_add_executor_job(
                    client.get_device_list)
            except TPLinkNeedSmsError:
                _LOGGER.info("选项流：该账号需要短信验证")
                errors["base"] = "need_sms_reconfig"
                return self.async_show_form(
                    step_id="add_device", data_schema=vol.Schema({}),
                    errors=errors)
            except TPLinkAuthError as err:
                _LOGGER.warning("选项流登录失败: %s", err)
                errors["base"] = ("terminal_limit" if "上限" in str(err)
                                  else "invalid_auth")
                return self.async_show_form(
                    step_id="add_device", data_schema=vol.Schema({}),
                    errors=errors)
            except TPLinkCloudError as err:
                _LOGGER.warning("选项流拉设备列表失败: %s", err)
                errors["base"] = "cannot_connect"
                return self.async_show_form(
                    step_id="add_device", data_schema=vol.Schema({}),
                    errors=errors)
            self._locks = [
                d for d in devices
                if d.get("deviceType") == LOCK_DEVICE_TYPE
            ]

        # 本条目已有 + 其它条目已占用 的都不列出，避免重复实体
        have = {d["device_id"] for d in _devices_of_entry(entry)}
        try:
            have |= _other_entry_device_ids(self.hass, entry)
        except Exception:  # noqa: BLE001  老版本没有 async_entries 时忽略
            pass
        candidates = [d for d in self._locks if d["deviceId"] not in have]
        available = {d["deviceId"]: _lock_label(d) for d in candidates}
        if not available:
            errors["base"] = "no_new_locks"
        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICES): cv.multi_select(available)}
            ) if available else vol.Schema({}),
            errors=errors,
            description_placeholders={
                "locks": "\n".join(
                    "- %s（%s…%s）" % (_lock_label(d), d["deviceId"][:8],
                                      d["deviceId"][-4:])
                    for d in candidates
                ) or "（无可添加）",
            },
        )

    # ------------------------------------------------------------ 去除门锁
    async def async_step_remove_device(self, user_input=None) -> FlowResult:
        """从本条目移除一把门锁（至少保留一把）。"""
        errors: dict[str, str] = {}
        entry = self._entry
        current = _devices_of_entry(entry)
        options = {d["device_id"]: d.get("device_name") or d["device_id"][-8:]
                   for d in current}
        locks_text = "\n".join("- %s" % v for v in options.values())

        if len(current) <= 1:
            # 只剩一把：没有可选项，直接给出明确原因（否则是空白表单）
            _LOGGER.info("条目 %s 只有一把锁，拒绝移除", entry.entry_id)
            errors["base"] = "cannot_remove_last"
            return self.async_show_form(
                step_id="remove_device", data_schema=vol.Schema({}),
                errors=errors,
                description_placeholders={"locks": locks_text},
            )

        if user_input is not None:
            remove_id = user_input[CONF_DEVICE_ID]
            remaining = [d for d in current if d["device_id"] != remove_id]
            new_data = dict(entry.data)
            new_data[CONF_DEVICES] = remaining
            self.hass.config_entries.async_update_entry(
                entry, data=new_data, title=_title_for(remaining))
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_create_entry(
                title="", data=dict(entry.options))

        return self.async_show_form(
            step_id="remove_device",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_ID): vol.In(options)}
            ),
            errors=errors,
            description_placeholders={"locks": locks_text},
        )
