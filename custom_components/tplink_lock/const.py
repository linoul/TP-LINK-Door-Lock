"""TP-LINK Door Lock integration constants."""

DOMAIN = "tplink_lock"

CONF_DEVICE_ID = "device_id"
CONF_TERMINAL_UUID = "terminal_uuid"

# 轮询间隔（秒）：日志高频轮询；状态/电量每 4 个周期刷一次
DEFAULT_SCAN_INTERVAL = 15

# 云端接口
HOST_BASIC_CLOUD = "https://n-wap-ipc.tplinkcloud.com.cn"

# --- 签名常量（100% 还原自 libNetworkUtil.so .rodata） ---
ACCESS_KEY = "8c55a4a7-c86e-45c7-bbf6-524363ca394b"   # @0x1ae6e5
SECRET_KEY = "85cd4b2f-8c89-4927-b384-3e48ea55d4d6"   # @0x1ae70a
APP_TYPE = "SurveillanceSystem_Android"                # App 硬编码

UA = "TP-LINK_APP"
CT = "application/json; charset=UTF-8"
CONN = "keep-alive"

# client-info 查询串（顺序敏感，与 App 抓包一致）
CLIENT_INFO_QUERY = (
    "appType=SurveillanceSystem_Android&ospf=10.0.0"
    "&appVer=5.15.22.2003&netType=wifi&locale=zh_cn&model=SM-N9600"
)

# 错误码
ERR_OK = 0
ERR_TOKEN_INVALID = -20651       # 登录超时/会话失效 -> 重新 secureLogin
ERR_TERMINAL_LIMIT = -23027      # 绑定终端数量上限
ERR_NEED_SMS = -23024            # 新终端需短信验证（terminalBindCode）

# 门锁日志类型（frida 抓包分析）
LOG_TYPE_LOCK = 1        # 开关锁操作
LOG_TYPE_DOOR = 2        # 门异常（门未关/虚掩/撬动）
LOG_TYPE_SETTING = 3     # 设置变更
LOG_TYPE_DOORBELL = 5    # 门铃
