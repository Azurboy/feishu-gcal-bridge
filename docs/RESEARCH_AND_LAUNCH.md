# 社区调研、项目定位与发布建议

调研日期：2026-09-29。结论服务于一个小型开源项目；不作市场规模预测。开发范围以 [v0.1 规格](./DEVELOPMENT_SPEC_v0.1.md) 为准。

## 1. 调研结论

**值得做一个聚焦飞书的小工具。** 已有公开证据表明，有人长期需要把飞书日程带到个人日历入口；解决方案散落在导出脚本、CalDAV 兼容修补、ICS 转换和自建同步项目里。证据足以支持“先解决自己的问题并公开”，不足以支持“这是一个已验证的大市场”。

用户最新补充：自己的 agent 已能读到 Google 附加日历。因此产品只需做好日历数据进入 Google，agent 是下游使用者，无需另建适配层。

建议一句话定位：

> **让个人 Google 日历及时看到飞书日程。**

英文：

> **Keep your Feishu calendar in sync with Google Calendar.**

第二句再交代：适用于个人 Gmail；自行运行；飞书单向同步到一个 Google 附加日历。AI 助手、个人排程、统一日程视图都是用途，不把某个 agent 品牌写入产品名。

## 2. 社区到底证明了什么

| 证据 | 观察 | 支持什么 / 不支持什么 |
| --- | --- | --- |
| [Xuanwo：从飞书导出到 Fastmail](https://xuanwo.io/reports/2023-35/)，2023-08-28 | 工作使用飞书，个人想在 Fastmail 统一查看；需要处理 CalDAV 目录发现 | 真实跨日历需求、飞书接入摩擦；不直接证明 Gmail 用户规模 |
| [Jiajie Chen：导出为 iCalendar](https://jia.je/software/2025/02/04/feishu-dump-calendar/)，2025-02-04，后续更新 | 使用飞书 CalDAV 凭据与 vdirsyncer 导出 ICS | 用户愿意自行搭建；文章引用前述实践，不能把引用链都计为独立需求 |
| [python-caldav issue #459](https://github.com/python-caldav/caldav/issues/459)，2024-12-06 | 报告飞书可列目录但 GET 事件失败；issue 已关闭 | 明确的历史兼容线索；不能宣称当前版本仍有同一缺陷 |
| [Google Calendar 社区：外部日历刷新](https://support.google.com/calendar/thread/254277846/in-which-intervals-does-googe-synchronize-external-calendars?hl=en)，2024-01 | 用户询问 URL 订阅更新频率 | 订阅时效是实际困扰；志愿者回复不是官方 SLA |
| [Muse 用户实践](https://www.reddit.com/r/MetaAI/comments/1wk92xe/useful_things_ive_set_up_with_muse/) | 作者介绍用 Google 日历生成家庭行程摘要 | 日历是个人 agent 的有用上下文；单帖不能推断采用率或所有日历类型兼容 |
| 本次用户实验，2026-09-29 | 用户报告其 agent 能读取 Google 附加日历 | 本项目可据此聚焦同步本身；非独立复测，也不代表所有 agent |

检索覆盖 GitHub 项目和 issues、开发者博客、Google 帮助社区、Reddit，以及中文社区关键词。未检索到足以量化需求的广泛独立讨论；搜索引擎未覆盖的私域、登录内容及未索引帖子不在结论内。没有访谈、下载量或留存数据。

不将泛 CalDAV 教程、搬运文章、SEO 内容当成新增用户证据；对声称飞书“双向写入”的非官方教程，也不拿来证明本项目能够写回飞书。

## 3. 现有方案与我们应有的差别

Star 为 2026-09-29 访问页面时的显示值或近似值，会变化；它反映关注度，不等于安全审计、成熟度或当前可用性。

| 方案 | 已看到的能力 | 对本需求的取舍 |
| --- | --- | --- |
| [飞书官方 Google 双向同步](https://www.feishu.cn/hc/zh-CN/articles/071816947155-管理员配置-google-日历与飞书日历的双向同步) | 管理员配置服务账号、Workspace 后台与成员映射 | 不适用于当前个人 Gmail 的管理员配置路径 |
| [adjamian/lark-gcal-sync](https://github.com/adjamian/lark-gcal-sync)，0 stars | 最接近的专用项目；OpenAPI → Google 镜像、Python/macOS、SQLite、定时 | 可参考现成行为；配置飞书自建应用可能涉及组织审批。我们的主要差别应是 CalDAV 接入和飞书兼容体验 |
| [nemofq/caldav-to-ics](https://github.com/nemofq/caldav-to-ics)，5 stars | CalDAV → ICS，可用于飞书；Vercel Blob 提供订阅 URL | 可复用思路；README 明示 URL 公开、默认每日任务，不满足我们对可控更新频率的预期 |
| [RobbyV2/caldav-ics-sync](https://github.com/RobbyV2/caldav-ics-sync)，4 stars | CalDAV/ICS 转换、自托管 WebUI，含飞书 URL 兼容提示 | 已有通用桥接；我们无需复制它的管理界面和更广范围 |
| [GAS-ICS-Sync](https://github.com/derekantrican/GAS-ICS-Sync)，约 1.9k stars | 用 Apps Script 将 ICS 同步进 Google，支持更频繁执行 | 更有社区积累；前端仍需可读取的 ICS 来源，值得作为现成替代方案介绍 |
| [vdirsyncer](https://github.com/pimutils/vdirsyncer)，约 1.9k stars | 已有 CalDAV 等同步基础工具，社区飞书导出实践使用过 | 底层协议工具并非全都冷门；我们应复用成熟库，不重写通用同步平台 |
| [CalendarBridge：任意 CalDAV 支持](https://help.calendarbridge.com/announcements/connect-any-caldav-calendar/)，2026-08-10 公告 | 商业服务宣布加入通用 CalDAV 连接 | 真实替代选项；尚未验证飞书兼容和该来源的具体延迟，不宣称市场上只有我们能做 |

因此，不建议以“现有工具 stars 太少，所以另起一个更权威项目”为理由。新项目同样从零 stars 开始。更好的理由是：**把飞书 → 个人 Google 这条路径做得更容易安装、行为清楚、遇到错误能解释。**

若开发探测发现现有维护中的工具只需小补丁就能满足要求，优先评估贡献补丁或提供精简配置包。独立项目的价值应来自降低这条路径的使用成本，而非堆功能。

### 实际值得复用什么

- 复用 CalDAV、iCalendar、Google OAuth 的成熟库，避免自己写协议和时间解析。
- 学习现有项目的同步窗口、独立镜像和启动方式，保留来源说明。
- 不未经核验就照抄其“永不重新授权”“Google 修改必定覆盖”等承诺。
- 许可证按依赖和引用分别检查；例如 GAS-ICS-Sync 标为 GPL-3.0，不能因为公开可读就随意复制进拟采用 MIT 的项目。

## 4. Cue、Muse 的准确角色

已按用户澄清识别为 **Manus 的 Cue** 和 **Meta 的 Muse**，排除了同名产品。

- [Manus 2.0 官方介绍](https://www.manus.im/blog/introducing-manus-2-0)介绍 Cue 个人 agent 应用；[官方博客目录](https://manus.im/blog)列出 2026-09-28 的发布记录。正文直接打开不稳定，身份依据来自官方索引及搜索摘录。Manus 的 [连接器文档](https://help.manus.im/en/articles/12231777-how-can-i-use-manus-connectors)提到 Google Calendar；不能仅据此声称 Cue 完整继承所有连接器能力。
- [Meta 官方 Muse 介绍](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/)发表于 2026-09-08，描述个人 agent 的浏览器和应用使用能力；不能据此声称 Muse “只能触达 Google 日历”。
- 用户已经做过附加日历实验，工程可以直接利用这一点。公开 README 写“可供你已连接 Google Calendar 的工具使用”，不写“获得 Cue/Muse 官方支持”。

AI 助手的发展让这个问题更容易被注意到，这是合理产品判断；“爆火必将带来大量需求”仍属于假设。我们无需先证明宏观趋势，先做出能连续使用的小工具即可。

## 5. 项目该如何定位

### 首批用户

在飞书工作的个人，同时使用个人 Gmail，并希望在 Google Calendar、个人日程软件或 agent 中看到工作时间。他们能够取得飞书 CalDAV 凭据，愿意完成一次本地配置。

首版的受众应诚实地限定为可接受自部署的用户。Google OAuth client 配置仍然有门槛，不能宣传“任何人一分钟安装”。

### 三个卖点就够

1. **个人 Gmail 可用的路径**：无需购买 Workspace 或配置域级管理员同步；实际可用仍依赖飞书开放 CalDAV。
2. **飞书细节处理好**：发现地址、重复日程、改期取消、全天时间、凭据失效提示。
3. **看得懂的同步状态**：最后完整成功时间、失败原因、明确的单向边界；不让错误伪装成同步完成。

自行运行、最小 Google 权限、无公开 ICS 链接是支持这些卖点的事实。未实现前保持“设计目标”措辞，发布后仅将实测通过的内容改为能力陈述。

不需要宏大品牌。工作名 `Feishu Calendar Bridge`、可搜索仓库名 `feishu-gcal-bridge` 已足够；暂不投入域名、Logo、落地页或多渠道内容系统。

## 6. README 应该怎么写

完整草案见 [README_DRAFT.md](./README_DRAFT.md)。顺序如下：

1. 一句话说明飞书 → Google，紧接着写个人 Gmail 与单向镜像。
2. 一个真实演示：飞书创建 → Google 出现 → 飞书改期/取消 → Google 更新。agent 读取可以在演示末尾顺带出现。
3. 使用条件：CalDAV、Google 账号、运行环境、电脑休眠影响。
4. 最短可复现安装路径，凭据获取单独解释。发布前必须从干净环境重走一遍。
5. 同步/不导出的字段，尤其是重复、取消、私密和历史窗口。
6. 常见故障：目录发现、401、OAuth 第 7 天、事件不更新、缺少实例。
7. 已测平台与版本、来源致谢、贡献与诊断信息要求。

不放假 badge、未验证兼容表、无法执行的安装命令或“企业级”之类词。项目还没实现时清楚写 proposal；正式发布再用真实版本号、下载入口、测试记录替换。

### 可直接使用的 GitHub About 文案

> One-way Feishu → Google Calendar sync for personal Gmail accounts. Self-hosted, with Feishu CalDAV support.

建议 topics：`feishu`、`lark`、`google-calendar`、`caldav`、`calendar-sync`、`self-hosted`。其中 Lark 国际版在实测前不得借 topic 暗示已经支持。

## 7. 如何让用户注意到：轻量发布顺序

### 第一步：先有一个可信演示

用合成事件录一段 45–60 秒视频，标出真实经过的时间；创建、改期、取消三件事都展示。演示标题直接使用用户会搜索的问题：

> 飞书日历怎么同步到个人 Google Calendar？我做了一个小工具。

配套一张字段/权限表、一份实测安装说明，就足以发布。无需先建网站。不要加速视频后暗示瞬时同步。

### 第二步：找 3–5 位同类用户

在自己认识、明确有需求的人里试用：他们是否能独立取得 CalDAV 凭据、完成 Google 授权、隔天仍在更新。这比首批 star 数更能决定下一步。

只记录使用者自愿提供的几个结果：

- 完成配置了吗？卡在哪一步？
- 第一次事件到 Google 花了多久？
- 第 7–8 天还在正常同步吗？
- 飞书改期或取消，有没有留下旧事件？

默认不开遥测，不收集会议标题。若几个人都卡在 OAuth，再集中解决安装体验；不提前开发一整套公共登录服务。

### 第三步：把实践发布到具体问题所在的社区

| 渠道 | 适合内容 | 时机 |
| --- | --- | --- |
| GitHub README / Discussions | 安装、实测记录、飞书常见问题与替代方案 | 有可运行版本时 |
| V2EX、少数派等中文社区 | 个人 Gmail 的实际问题 + 一次完整演示 + 开源链接 | 至少几位用户跑通后；按版规发布 |
| Manus/Cue、Muse 相关社区 | 你的 agent 已连 Google，但缺飞书工作日程；展示这一具体用法 | 有对应产品的实际演示后，注明独立项目 |
| 原相关项目/issues | 反馈可复现飞书问题、贡献修复或补充方案 | 仅内容确实相关时，不批量贴推广链接 |
| Show HN / Product Hunt | 面向国际使用者的可运行版本和英文说明 | 有需求再做，不列为首版任务 |

这里是发布计划，本轮没有发帖、联系用户或发送消息。实施发布时仍需遵守目标社区当时的规则。

### 发布文案草稿

> 工作会议都在飞书，个人日历和 AI 助手却在用 Google Calendar。我做了一个小工具，把飞书日程单向同步到个人 Gmail 下的附加日历。重点处理改期、取消、重复日程和全天事件；凭据保留在自己机器上。这里是实际演示、安装方式和已知限制。欢迎同样使用飞书 + 个人 Google 日历的人试用，尤其希望收集配置过程中卡住的地方。

此文案须在功能实现和验证后使用。附上真实链接与实测延迟，不保留不存在的演示。

## 8. 信任如何建立

Stars 可以帮助发现项目，但无法直接解决用户“敢不敢给日历凭据”的担忧。前几个版本优先提供：

- 代码短且用途集中；依赖锁定、许可证清楚。
- README 明示飞书只读、Google 专用日历、数据字段与本地凭据路径。
- 真正覆盖改期、取消、断网重试、重复实例的测试。
- 发布记录写清验证过的环境和已知问题，不声称审计或官方合作。
- 报错模板只接收脱敏状态，不要求上传 token、完整 ICS 或公司会议内容。

这些都直接服务一个小工具的可用性，无需“信任中心”、复杂认证计划或额外产品线。

## 9. 仍未验证的事项

1. 当前用户所属飞书租户是否开放 CalDAV，输出是否完整覆盖常用日程语义。
2. 使用选定 Python 库后，历史发现/GET 问题在当前飞书是否仍需处理。
3. 本工具的最小 Google scope、长期授权和实际传播延迟；本轮只核对文档，未连接账号。
4. 其他 CalDAV 服务及 Lark 国际版兼容性；不进入首版承诺。
5. 有多少人愿意自行配置；目前没有独立的市场或转化数据。

以上不妨碍批准一个小规模实现。它们分别在首次连接、日常运行和少量试用中回答，不需要先启动完整市场研究。
