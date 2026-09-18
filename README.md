# yande.re 找图（AstrBot 插件）

一站式搜图聚合：中文搜图 + 反向以图搜图 + 订阅推送 + 收藏，带会话级分级治理。
主图源 yande.re（可选 konachan / konachan.net），慢窗/故障自动回退国内源 Lolicon(pixiv)。
系统设计见 [DESIGN.md](DESIGN.md)。

## 特性

- **中文自然搜图**：近 2 万条经 yande.re 真实标签库校准的词表自动翻译；连写自动拆词
  （`白发女仆猫娘` → 三个概念）；词表未命中走 bge 嵌入召回 + 重排的语义匹配（RAG）
- **30s 出图保证**：主源超时自动竞速国内回退源（Lolicon API，pixiv 库，国内直连），
  全局截止时钟钳制搜索与下载预算
- **反向搜图**：回复图片 `/搜原图`，SauceNAO → iqdb 链式反查，命中本站图源直接补图
- **治理**：会话级分级上限（s/q/e）、R18 白名单解锁、冷却、每日配额、合并转发
- **其他**：热门榜、续看、收藏、每日订阅推送、LLM 自然语言兜底（可选）

## 命令一览

| 命令 | 别名 | 说明 |
|---|---|---|
| `/p <词...> [n]` | — | 搜图，默认随机（同标签组合自动排除最近发过的图）；加 `精选` 走质量优先 |
| `/热门 [今日\|本周\|本月] [n]` | `/hot` | 站点热门榜，按会话分级上限过滤 |
| `/下一张 [n]` | `/next` | 续看本会话最近一次搜索（10 分钟内有效） |
| `/标签 <词>` | `/tag` | 中文↔英文双向查词 + 站内数量 + 相似标签 |
| `/搜原图` | `/sn` `/sauce` | 回复图片反查出处；命中本站图源直接补图 |
| `/收藏 [序号]` · `/我的收藏 [页]` | `/fav` `/favlist` | 收藏最近发出的图 |
| `/搜图设置 ...` | `/dbset` | 管理员：`查看`/`r18 on\|off`/`分级 s\|q\|e`/`冷却 8`/`配额 80`/`转发 on`/`开关 on`/`模式 off` |
| `/订阅` · `/退订 <编号>` | `/sub` `/unsub` | 管理员：`/订阅 热门 08:00` 或 `/订阅 猫娘 08:00 3` 每日推送 |
| `/搜图模式 on\|off` | `/searchmode` | 管理员：开启后本会话所有图片自动反搜（30s 一张） |
| `/搜图帮助` | `/dbhelp` | 命令总表 |

语法要点：数量后缀 1-5；多词为 AND（空结果时自动 `_` 连写重试）；分级直觉词
`r18`/`涩图`/`擦边`/`全年龄`（或 `rating:e`）、排序词 `精选` 和 `site:` 不占标签位。

管理员在插件配置 `admin_users` 里设置（逗号/空格分隔 QQ 号），AstrBot 管理员自动兼容。

## 配置要点

- **`proxy_url`**：图站 API 与图片 CDN 在国内网络通常需要代理，按需填写
- **语义匹配（可选，强烈建议）**：自动复用 AstrBot 已配置的 `openai_embedding` /
  `vllm_rerank` provider（bge-m3 + bge-reranker-v2-m3）；也可在插件配置里显式指定
  `embed_api_url` 等。需要语义索引文件（见下），缺失时该层自动关闭，仅用词表
- **回退源（可选，默认开启）**：`fallback_sites=lolicon`，国内网络慢窗时约 10s 内出图；
  `fallback_headstart` 控制主源领先时间，`deadline_seconds` 为全局截止

## 语义索引构建

`tag_index.npz`（约 75MB）不随仓库分发，需自建（约 5 分钟，需能访问 yande.re）：

```bash
pip install httpx numpy
HTTPS_PROXY=http://127.0.0.1:7890 \
EMBED_URL=https://your-embed-host/v1 EMBED_KEY=xxx \
RERANK_URL=https://your-rerank-host/v1/rerank \
python3 tools/build_embed_index.py
```

产物 `tag_index.npz` + `tag_index_meta.json` 放插件根目录即可。
`tags_zh.json`（18,433 条中文词表）已随仓库附带，由 `tools/build_tags.py` 生成：
多个开源中文词表对 yande.re 真实标签库全量校准过滤，保证每条都能搜到图。

## 治理模型

- **分级上限**：群默认 `group_rating_cap`（s），私聊默认 `default_rating`；
  管理员按会话覆盖。e 级解锁 = 配置 `r18_whitelist` ∪ 管理员 `/搜图设置 r18 on`
  （存库，二者并集）；查询超上限自动降级并提示解锁方法
- **频控**：会话冷却（默认 8s，管理员豁免）+ 每人每日图量配额（默认 80）
- 状态存 `data/plugin_data/astrbot_plugin_yandere_search/state.db`（sqlite WAL）

## 测试

```bash
python3 tools/local_test.py --quick   # 离线全量回归（假事件栈，不需要 AstrBot）
python3 tools/local_test.py           # 含 live 网络项
```

## License

LGPL-3.0-or-later（见 [LICENSE](LICENSE)）
