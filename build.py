#!/usr/bin/env python3
"""
China Tech Tracker — 抓取 -> 去重 -> 模型翻译/摘要/分类 -> 生成静态页面

运行：  python build.py
需要：  对应厂商的 API key（见 providers.py / README）
产出：  data/items.json    累积存档，可直接转 Excel
        data/health.json   每次运行的健康快照
        docs/index.html    GitHub Pages 发布的页面

异常时会发通知并以退出码 1 结束，让 Actions 标红。
"""

import os, re, sys, json, hashlib, pathlib, datetime as dt
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import yaml, feedparser, requests
from providers import enrich, DEFAULT_MODEL
import notify

ROOT     = pathlib.Path(__file__).parent
DATA     = ROOT / "data" / "items.json"
HEALTH   = ROOT / "data" / "health.json"
TEMPLATE = ROOT / "template.html"
OUT      = ROOT / "docs" / "index.html"

PROVIDER = os.environ.get("TRACKER_PROVIDER") or "claude"   # claude|deepseek|qwen|gemini
MODEL    = os.environ.get("TRACKER_MODEL") or DEFAULT_MODEL[PROVIDER]


# ---------------------------------------------------------------- 取数

def load_sources():
    cfg = yaml.safe_load((ROOT / "sources.yml").read_text("utf-8"))
    srcs = [s for s in cfg["sources"] if s.get("enabled", True)]

    # 可选：同事通过在线表格（腾讯文档/飞书/Google Sheet 发布为 CSV）增删信源，
    # 他们不需要 GitHub 账号。把 CSV 地址设成仓库变量 SOURCES_CSV_URL 即可。
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
        days=int(defaults.get("lookback_days", 7)))
    try:
        parsed = feedparser.parse(feed_url(src))
    except Exception as e:
        print(f"  ! {src['id']} 抓取失败：{e}")
        return out

    limit = int(src.get("max_items_per_run", defaults.get("max_items_per_run", 25)))
    for e in parsed.entries[:limit]:
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if getattr(e, key, None):
                published = dt.datetime(*getattr(e, key)[:6], tzinfo=dt.timezone.utc)
                break
        if published and published < cutoff:
            continue
        link = getattr(e, "link", "")
        if not link:
            continue
        out.append({
            "id":        hashlib.sha1(link.encode()).hexdigest()[:16],
            "url":       link,
            "title_cn":  clean(getattr(e, "title", "")),
            "raw":       clean(getattr(e, "summary", ""))[:1500],
            "date":      (published or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%d"),
            "source_cn": src.get("name_cn", src["id"]),
            "source_en": src.get("name_en", src["id"]),
            "sector":    src.get("default_sector", "综合"),
        })
    print(f"  · {src['id']}: {len(out)} 条")
    return out


# ---------------------------------------------------------------- 健康检查

def diagnose(sources, per_source, fresh, todo, done, failed):
    """区分两种故障：跑挂了（Actions 自己会红），和跑通了但没产出（静默停更）。"""
    dead = [k for k, v in per_source.items() if v == 0]
    problems = []

    if not fresh:
        problems.append(
            f"所有 {len(sources)} 个信源都没抓到内容 —— 大概率是网络或抓取逻辑坏了")
    elif len(dead) > len(sources) / 2:
        problems.append(
            f"{len(dead)}/{len(sources)} 个信源返回空：{', '.join(dead[:8])}")

    if todo and not done:
        problems.append(
            f"有 {len(todo)} 条新内容，但一条都没加工成功 —— 大概率是 {PROVIDER} 的 "
            f"key 失效、额度用尽，或型号名 {MODEL} 已下线")
    elif todo and failed > len(todo) * 0.5:
        problems.append(f"加工失败率过半：{failed}/{len(todo)} 条失败")

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
    todo = {i["id"]: i for i in fresh if i["id"] not in known}
    print(f"\n新条目 {len(todo)} 条，送 {PROVIDER}/{MODEL} 加工…")

    with ThreadPoolExecutor(max_workers=6) as pool:
        raw = list(pool.map(enrich, todo.values()))
    done = [r for r in raw if r]
    failed = sum(1 for r in raw if r is None)
    print(f"通过相关性筛选 {len(done)} 条（{failed} 条被丢弃或加工失败）")

    keep_from = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    merged = sorted([i for i in archive + done if i["date"] >= keep_from],
                    key=lambda i: (i["date"], i["source_en"]), reverse=True)

    problems, dead = diagnose(sources, per_source, fresh, todo, done, failed)
    now = dt.datetime.now(dt.timezone.utc)

    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps({
        "last_run": now.isoformat(timespec="seconds"),
        "status":   "fail" if problems else ("warn" if dead else "ok"),
        "provider": PROVIDER,
        "model":    MODEL,
        "sources":  {"total": len(sources), "empty": len(dead), "empty_ids": dead},
        "items":    {"fetched": len(fresh), "new": len(todo), "enriched": len(done),
                     "failed": failed, "archive_total": len(merged)},
        "problems": problems,
    }, ensure_ascii=False, indent=1), "utf-8")

    DATA.write_text(json.dumps(merged, ensure_ascii=False, indent=1), "utf-8")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        TEMPLATE.read_text("utf-8")
            .replace("/*DATA*/[]", json.dumps(merged, ensure_ascii=False))
            .replace("{{UPDATED}}", now.strftime("%Y-%m-%d %H:%M UTC"))
            .replace("{{UPDATED_ISO}}", now.strftime("%Y-%m-%dT%H:%M:%SZ")),
        "utf-8")
    print(f"\n完成：存档 {len(merged)} 条 -> {OUT}")

    if problems:
        print("\n!! 本次运行异常：")
        for p in problems:
            print("   -", p)
        notify.send("抓取异常，页面可能已停更",
                    [f"· {p}" for p in problems] + [f"（{PROVIDER}/{MODEL}）"])
        sys.exit(1)          # 让 Actions 标红，触发 workflow 里的兜底通知


if __name__ == "__main__":
    main()
