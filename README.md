# China Tech Tracker 中国科技动态追踪

自动抓取 → 模型分类摘要 → 生成公开网页。同事只需浏览器，无需登录或安装。
页面：https://wangclaire57-rgb.github.io/China-Tech-Tracker/

---

## 分类体系：两个维度

**8 个产业板块**（sector）
`AI` · `Robotics` · `Semiconductors` · `Quantum` · `Digital Connectivity` · `Biotech` · `Green Tech` · `Space`

**6 类新闻性质**（news_type）

| 类型 | 覆盖 |
|---|---|
| `Breakthrough` 新技术 | 研究成果、技术里程碑、首次实现 |
| `Product` 新产品 | 产品/型号/服务发布、量产、交付 |
| `Corporate` 公司动态 | 战略调整、重组、合作、人事、订单、产能 |
| `Capital` 资本运作 | IPO、融资、并购、股价、估值 |
| `Policy` 监管政策 | 监管、国家规划、标准、行业组织、政府合作 |
| `Geopolitics` 国际关系 | 出口管制、制裁、实体清单、关税、国际合作与冲突 |

页面上两个维度可以交叉筛选——比如只看「Semiconductors × Capital」（半导体领域的融资并购）
或「Space × Breakthrough」（航天技术突破）。

---

## 文件

| 文件 | 作用 |
|---|---|
| `sources.yml` | 信源清单。**增删信源只改这个文件** |
| `build.py` | 主流程：抓取 → 去重 → 加工 → 写页面 → 健康检查 |
| `providers.py` | 模型适配层，claude/deepseek/qwen/gemini 四家可切换 |
| `notify.py` | 失败通知（飞书/钉钉/企业微信/Slack） |
| `template.html` | 页面模板 |
| `migrate_archive.py` | 一次性：把旧存档迁移到新分类体系 |
| `compare.py` | 多厂商横评，生成 compare.html |
| `data/items.json` | 累积存档（可转 Excel） |
| `data/health.json` | 每次运行的健康快照 |

---

## 信源设计原则：主题式 > 站点式

**不要用 `site:xxx.com when:7d` 作为主力查询。**

实测：`site:taibo.cn when:7d` 返回 88 条，但前几条是「会员服务」页和一条人事新闻——
Google 按「对该站点的相关性」排序，不按重要性，真正的航天报道排在 `max_items_per_run`
截断线之后，根本进不了管线。这就是为什么旧配置下航天板块一个月只抓到 4 条，
且其中 3 条不是航天新闻。

主题式查询 `中国 商业航天 OR 火箭 发射 成功 when:14d` 的头部直接就是重要新闻。

两种写法：

```yaml
# 主题式（主力）—— 问「这件事谁报道了」
- id: space_launch
  name_cn: 中国航天·发射
  name_en: Space · Launch
  type: gnews
  query: "中国 商业航天 OR 火箭 发射 成功 when:14d"
  lang: zh
  default_sector: Space
  max_items_per_run: 40

# 站点式（辅助）—— 必须加主题词收窄，否则全是导航页
- id: taibo_space
  name_cn: 泰伯网
  name_en: Taibo
  type: gnews
  query: "site:taibo.cn 卫星 OR 火箭 OR 航天 OR 遥感 when:14d"
  lang: zh
  default_sector: Space

# 原生 RSS（有就优先用，最干净）
- id: scmp_tech
  type: rss
  url: https://www.scmp.com/rss/36/feed
  lang: en
  default_sector: AI
```

`default_sector` 只是兜底，模型会重新判定实际板块。
`enabled: false` 可临时停用某个源。

---

## 配置

**Secrets**（只配你用的那家）
`DASHSCOPE_API_KEY`（千问）/ `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `GEMINI_API_KEY`
`NOTIFY_WEBHOOK_URL`（可选，群机器人）

**Variables**

| 名字 | 默认 | 说明 |
|---|---|---|
| `TRACKER_PROVIDER` | `qwen` | `claude` / `deepseek` / `qwen` / `gemini` |
| `TRACKER_MODEL` | 各家默认 | 型号名变动频繁时在这里覆盖 |
| `TRACKER_CONCURRENCY` | `4` | **限流严重时调到 2** |
| `TRACKER_MAX_NEW` | `250` | 单次最多加工多少条新内容 |
| `NOTIFY_WEBHOOK_TYPE` | `feishu` | `feishu`/`dingtalk`/`wecom`/`slack` |
| `NOTIFY_PAGE_URL` | — | 页面地址，附在告警消息里 |

---

## 为什么会「跑成功但没更新」

模型调用失败会被 `try/except` 吞掉，单条跳过。如果 key 失效或额度用尽，
**每条都失败，但 workflow 仍然显示绿色**——页面还在、内容看着正常，只是停在某一天。

所以 `build.py` 每次跑完自己判一次，命中任一条就退出码 1：

- 所有信源返回空 / 超过 1/3 的信源返回空
- 有新内容但一条都没加工成功
- 加工失败率超过 25%
- 出现致命错误（key 无效 / 额度不足 / 型号名无效）

`data/health.json` 会记录**真实的错误原文**和归类统计，例如
`"错误构成：限流(429)×38，认证失败(key 无效)×2"`——不用翻 Actions 日志就知道是哪种问题。

页面顶部还有一层：超过 48 小时没更新会亮红色横幅。

---

## 成本

每天新增约 200 条（稳态），每条约 1,200 input + 250 output tokens：

| provider | 型号 | 每月 |
|---|---|---|
| `qwen` | qwen3.7-flash | **≈ $0.4** |
| `gemini` | gemini-3.5-flash | $0（免费额度内，但免费层内容会被用于训练） |
| `deepseek` | deepseek-flash（平峰） | ≈ $2 |
| `claude` | claude-opus-5 | ≈ $75 |

GitHub Actions 公开仓库免费，Pages 免费。

---

## 用千问（阿里云百炼）的注意事项

**限流额度非常宽松**：`qwen3.7-flash` 在北京区是 RPM 30,000 / TPM 10,000,000，
而且**充值不改变这个阈值，免费和付费一样**。按每天 200 条算，你用掉不到千分之一。
所以如果出现大批失败，**基本不会是 RPM/TPM 限流**，要往额度和并发上查。

**三种容易混淆的错误，必须分清：**

| 错误码 | HTTP | 含义 | 怎么办 |
|---|---|---|---|
| `Throttling.RateQuota` | 429 | 请求频率超限 | 重试即可，一分钟内恢复 |
| `Throttling.Concurrency` | 429 | 并发数超平台动态上限 | 调低 `TRACKER_CONCURRENCY` |
| `AllocationQuota.FreeTierOnly` | **403** | **免费额度已耗尽** | **充值或完成实名认证**，重试无用 |
| `Arrearage` | 400 | 账户欠费 | 充值 |
| `InvalidApiKey` | 401 | key 无效 | 换 key |
| `ModelNotFound` | 404 | 型号名不存在 | 用 `TRACKER_MODEL` 改名 |

注意 `403` 在百炼这里是**额度耗尽**，不是认证失败——很多通用错误处理会把 403 归到
「key 有问题」，方向就跑偏了。`build.py` 已经按百炼的码做了区分，`health.json` 会
直接写明是哪一种。

**已关闭思考模式**：`providers.py` 里给 qwen 传了 `enable_thinking: false`。
分类摘要属于批量抽取，不需要思考链，开着纯粹浪费 output token（思考过程是计费的）。
部分 Qwen3 型号在「非流式 + 思考」下还会直接返回 400。

**并发**：默认 `TRACKER_CONCURRENCY=4` 偏保守。既然 RPM 上限是 30,000，
跑通之后可以试着调到 8–10 提速；只有撞上 `Throttling.Concurrency` 才需要调回去。
