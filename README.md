# Feishu Calendar Bridge

**把一个飞书日历单向同步到个人 Google 账号的附加日历。**

One-way Feishu CalDAV → personal Google Calendar sync.

飞书是日程来源，Google 中的 `Feishu` 日历是副本。工具在 Mac 上每 120 秒读取一次飞书，并用 Google Calendar API 更新副本；电脑休眠或关机会暂停。用户已验证自己的 agent 能读取 Google 附加日历，工具本身只负责日历同步。

**当前状态：开发预览版。** 已有本地合成日程和模拟 Google API 测试；飞书真实账号、个人 Gmail OAuth、Mac launchd 以及连续 8 天运行尚待实际验收。因此以下时效是设计目标，尚非实测结果。

## 能做什么

- 新增、改标题/时间、取消、重复日程、全天日程、跨时区事件；单次改期保留原实例身份。
- 选择标题与时间或仅 `Busy`。`CLASS:PRIVATE/CONFIDENTIAL` 始终只输出 `Busy`；没有验证私密标记前只允许仅忙闲模式。
- 默认镜像过去 7 天至未来 180 天的日程。自然老化的历史副本保留，窗口内的删除需两次完整扫描确认；明确取消标记可直接移除。
- 目标日历中的手工事件不受影响。已有镜像被手工改动时会恢复；带参与人、会议链接等额外内容的镜像会暂停处理，避免邀请副作用。
- 本地 SQLite 记录映射和写入意图，确定性 Google event ID 防止崩溃重试产生副本。所有 Google 事件关闭默认提醒。

地点、描述、参与人、会议链接和附件不会复制。Google 里的改动不会回传飞书。源只读、无服务端、无遥测、无公开 ICS URL。

## 准备

1. **飞书 CalDAV：** 在飞书日历设置中生成用于第三方日历的 CalDAV 用户名和专用密码。组织若未开放该功能，需要管理员处理。[飞书第三方日历说明](https://www.feishu.cn/hc/zh-CN/articles/079695875192-%E7%AC%AC%E4%B8%89%E6%96%B9%E6%97%A5%E5%8E%86%E5%90%8C%E6%AD%A5-%E7%AE%A1%E7%90%86%E5%91%98%E6%89%8B%E5%86%8C)
2. **个人 Google 账号：** 在自己的 Google Cloud 项目启用 Calendar API，配置 OAuth 同意屏幕，创建 **Desktop app** 类型 OAuth client，下载 JSON 并放在本仓库以外。此工具仅申请 `calendar.app.created`，用于创建和管理其附加日历。[Google 桌面 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app) · [Calendar 权限](https://developers.google.com/workspace/calendar/api/auth)
3. **Mac：** Python 3.12+。下例使用 [uv](https://docs.astral.sh/uv/)；也可用 `python3 -m venv` 与 `pip install -e .`。

Google OAuth 同意屏幕若保持 External / Testing，带日历权限的 refresh token 通常 7 天到期；长期自用需要按 Google 当前政策处理发布状态，Production 不等于审核通过或永久有效。[Google token 生命周期](https://developers.google.com/identity/protocols/oauth2)

## 安装和第一次同步

```sh
git clone https://github.com/Azurboy/feishu-gcal-bridge.git
cd feishu-gcal-bridge
uv sync --locked
uv run fgbridge setup
```

`setup` 会让你输入飞书 CalDAV 地址、用户名和隐藏输入的密码，发现并选择一个日历；若源时区无法自动取得，会要求输入 IANA 时区。随后输入 Google Desktop client JSON 的绝对路径，在浏览器授权个人 Google 账号。首次成功设置会创建一个专用 `Feishu` 附加日历并立即同步。再次运行同一绑定不会另建日历。

建议先在飞书创建不含工作信息的测试日程，确认 Google 中的副本。需要导出标题时，先用飞书 UI 创建一个私密测试日程，使 `setup` 能确认源中存在 `CLASS:PRIVATE/CONFIDENTIAL` 标记；否则工具只能运行仅忙闲模式。此检查仍须在真实飞书账号验收后才能视为兼容性保证。

```sh
uv run fgbridge sync --dry-run
uv run fgbridge sync
uv run fgbridge status
uv run fgbridge schedule install
```

`--dry-run` 只计算预计变更，不写 Google 事件，也不推进连续缺失计数。定时任务安装于当前用户的 macOS `launchd`，登录期间每 120 秒启动一次。`status` 显示最后尝试、最后完整成功、最近增改删数量和错误码；超过 10 分钟没有完整成功会显示陈旧。

若源突然从非空变为全空，同步会停止删除。确认选中正确的飞书日历且 CalDAV 确实返回空窗口后，才运行：

```sh
uv run fgbridge sync --confirm-empty
```

## 停用和清理

```sh
uv run fgbridge schedule remove
```

这只停止后台任务，保留 Google 副本。若要彻底退出，在 Google Calendar 手工删除本工具创建的 `Feishu` 附加日历，在 Google 账号撤销 OAuth 授权，在飞书撤销 CalDAV 专用密码，最后删除本机 `~/Library/Application Support/fgbridge/` 中的配置、token 和状态文件。不要删除其他日历。

本地凭据目录权限为 `0700`，文件为 `0600`；文件权限保护并不等于加密。所选导出字段进入 Google 后，其他获该 Google 账号授权的应用可能读取它们。日志只包含数量和错误码，不包含标题、密码、token 或原始 ICS。

## 当前验证边界

```sh
uv sync --locked --extra dev
uv run --extra dev pytest -q
```

合成测试覆盖重复、单次和此后改期、取消、全天、夏令时、私密降级、部分读取失败、连续缺失、崩溃恢复与丢失 SQLite 后恢复。**尚未完成**个人 Gmail 最小权限调用、飞书 UI 实际样本比对、20 次时效测量与连续 8 天运行。未完成这些验收前，请把本仓库视作可审查的开发预览，而非已验证的日常日历同步产品。详细验收条件见[开发规格](docs/DEVELOPMENT_SPEC_v0.1.md)。

如果发现飞书兼容问题，请提交操作步骤、预期/实际时间、脱敏错误码和版本；不要上传 token、专用密码、完整 ICS 或真实会议内容。项目采用 [MIT License](LICENSE)。
