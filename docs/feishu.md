# 飞书配置

[文档目录](README.md) · [配置参考](configuration.md) · [日常使用](operations.md)

使用**企业自建应用机器人 + 长连接 + 本人单聊**，无需群 Webhook 或公网回调地址。同一应用只运行一个实例。

## 1. 准备应用与凭据

1. 在[开发者后台](https://open.feishu.cn/app)创建企业自建应用，添加「机器人」能力。组织需允许创建与发布应用。
2. 从「凭证与基础信息」取得 App ID、App Secret。
3. 在「权限管理」开通以下**应用身份权限**：

| 权限 | 用途 |
| --- | --- |
| `im:message:send_as_bot` | 发送消息、更新自己的卡片 |
| `im:message.p2p_msg:readonly` | 接收本人单聊消息 |

取得本人 Open ID：在 [API 调试台](https://open.feishu.cn/api-explorer)选中此应用，打开「发送消息」接口 `POST /open-apis/im/v1/messages`，将 `receive_id_type` 设为 `open_id`，点击「快速复制 open_id」选择自己。只需复制，无需发送消息。

三个值填入安装向导或本机 `.env`：

```dotenv
FEISHU_APP_ID='cli_EXAMPLE00000000'
FEISHU_APP_SECRET='REPLACE_WITH_APP_SECRET'
FEISHU_OWNER_OPEN_ID='ou_EXAMPLE00000000'
```

不要使用其他应用的 Open ID、组织 `user_id` 或群 `chat_id`。程序只接受配置的本人操作。

## 2. 启动本机服务

安装 [原生 CLI](native.md) 或 [Docker Compose](deployment.md)，保持后台在线：

| 检查 | 原生 | Docker |
| --- | --- | --- |
| 后台健康 | `media-importer status` | `docker compose ps` |
| 连接日志 | `media-importer logs` | `docker compose logs --tail 100 bot` |

程序已包含飞书官方 SDK，无需另跑示例程序。后台确认建立长连接后，才能保存下一步订阅。[官方长连接说明](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)

## 3. 订阅与菜单

在「事件与回调」中分别保存：

| 页签 | 接收方式 | 订阅 |
| --- | --- | --- |
| 事件配置 | 长连接 | `im.message.receive_v1` |
| 回调配置 | 长连接 | `card.action.trigger` |

消息事件接收文字与菜单，卡片回调处理确认和刷新，两者都需要。不填写请求 URL、Verification Token 或 Encrypt Key。

在「机器人 → 机器人自定义菜单」启用可切换菜单，默认展示菜单。可按下面组织：

| 主菜单 | 子菜单 | 发送文字 |
| --- | --- | --- |
| 状态 | 查看状态 | `查看状态` |
| 同步 | 开启同步 | `开启同步` |
| 同步 | 停止同步 | `停止同步` |
| 设置 | 删除原片开关 | `删除原片开关` |
| 设置 | 素材范围 | `素材范围` |
| 设置 | 预览文件 | `预览文件` |
| 设置 | 自动通知 | `自动通知` |

叶子菜单动作选择**发送文字消息**，不选推送事件；也可直接输入同名文字。限速、缓存额度等低频参数留在[配置文件](configuration.md)。[官方菜单说明](https://open.feishu.cn/document/client-docs/bot-v3/bot-customized-menu)

## 4. 发布与验证

在「版本管理与发布」创建版本，将可用范围限定为本人并发布；组织要求审核时由管理员批准。保存草稿不等于发布，权限、订阅和菜单变更后也要发布。

打开机器人单聊，保持原片删除关闭：

1. 「查看状态」应返回两路进度和连接状态。
2. 「刷新状态」应更新原卡片及北京时间。
3. 连接相机，核对确认卡身份，点击「确认并备份」。
4. 发送 `自动通知` 并开启，检查进度和结果提醒。

**开启同步会处理当前范围内的全部待导入素材及完整缓存，不能在菜单单选文件。** 首次验收使用少量素材。

## 通知

自动传输通知默认关闭。开启后，每 5 分钟更新同一张进度卡；完成与需要处理的失败单独提醒，间隔可在 JSON 修改。

| 消息 | 重点 |
| --- | --- |
| 状态 / 进度 | 导入、上传分别完成多少，当前文件与等待原因 |
| 本地导入完成 | 本轮导入结果、连接要求、待归档数量 |
| 归档完成 | 数量、容量、原片和缓存处理结果、实际归档位置 |
| 失败 / 恢复 | 影响与下一步；持续同一故障不重复刷屏 |
| 设备确认 / 断开 | 身份核对、连接变化；确认卡不依赖通知开关 |

消息时间统一为北京时间，逐文件日志留在本机。设备重新确认规则见[日常操作](operations.md#日常操作)。

## 常见问题

| 现象 | 检查 |
| --- | --- |
| 搜不到机器人 | 机器人能力、发布状态、本人可用范围、客户端组织 |
| 无法保存长连接 | 后台是否运行，App ID/Secret 是否匹配，网络是否可达 |
| 菜单缺失或旧版 | 发布后重新进入会话，先输入 `状态` 验证 |
| 文字无回复 | 消息权限、事件订阅、Owner Open ID、菜单动作 |
| 卡片不能操作 | 卡片回调是否发布；重新获取卡片，旧卡可能已失效 |
| 能查状态但无自动提醒 | `notifications.enabled`、本机发送日志 |

共享日志前移除凭据、私人路径与身份信息。设备或存储问题见[排障表](operations.md#故障排查)。
