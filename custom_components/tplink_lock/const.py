"""TP-LINK Door Lock integration constants."""

DOMAIN = "tplink_lock"

CONF_DEVICE_ID = "device_id"
CONF_DEVICES = "devices"          # 多门锁列表 [{"device_id", "device_name"}]
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
ERR_CODE_INVALID = -20607        # 短信验证码错误

# 门锁日志类型（frida 抓包分析）
LOG_TYPE_LOCK = 1        # 开关锁操作
LOG_TYPE_DOOR = 2        # 门异常（门未关/虚掩/撬动）
LOG_TYPE_SETTING = 3     # 设置变更
LOG_TYPE_DOORBELL = 5    # 门铃

# --- 型号兜底（2026-11-06） --------------------------------------------------
# 设备页型号优先取云端 getDeviceList 的 deviceModel；只有在云端读不到时
# 才回退到这个值。保留上游的 TL-SL41，避免把其他型号错标成 SL42 Pro。
DEFAULT_MODEL = "TL-SL41"

# 云端日志结构化字段的中文标签。key 统一用字符串，兼容 int/str 两种返回。
# 只列有真实日志样本佐证的取值；未知值保留原始数字，避免误标。
VERIFY_TYPE_LABELS = {
    "8": "人脸",
}
TEMPLATE_ID_LABELS = {
    "1": "开门",
    "6": "验证失败",
    "7": "室内开门",
    "8": "关门上锁",
}
# templateId -> 事件类型：结构化字段优先于中文关键词匹配
TEMPLATE_EVENT_MAP = {
    "1": "unlock",
    "6": "verify_failed",
    "8": "lock",
    # SL42 Pro 猫眼侦测：真实日志样本「有人在门前逗留」，logTypeId=4，
    # 带 snapshotUrl / baseUrl / duration / fileSize 媒体字段。
    "96": "loiter",
}

# --- 第三轮：App 设置项 / 逗留录像（2026-11-06） ---------------------------
# getDeviceConfig 把 moduleSectionInfo.dev_manager.name 置为空数组时，
# 云端会返回全部三个配置段：dev_status / dev_config / dev_info。
# 以下是 dev_config 中实测存在的字段（SL42 Pro 固件 1.0.16）。
DEV_CONFIG_FIELDS = (
    "auto_lock_delay", "face_verify_enable", "dual_verify_enable",
    "pried_warning_enable", "face_verify_mode", "face_verify_interval",
    "face_detect_sensitivity", "standby_mode", "alias",
)
DEVICE_INFO_SECTION_FIELDS = (
    "fw_major", "fw_front_panel", "fw_touch", "fw_wifi_panel",
    "fw_motor_panel", "hw_version", "device_model", "mac", "qr_code",
)

# 数值型设置项的中文标签（未知值保留原始数字，不臆测）
FACE_VERIFY_MODE_LABELS = {"0": "普通模式", "1": "安全模式"}
FACE_DETECT_SENSITIVITY_LABELS = {"0": "低", "1": "中", "2": "高"}
# 源码 SettingConstant.js 的 StandbyMode 是三态，不是 0/1 开关：
#   0=已关闭（不待机，常在线） 1=长连接 2=节能唤醒（深度休眠，需 wakeupDevice）
STANDBY_MODE_LABELS = {
    "0": "已关闭",
    "1": "长连接",
    "2": "节能唤醒",
}

# 逗留录像日志字段（templateId=96）
LOITER_TEMPLATE_ID = "96"
SNAPSHOT_FETCH_TIMEOUT = 12

# --- 实体扩展（2026-11-06 第二轮） -----------------------------------------
# 每轮拉取的日志条数：用于统计今日开关门次数、最近门铃/告警时间。
# 只是同一接口的 limit 参数变大，不增加请求次数。
LOG_FETCH_LIMIT = 50

LOW_BATTERY_THRESHOLD = 20

# 权威映射：反编译 SmartLockStatus.getLockStatusDesc() + res 字符串
LOCK_STATUS_LABELS = {
    "0": "正常",
    "1": "门锁破坏",
    "2": "已上锁",
    "3": "门未关",
    "4": "门虚掩",
    "5": "已开锁",
    "7": "门未锁好",
}
LOCK_STATUS_LOCKED = "2"
# App isWarning(): 1/3/4/7 视为告警态
LOCK_STATUS_WARNING = {"1", "3", "4", "7"}

# getDeviceList 中允许带进 HA 的设备元信息字段（避免把云端凭据带入 hass.data）
DEVICE_INFO_FIELDS = (
    "deviceId", "deviceModel", "deviceName", "deviceType",
    "fwVer", "hwVer", "deviceHwVer", "deviceMac", "mac",
    "region", "alias", "status", "deviceStatus",
)

# 开锁方式：优先用云端 verifyType，未知时按日志文案关键词兜底。
# 顺序敏感：「临时密码」必须先于「密码」，「人脸」先于「指纹」。
# templateId -> 开锁方式（有真实日志样本佐证）
TEMPLATE_UNLOCK_METHOD = {
    "7": "室内开门",
}

UNLOCK_METHOD_KEYWORDS = (
    ("室内开门", "室内开门"),
    ("门内", "室内开门"),
    ("室内", "室内开门"),
    ("临时密码", "临时密码"),
    ("人脸", "人脸"),
    ("指纹", "指纹"),
    ("密码", "密码"),
    ("门卡", "门卡"),
    ("NFC", "门卡"),
    ("卡片", "门卡"),
    ("钥匙", "机械钥匙"),
    ("远程", "远程开锁"),
    ("APP", "远程开锁"),
    ("App", "远程开锁"),
    ("双重验证", "双重验证"),
    ("组合", "组合验证"),
)

# 「近期告警」二进制传感器：这些事件类型在时间窗口内出现即置 on
RECENT_ALARM_WINDOW_MINUTES = 10
ALARM_EVENT_TYPES = frozenset({
    "door_alarm", "tamper", "loiter", "verify_failed", "low_battery",
})
