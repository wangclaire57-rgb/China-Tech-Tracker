# China Tech Tracker

An automated news tracker for Chinese technology, policy and markets. It collects
articles from a fixed list of approved outlets, uses an LLM to translate, summarise
and classify each one, and publishes a filterable web page.

**Live page:** https://wangclaire57-rgb.github.io/China-Tech-Tracker/

Anyone can read the page in a browser. No login, no software to install.

> **Setting this up for the first time?** Read **[DEPLOYMENT.md](DEPLOYMENT.md)** instead —
> it walks through the whole process step by step. This file is the reference for
> people maintaining an existing deployment.

---

## How it works

```
GitHub Actions  (runs daily at 00:00 UTC / 08:00 Beijing, on GitHub's servers)
      │
      ├─ Read sources.yml ────────── what to search for
      ├─ Fetch RSS / Google News ─── raw articles
      ├─ Filter by whitelist.yml ─── discard anything not from an approved outlet
      ├─ De-duplicate ───────────── same story from several outlets → keep one
      ├─ Send to the LLM ────────── English title, <100-word summary, sector, type, companies
      ├─ Write data/items.json ──── cumulative archive (exports to Excel)
      └─ Write docs/index.html ──── the page
                │
        GitHub Pages publishes it
```

Nothing runs on anyone's computer. The build happens on GitHub's servers; readers
only open a URL.

---

## Classification

Every item gets two labels, so you can cross-filter (for example
*Semiconductors × Capital* for chip-sector funding and M&A).

**Eight sectors**
`AI` · `Robotics` · `Semiconductors` · `Quantum` · `Digital Connectivity` ·
`Biotech` · `Green Tech` · `Space`

**Six news types**

| Type | Covers |
|---|---|
| `Breakthrough` | Research results, technical milestones, first-of-its-kind capability |
| `Product` | Products, models or services actually launched or shipped |
| `Corporate` | Strategy, reorganisation, partnerships, personnel, orders, capacity |
| `Capital` | IPOs, fundraising, M&A, share prices, valuations |
| `Policy` | Regulation, state plans, standards bodies, industry associations |
| `Geopolitics` | Export controls, sanctions, entity lists, tariffs, international cooperation |

---

## Files

| File | Purpose |
|---|---|
| `whitelist.yml` | **The approved outlet list.** Only these domains are collected |
| `sources.yml` | What to search for. **Edit this to add or remove topics** |
| `build.py` | Main pipeline: fetch → filter → de-duplicate → classify → publish |
| `providers.py` | Model adapter. Switches between Claude / DeepSeek / Qwen / Gemini |
| `notify.py` | Failure alerts (Feishu / DingTalk / WeCom / Slack) |
| `template.html` | Page template |
| `verify_files.py` | Pre-flight check that every file contains what it should |
| `compare.py` | Side-by-side quality comparison across model providers |
| `purge_legacy.py` | One-off: remove pre-whitelist archive entries |
| `migrate_archive.py` | One-off: re-classify an archive into the current taxonomy |
| `data/items.json` | The archive |
| `data/health.json` | Snapshot of the most recent run |

---

## Source design: why topic queries, not `site:` queries

This is the least obvious part of the configuration, and getting it wrong silently
destroys coverage.

Google News **largely ignores topic keywords when a query contains many `site:`
operators.** Measured on a space-sector query:

| Query form | In whitelist | Actually on topic |
|---|---|---|
| `(site:a OR site:b …) 火箭 OR 卫星` | 60/60 | **0** |
| `火箭 OR 卫星 (site:a OR site:b …)` | 60/60 | 22 |
| `中国 商业航天 OR 火箭 发射` (no `site:`) | 22/60 | **21** |

A `site:`-scoped query returns whatever those outlets published recently — membership
pages, unrelated political appointments, oil prices — not what you asked for.

Because **fetching is free and only LLM processing costs money**, the right approach is
to retrieve with topic queries and then filter by publisher domain. The 80% that get
discarded cost nothing.

Three source types follow from this:

```yaml
# 1) topic — broad outlets (Xinhua, Caixin, The Paper, ministries).
#    They publish everything, so topic keywords do the filtering.
#    No site: operators. Results are filtered by publisher domain afterwards.
- {id: t_space, type: topic, sector: Space, lang: zh, name_cn: 航天·发射,
   query: "中国 商业航天 OR 火箭 发射 成功 OR 入轨"}

# 2) site — vertical outlets (JW Insights, Taibo, C114).
#    Everything they publish is already on topic, so no keywords needed.
- {id: s_taibo, type: site, domain: taibo.cn, sector: Space, lang: zh}

# 3) rss — native feeds. Cleanest option when one exists.
- {id: r_scmp, type: rss, sector: AI, lang: en, url: "https://www.scmp.com/rss/36/feed"}
```

`sector` is only a fallback — the model assigns the real one.
Add `enabled: false` to disable a source without deleting it.

---

## The whitelist

`whitelist.yml` maps approved domains to display names and a `tier`:

| Tier | Meaning | Used for |
|---|---|---|
| 0 | Government ministries | Most authoritative for policy |
| 1 | Major media | Xinhua, Caixin, SCMP, Reuters, Bloomberg … |
| 2 | Trade press | JW Insights, C114, Taibo, D1EV … |

Two layers enforce it: the fetch step drops any article whose publisher is not on the
list, and `tier` decides which copy survives de-duplication.

The publisher comes from the `<source url="…">` element in the Google News feed.
That is the only place it is available — `entry.link` is a `news.google.com` redirect
with no publisher information in it.

---

## Configuration

**Secrets** (Settings → Secrets and variables → Actions → Secrets)

| Name | When you need it |
|---|---|
| `DASHSCOPE_API_KEY` | Using Qwen (default) |
| `ANTHROPIC_API_KEY` | Using Claude |
| `DEEPSEEK_API_KEY` | Using DeepSeek |
| `GEMINI_API_KEY` | Using Gemini |
| `NOTIFY_WEBHOOK_URL` | Optional — chat-bot alerts |

**Variables** (same screen, Variables tab)

| Name | Default | Notes |
|---|---|---|
| `TRACKER_PROVIDER` | `qwen` | `claude` / `deepseek` / `qwen` / `gemini` |
| `TRACKER_MODEL` | provider default | Override when a model name is retired |
| `TRACKER_CONCURRENCY` | `4` | Lower to `2` if you hit concurrency throttling |
| `TRACKER_MAX_NEW` | `250` | Items processed per run — caps cost |
| `NOTIFY_WEBHOOK_TYPE` | `feishu` | `feishu` / `dingtalk` / `wecom` / `slack` |
| `NOTIFY_PAGE_URL` | — | Included in alert messages |

---

## Failure detection

A green workflow does **not** mean the page updated. Model calls are wrapped in
`try/except`, so if the API key expires every article fails individually while the
workflow still reports success. Four layers guard against that:

1. **`verify_files.py`** runs before the build and fails if any file does not contain
   what it should. This catches copy-paste mistakes, including a `build.py` that has
   been overwritten with something that exits 0 without doing anything.
2. **Self-diagnosis in `build.py`.** The run is marked failed when all sources return
   empty, more than a third of sources return empty, no new item is processed
   successfully, the failure rate exceeds 25%, or a fatal error appears
   (quota exhausted / invalid key / retired model name).
3. **Output timestamp assertion.** After the build, the workflow checks that
   `docs/index.html` was actually rewritten, and blocks the Pages deploy if not.
4. **Staleness banner.** The page shows a red banner if more than 48 hours have
   passed since the last update.

Alerts go to a chat bot if configured, and always to a GitHub Issue labelled
`tracker-alert`. GitHub emails you about issues in your own repository, so that
path needs no setup.

`data/health.json` records the actual error text and a breakdown by cause, so you
usually do not need to open the Actions log.

---

## Notes on Qwen (Alibaba Bailian)

**Rate limits are generous.** `qwen3.7-flash` in the Beijing region allows 30,000 RPM
and 10,000,000 TPM, and *topping up does not change these thresholds* — free and paid
accounts get the same limits. At roughly 200 items a day you use under 0.1% of that,
so a burst of failures is almost never RPM/TPM throttling.

**Three errors that are easy to confuse:**

| Error code | HTTP | Meaning | Action |
|---|---|---|---|
| `Throttling.RateQuota` | 429 | Request rate exceeded | Retry; recovers within a minute |
| `Throttling.Concurrency` | 429 | Too many parallel requests | Lower `TRACKER_CONCURRENCY` |
| `AllocationQuota.FreeTierOnly` | **403** | **Free tier exhausted** | Top up or verify your account — retrying will not help |
| `Arrearage` | 400 | Account in arrears | Top up |
| `InvalidApiKey` | 401 | Bad key | Replace the key |
| `ModelNotFound` | 404 | Model name retired | Set `TRACKER_MODEL` |

On Bailian a `403` means **quota exhausted**, not an authentication problem. Generic
error handling usually files 403 under "bad key" and sends you down the wrong path.
`build.py` distinguishes them and writes the verdict into `health.json`.

**Thinking mode is disabled** (`enable_thinking: false`). Classification and summarisation
are bulk extraction — a reasoning chain adds nothing, and thinking tokens are billed as
output. Some Qwen3 models also reject non-streaming requests when thinking is on.

---

## Cost

Roughly 200 new items a day, about 1,200 input and 250 output tokens each:

| Provider | Model | Per month |
|---|---|---|
| `qwen` | qwen3.7-flash | **≈ $0.4** |
| `gemini` | gemini-3.5-flash | $0 within the free tier (free-tier content is used for training) |
| `deepseek` | deepseek-flash (off-peak) | ≈ $2 |
| `claude` | claude-opus-5 | ≈ $75 |

GitHub Actions is free for public repositories; GitHub Pages is free. The model API
is the only cost.

To spend less: lower `TRACKER_MAX_NEW`, narrow the queries in `sources.yml`, or run
weekly instead of daily.

---

## Known limitations

- **Google News links are redirects** (`news.google.com/...`). They resolve to the
  original article, but the URL itself is not the publisher's. The real publisher is
  recorded separately and shown on the page.
- **Ministry websites are barely indexed by Google News.** Of 60 whitelisted domains,
  a typical run produces items from about 20 — the government domains are mostly
  silent. Capturing ministry announcements directly would require HTML scraping rules,
  which this version does not implement.
- **Summary quality depends on how much text the feed carries.** Many Chinese feeds
  provide only a sentence or two. The model is instructed never to invent detail
  beyond what it was given, so thin input produces thin summaries.
- **Only Claude enforces the output schema.** DeepSeek, Qwen and Gemini offer JSON
  mode but not JSON Schema, so structure is enforced by prompt plus client-side
  validation. Items with an out-of-range sector are discarded rather than misfiled.
