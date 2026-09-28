#!/usr/bin/env python3
"""
China Tech Tracker — 抓取 -> 去重 -> 模型分类/摘要 -> 生成静态页面

产出：
    data/items.json    累积存档
    data/health.json   每次运行的健康快照（含真实错误信息）
    docs/index.html    GitHub Pages 页面

环境变量：
    TRACKER_PROVIDER      claude | deepseek | qwen | gemini
    TRACKER_MODEL         覆盖型号
    TRACKER_CONCURRENCY   并发数，默认 4。限流严重时设成 2。
    TRACKER_MAX_NEW       单次最多加工多少条新内容，默认 250。
                          防止改了信源后一次涌入几百条把额度打爆。
"""

import os, re, sys, json, hashlib, pathlib, collections, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import yaml, feedparser, requests
from providers import enrich, resolve, SECTORS, NEWS_TYPES
import notify

ROOT     = pathlib.Path(__file__).parent
DATA     = ROOT / "data" / "items.json"
HEALTH   = ROOT / "data" / "health.json"
TEMPLATE = ROOT / "template.html"
OUT      = ROOT / "docs" / "index.html"

PROVIDER, MODEL, _ = resolve()
CONCURRENCY = int(os.environ.get("TRACKER_CONCURRENCY", "4"))
MAX_NEW     = int(os.environ.get("TRACKER_MAX_NEW", "250"))
KEEP_DAYS   = 180


# ---------------------------------------------------------------- 取数

def load_sources():
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text("utf-8"))
    srcs = [s for s in cfg["sources"] if s.get("enabled", True)]

    csv_url = os.environ.get("SOURCES_CSV_URL", "").strip()
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


def feed_url(src):
    if src["type"] == "rss":
        return src["url"]
    if src["type"] == "gnews":
        q = quote_plus(src["query"])
        if src.get("lang") == "en":
            return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        return f"https://news.google.com/rss/search?q={q}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    raise ValueError(f"未知的 type: {src['type']}")


def clean(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")).strip()


def fetch(src, defaults):
    out = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=int(defaults.get("lookback_days", 14)))
    try:
        parsed = feedparser.parse(feed_url(src))
    except Exception as e:
        print(f"  ! {src['id']} 抓取失败：{e}")
        return out

    limit = int(src.get("max_items_per_run", defaults.get("max_items_per_run", 30)))
    for e in parsed.entries[:limit]:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if getattr(e, key, None):
                published = dt.datetime(*getattr(e, key)[:6], tzinfo=dt.timezone.utc)
                break
        if published and published < cutoff:
            continue
        link = getattr(e, "link", "")
        title = clean(getattr(e, "title", ""))
        if not link or len(title) < 6:
            continue
        out.append({
            "id":        hashlib.sha1(link.encode()).hexdigest()[:16],
            "url":       link,
            "title_cn":  title,
            "raw":       clean(getattr(e, "summary", ""))[:1500],
            "date":      (published or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d"),
            "source_cn": src.get("name_cn", src["id"]),
            "source_en": src.get("name_en", src["id"]),
            "sector":    src.get("default_sector", "AI"),
            "news_type": "Corporate",
        })
    print(f"  · {src['id']:<22}{len(out):>4} 条")
    return out


# ---------------------------------------------------------------- 健康检查

def diagnose(sources, per_source, fresh, n_todo, n_done, errors):
    """把「跑通了但没产出」这种静默故障也判成失败。"""
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

    # 把错误按类型归并，直接说清楚是限流还是 key 的问题
    if errors:
        kinds = collections.Counter()
        for e in errors:
            low = e.lower()
            if "ratelimited" in low or "429" in low or "rate limit" in low or "throttl" in low:
                kinds["限流(429)"] += 1
            elif "auth" in low or "401" in low or "403" in low or "api key" in low or "invalid_api" in low:
                kinds["认证失败(key 无效)"] += 1
            elif "quota" in low or "insufficient" in low or "balance" in low or "arrears" in low:
                kinds["额度不足"] += 1
            elif "model" in low and ("not found" in low or "not exist" in low or "404" in low):
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
        elif kinds.get("认证失败(key 无效)") or kinds.get("额度不足") or kinds.get("型号名无效"):
            problems.append(f"出现致命错误：{top}")
    return problems, dead


# ---------------------------------------------------------------- 主流程

def main():
    defaults, sources = load_sources()
    print(f"抓取 {len(sources)} 个信源…")
    per_source, fresh = {}, []
    for src in sources:
        got = fetch(src, defaults)
        per_source[src["id"]] = len(got)
        fresh += got

    archive = json.loads(DATA.read_text("utf-8")) if DATA.exists() else []
    known = {i["id"] for i in archive}

    # 去重 + 按日期倒序取前 MAX_NEW 条，保证最新的先进
    todo = sorted({i["id"]: i for i in fresh if i["id"] not in known}.values(),
                  key=lambda i: i["date"], reverse=True)
    skipped = max(0, len(todo) - MAX_NEW)
    todo = todo[:MAX_NEW]
    print(f"\n抓到 {len(fresh)} 条，其中新内容 {len(todo) + skipped} 条"
          f"{f'（本次只处理最新 {MAX_NEW} 条，其余 {skipped} 条留到下次）' if skipped else ''}")
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
    merged = sorted([i for i in archive + done if i["date"] >= keep_from],
                    key=lambda i: (i["date"], i.get("source_en", "")), reverse=True)

    problems, dead = diagnose(sources, per_source, fresh, len(todo), len(done), errors)
    now = dt.datetime.now(dt.timezone.utc)

    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps({
        "last_run": now.isoformat(timespec="seconds"),
        "status":   "fail" if problems else ("warn" if dead else "ok"),
        "provider": PROVIDER, "model": MODEL,
        "concurrency": CONCURRENCY,
        "sources":  {"total": len(sources), "empty": len(dead), "empty_ids": dead},
        "items":    {"fetched": len(fresh), "new": len(todo), "deferred": skipped,
                     "enriched": len(done), "failed": len(errors),
                     "archive_total": len(merged)},
        "by_sector":    dict(collections.Counter(i["sector"] for i in merged).most_common()),
        "by_news_type": dict(collections.Counter(i.get("news_type", "?") for i in merged).most_common()),
        "problems": problems,
        "sample_errors": list(dict.fromkeys(errors))[:5],   # 去重后的前 5 条原始错误
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
    print("  类型:", dict(collections.Counter(i.get("news_type","?") for i in merged).most_common()))

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
