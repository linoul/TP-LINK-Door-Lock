# TP-LINK Door Lock (Home Assistant 自定义集成)

把 **TP-LINK TL-SL41 智能门锁**接入 Home Assistant。走 TP-LINK 物联云 API。

> ⚠️ 非官方集成。TP-LINK 未公开此 API，接口随时可能变更。使用风险自负。

## 功能

| 能力 | 说明 |
|---|---|
| 电量 | 电池百分比 + 电池电压/充电状态属性 |
| 门锁状态 | 正常 / 门锁破坏 / 已上锁 / 门未关 / 门虚掩 / 已开锁 / 门未锁好（映射取自 App 反编译） |
| 最近事件 | 状态直接显示事件内容，例如「"张三"通过人脸开门成功」 |
| 事件实体 | 开门 / 上锁 / 门异常 / 门铃 / 验证失败 / 设置变更 / 其他 |
| 配置向导 | 账号密码登录 → 自动列出门锁下拉选择，无需手抄 deviceId |
| 多门锁 | 同一账号下多把门锁可分别添加 |

## 安装

### 方式一：HACS（推荐）

1. HACS → 集成 → 右上角 ⋮ → **自定义仓库**
2. 仓库地址填入本项目地址，类别选 **Integration**
3. 搜索 **TP-LINK Door Lock** 安装
4. 重启 Home Assistant

### 方式二：手动

把 `custom_components/tplink_lock/` 整个目录复制到 HA 的 `config/custom_components/` 下，重启 HA。

## 配置

1. 设置 → 设备与服务 → **添加集成** → 搜索 `TP-LINK Door Lock`
2. 输入 TP-LINK 物联 App 的**账号（手机号/邮箱）**和**密码**
3. 登录成功后会出现下拉列表，选择要接入的门锁 → 提交

> 首次从新终端登录可能需要在 App 里完成一次短信验证；
> 若提示「终端数量已达上限」，到 App「账号与安全 → 登录设备管理」删除旧设备即可。

## 实体

| 实体 | 说明 |
|---|---|
| `sensor.<门锁>_电量` | 电池百分比（诊断分类） |
| `sensor.<门锁>_门锁状态` | 中文状态文本；属性：`原始值`、`告警` |
| `sensor.<门锁>_最近事件` | 最近一条事件内容；属性：`事件类型`、`时间`、`logId` |
| `event.<门锁>` | 事件实体，供自动化按 `event_type` 触发 |

### 门锁状态映射

| lock_status | 状态 | 告警 |
|---|---|---|
| 0 | 正常 | |
| 1 | 门锁破坏 | ✅ |
| 2 | 已上锁 | |
| 3 | 门未关 | ✅ |
| 4 | 门虚掩 | ✅ |
| 5 | 已开锁 | |
| 7 | 门未锁好 | ✅ |
| 其他 | `--` | |

### 事件类型

`unlock`（开门）、`lock`（上锁）、`door_alarm`（门异常）、`doorbell`（门铃）、`verify_failed`（验证失败）、`setting_change`（设置变更）、`other`。

## 自动化示例

### 有人开门时通知（带成员名）

```yaml
automation:
  - alias: 门锁 - 有人开门
    trigger:
      - platform: state
        entity_id: event.hao_shi_you_ting_men_suo
    condition:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'unlock' }}"
    action:
      - service: notify.mobile_app_你的手机
        data:
          title: 门锁已开启
          message: "{{ trigger.to_state.attributes.content }}"
```

### 门未关/虚掩提醒

```yaml
automation:
  - alias: 门锁 - 门未关提醒
    trigger:
      - platform: state
        entity_id: sensor.hao_shi_you_ting_men_suo_men_suo_zhuang_tai
        to: ["门未关", "门虚掩", "门未锁好"]
        for: "00:02:00"
    action:
      - service: notify.mobile_app_你的手机
        data:
          message: "门锁状态：{{ states('sensor.hao_shi_you_ting_men_suo_men_suo_zhuang_tai') }}"
```

### 门铃触发

```yaml
automation:
  - alias: 门锁 - 门铃
    trigger:
      - platform: state
        entity_id: event.hao_shi_you_ting_men_suo
    condition:
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'doorbell' }}"
    action:
      - service: tts.speak
        target:
          entity_id: media_player.客厅音箱
        data:
          message: 有人按门铃
```

### 低电量

```yaml
trigger:
  - platform: numeric_state
    entity_id: sensor.hao_shi_you_ting_men_suo_dian_liang
    below: 20
```

## 选项

设置 → 设备与服务 → 集成 → TP-LINK Door Lock → **配置**，可调整轮询间隔（10–600 秒，默认 15 秒）。修改后集成自动重载。

轮询策略：日志每周期轮询（事件延迟 ≈ 轮询间隔）；在线状态与电量仅在启动、发现新日志、或每 4 个周期时刷新，以减少云端请求。

## 调试日志

默认**不落盘**。需要排查时在 `configuration.yaml` 加入：

```yaml
logger:
  default: warn
  logs:
    custom_components.tplink_lock: debug
```

重启后日志写入 `config/tplink_lock_debug.log`，排查完删除该配置即可恢复静默。

## 已知限制

- **非实时**：门锁事件依赖云端轮询，延迟约为轮询间隔（默认 15 秒）。App 的实时推送走厂商推送通道（MiPush/HMS），无第三方可订阅接口。
- **门锁常态休眠**：设备为电池供电深度休眠，在线状态经常为离线，属正常现象。
- **TLS**：TP-LINK 部分节点证书链不完整，集成会自动降级为不校验（接口本身有 HMAC 签名保护），日志会出现一条 WARNING。
- **仅支持物联云账号体系**：`n-wap-ipc.tplinkcloud.com.cn` 域名下的设备；Tapo/Kasa 本地协议（python-kasa）不支持门锁。
- **终端上限**：每个账号可绑定终端数量有限，向导按账号派生固定终端 UUID，避免重复占用。


## 免责声明

本项目与 TP-LINK 官方无关，仅供个人学习与自用。请自行评估账号安全与设备风险，不要用于商业用途。
