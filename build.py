#!/usr/bin/env python3
"""
China Tech Tracker — 抓取 -> 白名单过滤 -> 去重 -> 模型分类摘要 -> 生成页面

只从 whitelist.yml 列出的域名收录内容。三种信源类型见 sources.yml 顶部说明。

环境变量：
    TRACKER_PROVIDER / TRACKER_MODEL / TRACKER_CONCURRENCY / TRACKER_MAX_NEW
    SOURCES_CSV_URL / NOTIFY_*
"""

import os, re, sys, json, difflib, hashlib, pathlib, collections, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus, urlparse

import yaml, feedparser, requests
from providers import enrich, resolve
import notify

ROOT     = pathlib.Path(__file__).parent
DATA     = ROOT / "data" / "items.json"
HEALTH   = ROOT / "data" / "health.json"
TEMPLATE = ROOT / "template.html"
OUT      = ROOT / "docs" / "index.html"


def env_int(name, default):
    """Actions 里未设置的 vars.X 会传成空字符串，int("") 会崩。统一兜住。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  ! {name}={raw!r} 不是整数，改用默认值 {default}")
        return default


PROVIDER, MODEL, _ = resolve()
CONCURRENCY = max(1, env_int("TRACKER_CONCURRENCY", 4))
MAX_NEW     = max(1, env_int("TRACKER_MAX_NEW", 250))
KEEP_DAYS   = 180
DEDUP_RATIO = 0.75      # 标题相似度阈值
DEDUP_WINDOW = 3        # 只在相差 N 天内比对


# ---------------------------------------------------------------- 白名单

WHITELIST = yaml.safe_load((ROOT / "whitelist.yml").read_text("utf-8"))["domains"]


def match_domain(host):
    """把发布媒体域名映射到白名单条目；不在白名单返回 None。"""
    host = (host or "").lower().removeprefix("www.")
    if not host:
        return None
    for w in WHITELIST:
        if host == w or host.endswith("." + w):
            return w
    return None


def publisher_of(entry, fallback_domain=None):
    """
    取真实发布媒体。Google News 的条目带 <source url="..."> ，
    这是唯一能拿到原始媒体的地方 —— entry.link 是 news.google.com 的跳转链接。
    """
    src = getattr(entry, "source", None)
    host = urlparse(getattr(src, "href", "") or "").netloc if src else ""
    key = match_domain(host) or match_domain(fallback_domain)
    if not key:
        return None
    info = WHITELIST[key]
    return {"source_domain": key,
            "source_cn": info["name_cn"],
            "source_en": info["name_en"],
            "source_tier": info["tier"]}


# ---------------------------------------------------------------- 取数

def load_sources():
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text("utf-8"))
    srcs = [s for s in cfg["sources"] if s.get("enabled", True)]
    csv_url = (os.environ.get("SOURCES_CSV_URL") or "").strip()
    if csv_url:
        try:
            import csv, io
            rows = list(csv.DictReader(io.StringIO(
                requests.get(csv_url, timeout=30).content.decode("utf-8-sig"))))
            for r in rows:
                if r.get("id") and str(r.get("enabled", "true")).lower() != "false":
                    srcs.append({k: v for k, v in r.items() if v})
            print(f"  + 在线表格追加 {len(rows)} 个信源")
        except Exception as e:
            print(f"  ! 在线表格读取失败，忽略：{e}")
    return cfg.get("defaults", {}), srcs


def feed_url(src, defaults):
    window = defaults.get("window", "when:14d")
    if src["type"] == "rss":
        return src["url"]
    if src["type"] == "site":
        q = quote_plus(f"site:{src['domain']} {window}")
    elif src["type"] == "topic":
        q = quote_plus(f"{src['query']} {window}")
    else:
        raise ValueError(f"未知的 type: {src['type']}")
    if src.get("lang") == "en":
        return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"


def clean(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def strip_suffix(title):
    """Google News 会在标题尾部拼 " - 媒体名"，去掉它。"""
    return re.sub(r"\s+-\s+[^-]{1,30}$", "", title).strip()


def fetch(src, defaults):
    """返回 (收录条目, 抓到总数, 被白名单挡下的数量)"""
    out, dropped = [], 0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=int(defaults.get("lookback_days", 14)))
    try:
        parsed = feedparser.parse(feed_url(src, defaults))
    except Exception as e:
        print(f"  ! {src['id']} 抓取失败：{e}")
        return out, 0, 0

    # RSS 源本身就是白名单媒体，用 url 的域名做归属
    rss_host = urlparse(src["url"]).netloc if src["type"] == "rss" else None
    limit = int(src.get("max_items_per_run", defaults.get("max_items_per_run", 40)))

    for e in parsed.entries[:limit]:
        pub = publisher_of(e, fallback_domain=src.get("domain") or rss_host)
        if not pub:                                   # 硬过滤：不在白名单直接丢
            dropped += 1
            continue
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if getattr(e, key, None):
                published = dt.datetime(*getattr(e, key)[:6], tzinfo=dt.timezone.utc)
                break
        if published and published < cutoff:
            continue
        link = getattr(e, "link", "")
        title = strip_suffix(clean(getattr(e, "title", "")))
        if not link or len(title) < 6:
            continue
        item = {
            "id":        hashlib.sha1(link.encode()).hexdigest()[:16],
            "url":       link,
            "title_cn":  title,
            "raw":       clean(getattr(e, "summary", ""))[:1500],
            "date":      (published or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d"),
            "sector":    src.get("sector", "AI"),
            "news_type": "Corporate",
        }
        item.update(pub)
        out.append(item)

    tag = f"{len(out):>3} 收录"
    if dropped:
        tag += f" / {dropped} 条非白名单已丢弃"
    print(f"  · {src['id']:<16}{tag}")
    return out, len(parsed.entries), dropped


# ---------------------------------------------------------------- 去重

def norm_title(t):
    return re.sub(r"[\s，。、！？：；“”‘’（）()《》\-—_|｜\[\]【】\"'!?.,:;]+", "", t)


def dedupe(items):
    """
    同一件事被多家报道时只留一条，优先级：tier 小的（政府 > 大媒体 > 垂直）,
    其次标题更长（通常信息更完整），最后日期更早（首发）。
    只在日期相近的条目间比对，避免 O(n²) 全量比较。
    """
    by_day = collections.defaultdict(list)
    for it in items:
        by_day[it["date"]].append(it)

    keep, dropped = [], []
    seen = set()
    days = sorted(by_day)
    for i, day in enumerate(days):
        # 候选池：本日 + 后面 DEDUP_WINDOW 天
        pool = []
        for d2 in days[i:i + DEDUP_WINDOW + 1]:
            pool += by_day[d2]
        for a in by_day[day]:
            if a["id"] in seen:
                continue
            cluster = [a]
            na = norm_title(a["title_cn"])
            for b in pool:
                if b["id"] == a["id"] or b["id"] in seen:
                    continue
                nb = norm_title(b["title_cn"])
                # 便宜的预筛：长度差太大就不用算相似度了
                if abs(len(na) - len(nb)) > max(len(na), len(nb)) * 0.5:
                    continue
                if difflib.SequenceMatcher(None, na, nb).ratio() >= DEDUP_RATIO:
                    cluster.append(b)
            cluster.sort(key=lambda x: (x.get("source_tier", 9),
                                        -len(x.get("title_cn", "")), x["date"]))
            keep.append(cluster[0])
            for x in cluster:
                seen.add(x["id"])
            dropped += cluster[1:]
    return keep, dropped


# ---------------------------------------------------------------- 健康检查

def diagnose(sources, per_source, fresh, n_todo, n_done, errors):
    dead = [k for k, v in per_source.items() if v == 0]
    problems = []
    if not fresh:
        problems.append(f"所有 {len(sources)} 个信源都没抓到内容 —— 网络或抓取逻辑故障")
    elif len(dead) > len(sources) / 3:
        problems.append(f"{len(dead)}/{len(sources)} 个信源返回空：{', '.join(dead[:8])}")
    if n_todo and not n_done:
        problems.append(f"有 {n_todo} 条新内容，一条都没加工成功")
    elif n_todo and len(errors) > n_todo * 0.25:
        problems.append(f"加工失败率 {len(errors)}/{n_todo}")

    if errors:
        kinds = collections.Counter()
        for e in errors:
            low = e.lower()
            if ("freetieronly" in low or ("free tier" in low and "exhausted" in low)
                    or "arrearage" in low or "insufficient" in low or "balance" in low):
                kinds["额度耗尽/欠费（需充值或实名认证）"] += 1
            elif ("ratelimited" in low or "429" in low or "throttling" in low
                    or "rate limit" in low or "too many concurrent" in low
                    or "allocated quota exceeded" in low):
                kinds["限流(429，可重试)"] += 1
            elif "invalidapikey" in low or "401" in low or "invalid api" in low or "not authorized" in low:
                kinds["key 无效或无权限"] += 1
            elif "modelnotfound" in low or ("model" in low and ("not exist" in low or "404" in low)):
                kinds["型号名无效"] += 1
            elif "板块越界" in e:
                kinds["分类越界(已丢弃)"] += 1
            elif "timeout" in low or "timed out" in low:
                kinds["超时"] += 1
            else:
                kinds["其他"] += 1
        top = "，".join(f"{k}×{v}" for k, v in kinds.most_common())
        if problems:
            problems.append(f"错误构成：{top}")
        elif (kinds.get("额度耗尽/欠费（需充值或实名认证）") or kinds.get("key 无效或无权限")
              or kinds.get("型号名无效")):
            problems.append(f"出现致命错误：{top}")
    return problems, dead


# ---------------------------------------------------------------- 主流程

def main():
    defaults, sources = load_sources()
    print(f"抓取 {len(sources)} 个信源（白名单 {len(WHITELIST)} 个域名）…")
    per_source, fresh, raw_total, raw_dropped = {}, [], 0, 0
    for src in sources:
        got, n_all, n_drop = fetch(src, defaults)
        per_source[src["id"]] = len(got)
        fresh += got
        raw_total += n_all
        raw_dropped += n_drop
    print(f"\n抓到 {raw_total} 条，非白名单丢弃 {raw_dropped} 条，留下 {len(fresh)} 条")

    archive = json.loads(DATA.read_text("utf-8")) if DATA.exists() else []
    known = {i["id"] for i in archive}
    new_items = list({i["id"]: i for i in fresh if i["id"] not in known}.values())

    # 新条目内部先去一次重，避免把同一件事的多个版本都送去加工（省钱）
    new_items, dup_pre = dedupe(new_items)
    if dup_pre:
        print(f"同题去重：加工前合并掉 {len(dup_pre)} 条")

    todo = sorted(new_items, key=lambda i: i["date"], reverse=True)
    skipped = max(0, len(todo) - MAX_NEW)
    todo = todo[:MAX_NEW]
    print(f"新内容 {len(todo) + skipped} 条"
          f"{f'（本次处理最新 {MAX_NEW} 条，其余 {skipped} 条留到下次）' if skipped else ''}")
    print(f"送 {PROVIDER}/{MODEL} 加工，并发 {CONCURRENCY}…")

    done, errors = [], []
    if todo:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for item, err in pool.map(enrich, todo):
                if item:
                    done.append(item)
                elif err:
                    errors.append(err)
    print(f"通过 {len(done)} 条 | 失败 {len(errors)} 条 | "
          f"判定不相关 {len(todo) - len(done) - len(errors)} 条")

    keep_from = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    merged = [i for i in archive + done if i["date"] >= keep_from]
    merged, dup_post = dedupe(merged)          # 与历史存档之间也去一次重
    merged.sort(key=lambda i: (i["date"], i.get("source_en", "")), reverse=True)
    if dup_post:
        print(f"同题去重：与存档合并时再去掉 {len(dup_post)} 条")

    problems, dead = diagnose(sources, per_source, fresh, len(todo), len(done), errors)
    now = dt.datetime.now(dt.timezone.utc)

    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps({
        "last_run": now.isoformat(timespec="seconds"),
        "status":   "fail" if problems else ("warn" if dead else "ok"),
        "provider": PROVIDER, "model": MODEL, "concurrency": CONCURRENCY,
        "sources":  {"total": len(sources), "empty": len(dead), "empty_ids": dead},
        "items":    {"raw_fetched": raw_total, "off_whitelist_dropped": raw_dropped,
                     "in_whitelist": len(fresh), "deduped_before_enrich": len(dup_pre),
                     "new": len(todo), "deferred": skipped,
                     "enriched": len(done), "failed": len(errors),
                     "deduped_on_merge": len(dup_post), "archive_total": len(merged)},
        "by_sector":    dict(collections.Counter(i["sector"] for i in merged).most_common()),
        "by_news_type": dict(collections.Counter(i.get("news_type", "?") for i in merged).most_common()),
        "by_source":    dict(collections.Counter(i.get("source_cn", "?") for i in merged).most_common()),
        "problems": problems,
        "sample_errors": list(dict.fromkeys(errors))[:5],
    }, ensure_ascii=False, indent=1), "utf-8")

    DATA.write_text(json.dumps(merged, ensure_ascii=False, indent=1), "utf-8")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        TEMPLATE.read_text("utf-8")
            .replace("/*DATA*/[]", json.dumps(merged, ensure_ascii=False))
            .replace("{{UPDATED}}", now.strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{UPDATED_ISO}}", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "utf-8")

    print(f"\n存档 {len(merged)} 条 -> {OUT}")
    print("  板块:", dict(collections.Counter(i["sector"] for i in merged).most_common()))
    print("  媒体:", dict(collections.Counter(i.get("source_cn","?") for i in merged).most_common(8)))

    if problems:
        print("\n!! 本次运行异常：")
        for p in problems:
            print("   -", p)
        if errors:
            print("   原始错误样本：")
            for e in list(dict.fromkeys(errors))[:3]:
                print("     ·", e)
        notify.send("抓取异常，页面可能已停更",
                    [f"· {p}" for p in problems] + [f"（{PROVIDER}/{MODEL}）"])
        sys.exit(1)


if __name__ == "__main__":
    main()
