# yande.re 找图插件 v2 系统设计

> 状态：**已实现（v2.0.0，2026-09-17）**。本地假事件栈 E2E 24/24 通过（`tools/local_test.py`）。
> 实施中发现的环境结论见 §8；原设计内容保留作为功能规格。

## 1. 现状盘点（v1.1.0）

已有能力：

- `/db`（质量优先）/ `/dbr`（随机）两条命令，`rating:` / `order:` 元前缀
- 18,433 条 yande.re 校准中文词表（`tags_zh.json`，全部经 post.xml 实测可搜）
- 空结果诊断（计数核对 + 相似标签建议）、多词 join 重试
- md5 键磁盘缓存、信号量限流下载、WebP 本地转码（动图保留动画）
- `tools/build_tags.py` 词表可重建流水线

短板（v2 要解决的）：

| 短板 | 说明 |
|---|---|
| 命令名不可读 | `/db` 是 Danbooru 时代的遗产，新用户无从理解 |
| 无反向搜图 | 二次元社群最高频需求之一："这张图的出处/原图" |
| 无热门流 | yande.re 原生 popular 接口完全没用上，缺少日常互动点 |
| 无续搜 | 看到喜欢的想"再来一张"只能重敲命令 |
| 无会话治理 | rating、频控、每日配额全靠全局配置，群主无法分级管控 |
| 单一图源 | konachan 同为 Moebooru，接入成本≈0 却没有用上 |

## 2. 生态调研结论

调研对象与发现：

- **AstrBot 插件市场**：已有 `astrbot_plugin_setu`（随机图 + 标签 + 分级 + 群组控制）和 `astrbot_plugin_img_rev_searcher_Ver2`（SauceNAO/ASCII2D 反搜聚合）。说明两块需求都真实存在，但目前"正向搜图"与"反向搜图"分裂在两个插件里——本插件定位是**一站式搜图聚合**。
- **CQ-picfinder-robot（Tsuk1ko，标杆项目）**：核心功能面 = SauceNAO 反搜、搜图模式（正则触发后连续图片自动反搜）、图库指定、`--ban-u` / `--ban-g` 黑名单、antiR18 过滤。治理与反搜的思路直接借鉴。
- **AstrBot 框架能力（已查文档确认）**：主动推送（`context.send_message(unified_msg_origin, chain)`）、管理员命令门（`filter.permission_type(filter.PermissionType.ADMIN)`）、aiocqhttp 合并转发（`Node` / `Nodes` 组件）、插件内调 LLM（`await self.context.get_using_provider_async()` → `prov.text_chat()`）。**框架没有内置 `filter.schedule` 定时装饰器**，订阅推送需自建 asyncio 调度循环。

## 3. 功能规划

### P0（v1.2.0 · 治理与体验补齐，零新外部依赖）

| 编号 | 功能 | 说明 |
|---|---|---|
| F1 | 中文命令 + 别名兼容 | `/搜图` `/随机` 为主命令，`/db` `/dbr` 保留为别名，老用户无感 |
| F2 | 会话级治理 | 每会话 rating 上限、冷却秒数、每日配额、开关；sqlite 存储，`/搜图设置` 管理（管理员） |
| F3 | 热门流 | `/热门`，后端 `post/popular_by_day/week/month.json`（已实测 200；day 支持 `day/month/year` 参数；返回混合分级，需按会话上限过滤，不足跨天补齐） |
| F4 | 续搜 | `/下一张`：记住会话内最近一次查询与游标，TTL 10 分钟 |
| F5 | 标签工具 | `/标签`：zh→en / en→zh 双向查词表 + 站内计数 + 相关标签推荐，把词表能力从"幕后翻译"变成"可查询的工具" |

### P1（v1.3.0 · 反搜与多源）

| 编号 | 功能 | 说明 |
|---|---|---|
| F6 | 以图搜图 | SauceNAO（JSON API，免费 key 约 100 次/天）→ iqdb.org 兜底；输出处链接 + 相似度 + 缩略图；命中 yande.re/konachan 时可直接调本插件取图 |
| F7 | 多图源 | 接入 konachan.com / konachan.net（同为 Moebooru，客户端仅 `api_url` 参数化）；`/搜图源` 或 `site:` 前缀切换 |
| F8 | 合并转发 | ≥3 张图时用 aiocqhttp `Node`/`Nodes` 打包成转发消息，降低刷屏与风控 |

### P2（v2.0.0 · 智能与订阅）

| 编号 | 功能 | 说明 |
|---|---|---|
| F9 | 订阅推送 | `/订阅 热门 08:00` 或 `/订阅 猫娘 08:00 3`；插件内 asyncio 调度循环（30s 粒度）+ `context.send_message` 主动推送 |
| F10 | LLM 自然语言搜图 | 长句未命中词表时，用 AstrBot 内置 LLM 把"银发红瞳的猫娘在雨天"翻译成标签组合，再经本地有效标签集校验过滤——LLM 只产候选，词表与站点计数做裁判 |
| F11 | 收藏夹 | `/收藏`（对最近发出的图）/ `/我的收藏` |
| F12 | 搜图模式 | picfinder 式：会话内开关，开启后所有图片自动反搜 |

## 4. 命令列表（v2 全量）

### 4.1 主命令

| 命令 | 别名 | 参数 | 权限 | 说明 |
|---|---|---|---|---|
| `/搜图 <词...> [n]` | `/db` `/yss` | 中文/英文标签、数量 1-5、`rating:` `order:` `site:` | 所有人 | 标签搜索，质量优先 |
| `/随机 <词...> [n]` | `/dbr` | 同上 | 所有人 | 标签搜索，随机取样 |
| `/热门 [周期] [n]` | `/hot` | 周期=今日(默认)/本周/本月 | 所有人 | 站点热门榜，按会话 rating 上限过滤 |
| `/下一张 [n]` | `/next` | 数量 1-5 | 所有人 | 复述本会话最近一次查询，取后续结果 |
| `/标签 <词>` | `/tag` | 中文或英文 | 所有人 | 查词表映射 + 站内计数 + 相关标签 |
| `/搜原图` | `/sn` `/sauce` | 回复一张图，或命令附带图片 | 所有人 | 以图搜图：SauceNAO → iqdb 链式反查出处 |

示例：

```
/搜图 猫娘 3
/搜图 雷姆 rating:e 5
/随机 旗袍 site:konachan
/热门 本周 5
/标签 巨乳
（回复某张图）/搜原图
```

### 4.2 管理命令（管理员）

| 命令 | 别名 | 子命令 | 说明 |
|---|---|---|---|
| `/搜图设置` | `/dbset` | `查看` | 查看本会话全部设置 |
| | | `rating <s\|q\|e>` | 本会话分级上限（超过即降级并提示） |
| | | `冷却 <秒>` | 本会话命令冷却，默认 8 |
| | | `配额 <张/日>` | 每人每日图片配额，默认 80 |
| | | `转发 <on\|off>` | ≥3 图合并转发，默认 on |
| | | `开关 <on\|off>` | 本会话总开关 |
| `/订阅` | `/sub` | （无参） | 列出本会话订阅 |
| | | `热门 <HH:MM>` | 每日定时推热门 |
| | | `<词> <HH:MM> [n]` | 每日定时推关键词新图 |
| `/退订 [编号]` | `/unsub` | | 取消订阅 |
| `/搜图帮助` | `/dbhelp` | | 输出命令总表 |

### 4.3 P2 附加

| 命令 | 说明 |
|---|---|
| `/收藏` | 收藏本会话最近发出的 1 张图 |
| `/我的收藏 [页]` | 翻看收藏 |
| `/搜图模式 <on\|off>` | 开启后本会话所有图片自动反搜 |

### 4.4 语法规范

- **数量后缀**：写在命令尾，1-5，受全局 `max_images` 封顶。
- **元前缀**（不占标签位）：`rating:s|q|e`（超会话上限会被降级）、`order:score|random`、`site:yandere|konachan|konachan_net`。
- **多词 = AND**；全空结果时自动用 `_` 连接重试（沿用现有逻辑）。
- **翻译链**：中文 → 词表精确命中 → 命中即用；未命中的长句（P2 起）走 LLM 兜底，LLM 输出必须通过本地有效标签集校验，无效标签静默丢弃。
- 命令名可用配置 `use_chinese_commands`（默认 on）一键切回纯英文别名，适配非中文环境。

## 5. 架构设计

```
main.py                    命令层：解析/编排/治理检查/消息组装（唯一 astrbot 依赖点）
├── yandere.py → booru.py  Moebooru 客户端（api_url 参数化多源；新增 popular_by_* 方法）
├── reverse.py   [P1]      SauceNAO + iqdb 反搜链，全局配额计数与降级
├── governance.py          会话设置 / 冷却 / 每日配额（sqlite DAO）
├── subscribe.py [P2]      订阅表 + asyncio 调度循环 + context.send_message 推送
├── tagger.py    [P2]      词表匹配 + LLM 候选生成 + 有效集校验
├── imaging.py             不变（WebP 转码）
└── state.db               sqlite（WAL，单写协程）
```

请求主流程：

```
命令 → governance.check(会话开关/冷却/配额/rating上限)
     → 解析（数量、元前缀、标签）→ 翻译（词表 [+LLM]）
     → booru.search / popular
     → 空结果诊断（count 核对 + suggest_tags）
     → 并发下载（缓存命中优先）→ WebP 转码
     → yield 单图 / ≥3 图合并转发 → usage 记账
```

## 6. 数据模型（state.db）

```sql
chats(umo TEXT PK, rating_cap TEXT DEFAULT 's', cooldown_s INT DEFAULT 8,
      quota INT DEFAULT 80, forward INT DEFAULT 1, enabled INT DEFAULT 1)
history(umo, user_id, kind, query_json, cursor, ts)   -- “下一张”用，TTL 10min
usage(umo, user_id, day, imgs)                        -- 每日配额记账
subs(id, umo, kind, payload, hh_mm, last_run_day)     -- 订阅
favorites(user_id, post_id, source, ts)               -- [P2]
```

## 7. 治理与安全

- **分级**：会话 `rating_cap`（群默认 `s`，私聊默认取全局 `default_rating`）；用户显式请求超上限 → 自动降级到上限并提示一次；`e` 档额外要求会话在全局 `r18_whitelist`（ umo 列表）内。
- **热门流过滤**：`popular_by_*.json` 返回混合分级且不接受 rating 参数 → 客户端按上限过滤，结果不足时向前多取一天补齐。
- **频控**：每会话冷却（默认 8s，管理员豁免）+ 每人每日图量配额（默认 80）。
- **SauceNAO 配额**：免费 key 约 100 次/天，全局计数入 state.db，超限自动降级为 iqdb-only 并提示。
- **依赖零增**：调度用 asyncio 循环（框架无 `filter.schedule`），HTML 解析用标准库，`requirements.txt` 保持只有 Pillow。

## 8. 外部接口验证状态

已实测（2026-09-17）：

- ✅ `GET /post/popular_by_day.json`、`popular_by_week.json`、`popular_by_month.json` → 200；day 版支持 `day/month/year` 参数；返回 40 条、含 s/q/e 混合分级。
- ✅ `GET /post.json?tags=date:2026-09-16+order:score` → 200（备选热门实现）。
- ❌ `age:<1d` 元标签无效（返回空）；`/post/popular.json`（无 by_day 后缀）→ 404。
- ✅ AstrBot：`context.send_message(umo, MessageChain)`、`filter.permission_type(filter.PermissionType.ADMIN)`、`Node`/`Nodes` 合并转发（aiocqhttp）、`get_using_provider_async` + `text_chat`（均为官方文档确认）。

实施时待验证（现已验证，2026-09-17）：

- ⚠️ SauceNAO 从本机与 mihomo 代理出口均被 Cloudflare 挑战（HTTP 403 "Just a moment"）→
  代码保留实现（可用网络/带 key 时生效），遇 CF 拦截自动 10 分钟退避，iqdb 成为主力引擎；
- ✅ iqdb.org：搜索表单必须 **multipart**（urlencoded 会被当首页重新渲染），结果表是无
  class 的裸 `<table>`（靠 `% similarity` 过滤），链接为协议相对地址需补 `https:`；
- ⚠️ konachan.com 对本机及代理出口均 403（数据中心 IP 被 CF 拒）→ 保留为可选源
  （`extra_sites`），住宅网络下应正常；yande.re 全接口正常；
- ✅ AstrBot `MessageChain().message().file_image()` 链式调用返回 self；aiocqhttp 进站
  图片组件带 `url` 字段；`Reply.chain` 内嵌被回复消息组件列表；`filter.command(name, alias=set)`、
  `filter.permission_type(filter.PermissionType.ADMIN)`、`filter.event_message_type(EventMessageType.ALL)`
  均与 master 分支源码核对一致；
- Moebooru `-tag` 排除语法未在文档暴露（未验证，不用）。

## 9. 里程碑

| 版本 | 内容 | 新增依赖 |
|---|---|---|
| v1.2.0 | F1 中文命令、F2 治理、F3 热门、F4 续搜、F5 标签工具 | 无 |
| v1.3.0 | F6 以图搜图、F7 多源、F8 合并转发 | SauceNAO API key（免费注册） |
| v2.0.0 | F9 订阅推送、F10 LLM 自然语言、F11 收藏、F12 搜图模式 | 无 |

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| SauceNAO 免费额度用尽 | 全局计数 + 自动降级 iqdb；响应携带剩余额度提示 |
| iqdb 改版导致解析失败 | 解析层独立、失败时降级为返回搜索链接 |
| LLM 产出幻觉标签 | LLM 只产候选，一律过本地有效集（词表值域 + 站点计数），无效即丢 |
| 主动推送被平台限流 | 推送频率最低每日一次；aiocqhttp 群消息不受"主动消息"矩阵限制 |
| sqlite 并发写 | WAL + 单写协程，读走连接池 |
