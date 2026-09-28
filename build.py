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
import inspect
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote_plus

import yaml, feedparser, requests
import providers
from providers import enrich, DEFAULT_MODEL
import notify

ROOT     = pathlib.Path(__file__).parent
DATA     = ROOT / "data" / "items.json"
HEALTH   = ROOT / "data" / "health.json"
TEMPLATE = ROOT / "template.html"
OUT      = ROOT / "docs" / "index.html"

PROVIDER = os.environ.get("TRACKER_PROVIDER") or "claude"   # claude|deepseek|qwen|gemini
MODEL    = os.environ.get("TRACKER_MODEL") or DEFAULT_MODEL[PROVIDER]

# 单次运行最大加工 AI 新闻数量
MAX_ENRICH_ITEMS = int(os.environ.get("MAX_ENRICH_PER_RUN", 100))


# ---------------------------------------------------------------- 精准提示词 (Prompt) 定义

SYSTEM_PROMPT = """
You are a senior tech policy and industry intelligence analyst. Your job is to strictly evaluate, filter, and categorize Chinese technology news.

### 1. RELEVANCE & EVENT TYPES (MUST meet AT LEAST ONE of the following 4 categories):
If the news DOES NOT fit into any of these, set "is_relevant": false.
- [Tech Release]: Launch or technical breakthrough of new technologies, hardware, software, AI models, or scientific achievements.
- [Corporate Dynamics]: Investments, fundraising, IPOs, M&A, regulatory investigations, fines, antitrust actions, or strategic partnerships/joint-ventures.
- [Policy & Regulation]: Government policies, laws, sector standards, subsidies, guidelines, or regulatory enforcement across tech industries.
- [Geopolitics & Global Trade]: US-China tech competition, export controls, sanctions, Entity List updates, international compliance, or cross-border tech cooperation.

### 2. EXCLUSION RULES (HARD FILTERS - Mark "is_relevant": false):
- Exclude pure CSR, environmental propaganda, tree planting, desertification control, manual conservation, or traditional forestry/agriculture (e.g., 治沙, 植树造林).
- Exclude routine political speeches, non-tech administrative meetings, or general economic news without direct tech industry impact.
- Exclude consumer product unboxings, opinion pieces, user manuals, or paid marketing advertorials with no strategic industry value.

### 3. INDUSTRY SECTOR TAXONOMY & PRIORITY RULES:
Assign the news to EXACTLY ONE of the following sectors based on these guidelines:

1. "Artificial Intelligence":
   - LLMs, multimodal AI, AI agents, enterprise AI applications, compute infrastructure/clusters, AI governance & algorithm regulation.
2. "Robotics":
   - Embodied AI (具身智能), humanoid robots, autonomous systems, spatial intelligence, advanced industrial automation & manipulators.
3. "Semiconductors":
   - Chip design, manufacturing (fabs), EDA tools, lithography equipment, advanced packaging, IC materials, supply chain security.
4. "Quantum Computing":
   - Quantum processors, quantum key distribution (QKD), quantum algorithms, quantum sensing, quantum cryptography.
5. "Digital Connectivity":
   - 5G/6G, optical fiber/cables, telecom network infrastructure, communication satellite constellations, space data centers, subsea cables.
   - PRIORITY RULE: If a satellite, rocket, or space asset is primarily designed for broadband internet, telecommunications, or space-based networking/data, it MUST be categorized under "Digital Connectivity" (NOT Aerospace).
6. "Green Tech":
   - Commercial & industrial clean technologies ONLY — Electric Vehicles (EVs), battery technology/chemistry, solar/wind technology, hydrogen energy, smart grid, nuclear fusion/fission, mega-scale clean energy projects.
   - STRICTLY EXCLUDE: Traditional environmental protection, tree planting, or desertification control.
7. "Biotech":
   - Innovative pharmaceutical R&D, synthetic biology, Brain-Computer Interfaces (BCI), gene editing (CRISPR), advanced medical devices, bio-computing.
8. "Aerospace":
   - Commercial launch vehicles (rockets), non-communication satellites (e.g., remote sensing, Earth observation), commercial aviation (C919, eVTOL), space exploration hardware.
   - (Remember the priority rule: Communication/networking satellites and space data centers go to "Digital Connectivity").
9. "General Tech":
   - Cross-cutting digital transformation, overarching policy frameworks, or news that meets relevance criteria but spans multiple categories equally.

### 4. CRITICAL OUTPUT FORMAT RULES (JSON ONLY):
You MUST return a valid JSON object containing ALL 6 keys listed below under EVERY condition.
NEVER omit "title_en", "summary_en", "sector", or "companies", EVEN WHEN "is_relevant" IS FALSE.

{
  "is_relevant": true, // boolean: true or false
  "reason": "Brief explanation for the judgment",
  "sector": "Exact Sector Name from the list above",
  "title_en": "Professional concise English title (MANDATORY: translate the original title even if is_relevant is false)",
  "summary_en": "Executive English summary (2-3 sentences)",
  "companies": ["Primary tech companies explicitly involved"]
}
"""

if hasattr(providers, "SYSTEM_PROMPT"):
    providers.SYSTEM_PROMPT = SYSTEM_PROMPT


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
        days=int(defaults.get("lookback_days", 7)))
    try:
        parsed = feedparser.parse(feed_url(src))
    except Exception as e:
        print(f"  ! {src['id']} 抓取失败：{e}")
        return src["id"], out

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
            "sector":    src.get("default_sector", "General Tech"),
        })
    return src["id"], out


# ---------------------------------------------------------------- AI 处理包装

def enrich_item(item):
    """返回规范化字典：{"status": "success"|"filtered"|"failed", ...}"""
    try:
        sig = inspect.signature(enrich)
        if "prompt" in sig.parameters:
            res = enrich(item, prompt=SYSTEM_PROMPT)
        elif "system_prompt" in sig.parameters:
            res = enrich(item, system_prompt=SYSTEM_PROMPT)
        else:
            res = enrich(item)

        if not res or not isinstance(res, dict):
            return {"status": "failed", "reason": "AI 返回空或非合法字典"}

        # 检查是否被 Prompt 判定为无关内容
        if res.get("is_relevant") is False:
            reason = res.get("reason", "不符合科技新闻筛选标准")
            print(f"  [过滤] {item.get('title_cn', '')[:22]}... -> 原因: {reason}")
            return {"status": "filtered", "reason": reason}

        # 规范化 Sector
        valid_sectors = {
            "Artificial Intelligence", "Robotics", "Semiconductors",
            "Quantum Computing", "Digital Connectivity", "Green Tech",
            "Biotech", "Aerospace", "General Tech"
        }
        
        current_sector = res.get("sector")
        if current_sector not in valid_sectors:
            sec_lower = str(current_sector).lower()
            if "ai" in sec_lower or "intelligence" in sec_lower:
                res["sector"] = "Artificial Intelligence"
            elif "robot" in sec_lower or "embodied" in sec_lower:
                res["sector"] = "Robotics"
            elif "semiconductor" in sec_lower or "chip" in sec_lower:
                res["sector"] = "Semiconductors"
            elif "quantum" in sec_lower:
                res["sector"] = "Quantum Computing"
            elif "connectivity" in sec_lower or "telecom" in sec_lower or "5g" in sec_lower or "6g" in sec_lower or "satellite" in sec_lower:
                res["sector"] = "Digital Connectivity"
            elif "green" in sec_lower or "energy" in sec_lower or "ev" in sec_lower:
                res["sector"] = "Green Tech"
            elif "bio" in sec_lower or "pharma" in sec_lower:
                res["sector"] = "Biotech"
            elif "aero" in sec_lower or "space" in sec_lower:
                res["sector"] = "Aerospace"
            else:
                res["sector"] = "General Tech"

        return {"status": "success", "data": res}

    except Exception as e:
        print(f"  ! 加工条目报错 [{item.get('title_cn', '')[:15]}...]: {e}")
        return {"status": "failed", "reason": str(e)}


# ---------------------------------------------------------------- 健康检查

def diagnose(sources, per_source, fresh, todo, done_count, filtered_count, failed_count):
    dead = [k for k, v in per_source.items() if v == 0]
    problems = []

    if not fresh:
        problems.append(
            f"所有 {len(sources)} 个信源都没抓到内容 —— 大概率是网络或抓取逻辑坏了")
    elif len(dead) > len(sources) / 2:
        problems.append(
            f"{len(dead)}/{len(sources)} 个信源返回空：{', '.join(dead[:8])}")

    # 只有当【接口真正的报错数】超过总送审数的一半时才判定为 API 故障
    if todo and failed_count > len(todo) * 0.5:
        problems.append(
            f"AI 接口报错率过半：{failed_count}/{len(todo)} 条失败，请检查 {PROVIDER} Key 额度或型号状态")

    return problems, dead


# ---------------------------------------------------------------- 主流程

def main():
    defaults, sources = load_sources()

    print(f"正在并发抓取 {len(sources)} 个信源…")
    per_source, fresh = {}, []
    
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = pool.map(lambda src: fetch(src, defaults), sources)
        for src_id, got in results:
            per_source[src_id] = len(got)
            fresh += got
            print(f"  · {src_id}: {len(got)} 条")

    archive = json.loads(DATA.read_text("utf-8")) if DATA.exists() else []
    known = {i["id"] for i in archive}
    todo_dict = {i["id"]: i for i in fresh if i["id"] not in known}
    
    todo_list = list(todo_dict.values())
    if len(todo_list) > MAX_ENRICH_ITEMS:
        print(f"\n未加工新条目共 {len(todo_list)} 条，本次优先处理最新的 {MAX_ENRICH_ITEMS} 条…")
        todo_list = todo_list[:MAX_ENRICH_ITEMS]
    else:
        print(f"\n新条目 {len(todo_list)} 条，送 {PROVIDER}/{MODEL} 加工…")

    max_workers = 1 if PROVIDER == "gemini" else 20
    
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        enrich_results = list(pool.map(enrich_item, todo_list))
        
    done = [r["data"] for r in enrich_results if r.get("status") == "success"]
    filtered_count = sum(1 for r in enrich_results if r.get("status") == "filtered")
    failed_count = sum(1 for r in enrich_results if r.get("status") == "failed")
    
    print(f"\n处理完成：存留 {len(done)} 条高质量新闻，正常过滤 {filtered_count} 条无关/噪声条目，接口报错 {failed_count} 条")

    keep_from = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    merged = sorted([i for i in archive + done if i["date"] >= keep_from],
                    key=lambda i: (i["date"], i["source_en"]), reverse=True)

    problems, dead = diagnose(sources, per_source, fresh, todo_list, len(done), filtered_count, failed_count)
    now = dt.datetime.now(dt.timezone.utc)

    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps({
        "last_run": now.isoformat(timespec="seconds"),
        "status":   "fail" if problems else ("warn" if dead else "ok"),
        "provider": PROVIDER,
        "model":    MODEL,
        "sources":  {"total": len(sources), "empty": len(dead), "empty_ids": dead},
        "items":    {"fetched": len(fresh), "new": len(todo_dict), "enriched": len(done),
                     "filtered": filtered_count, "failed": failed_count, "archive_total": len(merged)},
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
            print("    -", p)
        notify.send("抓取异常，页面可能已停更",
                    [f"· {p}" for p in problems] + [f"（{PROVIDER}/{MODEL}）"])
        sys.exit(1)


if __name__ == "__main__":
    main()
