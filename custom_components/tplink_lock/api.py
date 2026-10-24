"""TP-LINK 物联云 API 客户端（同步 requests，在 executor 中调用）。

算法 100% 还原自 libNetworkUtil.so (arm64) 并经真机抓包逐位验证：
  X-Authorization = Timestamp={ts},Nonce={n},AccessKey={AK},Signature={hmac}
  sigstr = path(去前导'/') \n X-ExtraInfo \n base64(md5(body)) \n ts \n nonce
  Signature = HMAC-SHA1(SK, sigstr) 小写 hex
  X-ExtraInfo = "Account-Token=<token>,Term-Id=<tid>"（未登录无前缀）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import time
from typing import Any

import requests
import urllib3

# TLS 降级后避免每条请求刷 InsecureRequestWarning（已在日志中告警一次）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from .const import (
    ACCESS_KEY,
    APP_TYPE,
    CLIENT_INFO_QUERY,
    CONN,
    CT,
    ERR_NEED_SMS,
    ERR_OK,
    ERR_TERMINAL_LIMIT,
    ERR_TOKEN_INVALID,
    HOST_BASIC_CLOUD,
    SECRET_KEY,
    UA,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


class TPLinkCloudError(Exception):
    """云端通信错误。"""


class TPLinkAuthError(TPLinkCloudError):
    """认证失败（密码错误/需要短信验证码）。"""


class TPLinkSmsRequiredError(TPLinkAuthError):
    """新终端需要短信验证码完成绑定。"""


class TPLinkApiError(TPLinkCloudError):
    """接口返回非 0 error_code。"""


def _sig_string(path: str, extra_info: str, body: bytes,
                timestamp: str, nonce: str) -> str:
    p = path[1:] if path.startswith("/") else path
    md5_b64 = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
    return "%s\n%s\n%s\n%s\n%s" % (p, extra_info, md5_b64, timestamp, nonce)


def _extra_info(token: str | None, term_id: str) -> str:
    if token:
        return "Account-Token=%s,Term-Id=%s" % (token, term_id)
    return "Term-Id=%s" % term_id


def _describe_net_error(err: Exception) -> str:
    """把 requests 的网络异常翻译成可读原因。"""
    msg = str(err)
    if "Name has no usable address" in msg or "NameResolution" in msg:
        return ("DNS 解析失败（无可用地址）：请检查 HA 的 DNS 设置或路由器 DNS 代理，"
                "稍后会自动重试")
    if "Name or service not known" in msg or "nodename nor servname" in msg:
        return "DNS 解析失败：无法解析 TP-LINK 云端域名，请检查网络/DNS"
    if "timed out" in msg.lower():
        return "连接 TP-LINK 云端超时：网络不通或被拦截"
    return "网络请求失败：%s" % msg


def secure_login_body(username: str, password: str, terminal_uuid: str,
                      terminal_name: str = "HomeAssistant",
                      terminal_bind_code: str = "") -> str:
    """SecureLoginBean 的 Gson 序列化（字段按字母序，空串保留）。"""
    return ('{"appType":"%s","cloudPassword":"%s","cloudUserName":"%s",'
            '"refreshTokenNeeded":true,"terminalBindCode":"%s",'
            '"terminalName":"%s","terminalUUID":"%s"}'
            % (APP_TYPE, password, username, terminal_bind_code,
               terminal_name, terminal_uuid))


class TPLinkCloudClient:
    """与 TP-LINK 物联云交互（每账号一个实例）。"""

    def __init__(self, username: str, password: str, terminal_uuid: str,
                 device_id: str = "") -> None:
        self._username = username
        self._password = password
        self._terminal_uuid = terminal_uuid
        self.device_id = device_id
        self._session = requests.Session()
        self._session.trust_env = False  # 关键：绕过 Windows 系统代理
        self._verify_tls = True   # TP 云端证书链不完整时自动降级（见 _post）
        self._token: str | None = None
        self._term_id = "%s%x" % (terminal_uuid, int(time.time() * 1000))

    # ------------------------------------------------------------------ 基础
    def _post(self, action: str, payload: dict[str, Any],
              token: str | None) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        path = "biz/v1/%s?%s" % (action, CLIENT_INFO_QUERY)
        extra = _extra_info(token, self._term_id)
        body_b = body.encode("utf-8")
        ts = str(int(time.time()))
        nonce = str(random.randint(0, 2 ** 31 - 1))
        sig = hmac.new(
            SECRET_KEY.encode(), _sig_string(path, extra, body_b, ts, nonce).encode(),
            hashlib.sha1).hexdigest()
        headers = {
            "User-Agent": UA,
            "Content-Type": CT,
            "Connection": CONN,
            "X-ExtraInfo": extra,
            "X-Authorization": "Timestamp=%s,Nonce=%s,AccessKey=%s,Signature=%s"
                               % (ts, nonce, ACCESS_KEY, sig),
        }
        def _send(verify: bool):
            return self._session.post(
                "%s/%s" % (HOST_BASIC_CLOUD, path), data=body_b,
                headers=headers, timeout=REQUEST_TIMEOUT, verify=verify)

        try:
            resp = _send(self._verify_tls)
        except requests.exceptions.SSLError:
            # TP-LINK 云端部分节点证书链不完整（App 端同样不校验完整链），
            # 接口自身有 HMAC-SHA1 签名防篡改，此处降级并告警一次
            self._verify_tls = False
            _LOGGER.debug("TLS 证书链不完整，已降级为不校验（接口由 HMAC 签名保护）")
            resp = _send(False)
        except requests.exceptions.RequestException as err:
            # DNS/连接类瞬时故障（如 EAI_NODATA）重试一次再放弃
            _LOGGER.debug("请求异常（%s），1 秒后重试", err)
            time.sleep(1)
            try:
                resp = _send(self._verify_tls)
            except requests.exceptions.RequestException as err2:
                raise TPLinkCloudError(_describe_net_error(err2)) from err2
        if resp.status_code != 200:
            raise TPLinkCloudError("HTTP %s from %s" % (resp.status_code, action))
        try:
            return resp.json()
        except ValueError as err:
            raise TPLinkCloudError("非 JSON 响应: %s" % resp.text[:200]) from err

    def _request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """带 token 失效自动重登（仅一次）的请求。"""
        for attempt in (1, 2):
            data = self._post(action, payload, self._token)
            err = data.get("error_code")
            if err == ERR_OK:
                return data.get("result", {})
            if err in (ERR_TOKEN_INVALID,) and attempt == 1:
                _LOGGER.debug("token 失效(-%s)，重新登录", err)
                self.login()
                continue
            raise TPLinkApiError("%s -> %s" % (action, json.dumps(data, ensure_ascii=False)[:200]))
        raise TPLinkCloudError("%s 重试后仍失败" % action)

    # ------------------------------------------------------------------ 登录
    def request_terminal_bind_veri_code(self) -> None:
        """向新终端绑定手机号发送短信验证码。"""
        data = self._post(
            "getMobileVC4TerminalBind",
            {
                "appType": APP_TYPE,
                "cloudPassword": self._password,
                "cloudUserName": self._username,
                "terminalUUID": self._terminal_uuid,
            },
            None,
        )
        if data.get("error_code") != ERR_OK:
            raise TPLinkAuthError(
                "短信验证码发送失败 error_code=%s" % data.get("error_code"))

    def login(self, terminal_bind_code: str = "") -> dict[str, Any]:
        """secureLogin：返回 result（含 token / refreshToken）。"""
        body = secure_login_body(self._username, self._password,
                                 self._terminal_uuid,
                                 terminal_bind_code=terminal_bind_code)
        path = "biz/v1/secureLogin?%s" % CLIENT_INFO_QUERY
        extra = _extra_info(None, self._term_id)
        body_b = body.encode("utf-8")
        ts = str(int(time.time()))
        nonce = str(random.randint(0, 2 ** 31 - 1))
        sig = hmac.new(
            SECRET_KEY.encode(), _sig_string(path, extra, body_b, ts, nonce).encode(),
            hashlib.sha1).hexdigest()
        headers = {
            "User-Agent": UA,
            "Content-Type": CT,
            "Connection": CONN,
            "X-ExtraInfo": extra,
            "X-Authorization": "Timestamp=%s,Nonce=%s,AccessKey=%s,Signature=%s"
                               % (ts, nonce, ACCESS_KEY, sig),
        }
        def _send(verify: bool):
            return self._session.post(
                "%s/%s" % (HOST_BASIC_CLOUD, path), data=body_b,
                headers=headers, timeout=REQUEST_TIMEOUT, verify=verify)

        try:
            resp = _send(self._verify_tls)
        except requests.exceptions.SSLError:
            # TP-LINK 云端部分节点证书链不完整（App 端同样不校验完整链），
            # 接口自身有 HMAC-SHA1 签名防篡改，此处降级并告警一次
            self._verify_tls = False
            _LOGGER.debug("TLS 证书链不完整，已降级为不校验（接口由 HMAC 签名保护）")
            resp = _send(False)
        except requests.exceptions.RequestException as err:
            # DNS/连接类瞬时故障（如 EAI_NODATA）重试一次再放弃
            _LOGGER.debug("请求异常（%s），1 秒后重试", err)
            time.sleep(1)
            try:
                resp = _send(self._verify_tls)
            except requests.exceptions.RequestException as err2:
                raise TPLinkCloudError(_describe_net_error(err2)) from err2
        try:
            data = resp.json()
        except ValueError as err:
            raise TPLinkCloudError("登录响应非 JSON") from err
        err = data.get("error_code")
        if err == ERR_TERMINAL_LIMIT:
            raise TPLinkAuthError(
                "绑定终端数量已达上限，请在 TP-LINK 物联 App 账号安全中删除旧终端")
        if err == ERR_NEED_SMS:
            raise TPLinkSmsRequiredError("新终端需要短信验证")
        if err != ERR_OK:
            raise TPLinkAuthError("登录失败 error_code=%s" % err)
        result = data["result"]
        self._token = result["token"]
        _LOGGER.debug("secureLogin 成功, token=%s...", self._token[:16])
        return result

    # ------------------------------------------------------------------ 接口
    def get_device_list(self) -> list[dict[str, Any]]:
        """账号下全部设备（配置向导用它列出门锁供选择）。"""
        result = self._request("getDeviceList", {})
        return result.get("deviceList") or []

    def get_online_status(self) -> dict[str, Any]:
        return self._request("getDeviceOnlineStatus", {"deviceId": self.device_id})

    def get_device_config(self) -> dict[str, Any]:
        return self._request(
            "getDeviceConfig",
            {"deviceId": self.device_id,
             "moduleSectionInfo": {"dev_manager": {"name": ["dev_status"]}}},
        )

    def get_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        """取最新日志（desc）。"""
        result = self._request(
            "getDeviceLogByPage",
            {"deviceId": self.device_id, "start": -1, "end": -1,
             "limit": limit, "direction": "desc"},
        )
        return result.get("deviceLogInfos") or []

    # ------------------------------------------------------------------ 诊断
    def async_get_diagnostic_info(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "terminal_uuid": self._terminal_uuid}
