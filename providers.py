"""
模型适配层 —— claude / deepseek / qwen / gemini 四家可切换。

环境变量：
    TRACKER_PROVIDER   claude | deepseek | qwen | gemini
    TRACKER_MODEL      覆盖具体型号
    TRACKER_CONCURRENCY  并发数（默认 4；限流严重时调到 2）

分类采用两个维度：
    sector     8 个产业板块
    news_type  6 类新闻性质 —— 新技术 / 新产品 / 公司动态 / 资本运作 / 监管政策 / 国际关系
这样「半导体的并购案」和「半导体的技术突破」可以分别筛出来。
"""
import os, json, time, random, datetime as dt

SECTORS = ["AI", "Robotics", "Semiconductors", "Quantum",
           "Digital Connectivity", "Biotech", "Green Tech", "Space"]

NEWS_TYPES = [
    "Breakthrough",   # 新技术、研究成果、技术里程碑
    "Product",        # 新产品、新型号、商业发布
    "Corporate",      # 公司战略、重组、合作、人事、订单、产能
    "Capital",        # IPO、融资、并购、股价、估值
    "Policy",         # 监管、国家规划、标准、行业组织、政府合作
    "Geopolitics",    # 出口管制、制裁、国际合作与冲突
]

PROVIDERS = {
    "claude": {
        "kind": "anthropic", "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-opus-5", "json": "schema",
    },
    "deepseek": {
        "kind": "openai", "key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash", "json": "object",
    },
    "qwen": {
        "kind": "openai", "key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-flash", "json": "object",
        # 分类摘要属于批量抽取，不需要思考链。显式关掉有两个好处：
        #   1) 省钱 —— 思考过程按 output token 计费，这里纯属浪费
        #   2) 部分 Qwen3 型号在「非流式 + 思考」下会返回 400
        "extra_body": {"enable_thinking": False},
    },
    "gemini": {
        "kind": "openai", "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash", "json": "object",
    },
}

PRICES = {   # USD / 百万 token（输入, 输出）
    "claude-opus-5":    (5.00, 25.00),
    "claude-sonnet-5":  (2.00, 10.00),
    "claude-haiku-4-5": (1.00,  5.00),
    "deepseek-flash":   (0.30,  1.20),   # 高峰价，平峰减半
    "deepseek-v4-pro":  (1.32,  3.96),
    "qwen3.7-flash":    (0.03,  0.13),
    "qwen3.7-plus":     (0.28,  1.13),
    "gemini-3.5-flash": (0.00,  0.00),
}

SYSTEM_BASE = f"""You classify and summarise Chinese and English tech-news items for a
researcher studying Chinese innovation. Reply with a single json object, nothing else.

Fields:
- title_en: faithful English title. If the source is already English, clean it up but keep the meaning.
- title_cn: the Chinese title. If the source is English and you have no reliable Chinese title, return "".
- summary_en: under 100 English words. What happened, who did it, the key numbers.
  No hype, no speculation, no "this shows that". If the text is too thin, say only what is known.
- sector: exactly one of {SECTORS}
    AI = models, chips-agnostic software, AI applications, data centres, compute demand
    Robotics = humanoids, embodied AI, industrial and service robots, autonomous machines
    Semiconductors = chips, fabs, equipment, materials, memory, packaging, EDA
    Quantum = quantum computing, communication, sensing, metrology
    Digital Connectivity = 5G/6G, telecom carriers, network infrastructure, satellite comms, standards
    Biotech = drugs, medical devices, clinical trials, life sciences
    Green Tech = solar, wind, batteries, storage, hydrogen, EVs, grid, climate industry
    Space = launch vehicles, satellites, constellations, orbital computing, space agencies
- news_type: exactly one of {NEWS_TYPES}
    Breakthrough = research result, technical milestone, first-of-its-kind capability
    Product = a product, model or service actually launched or shipped
    Corporate = strategy, reorganisation, partnership, personnel, orders, capacity, operations
    Capital = IPO, fundraising, M&A, share price, valuation, investment
    Policy = regulation, state plan, standards body, industry association, government cooperation
    Geopolitics = export controls, sanctions, entity lists, tariffs, international conflict or cooperation
- companies: company or institution names the item is actually about, in English, max 5.
  Use the standard English name (中芯国际 -> SMIC, 长鑫存储 -> CXMT). Empty list if none.
- is_relevant: false if the item is NOT about Chinese technology, industry, policy or markets
  in one of the eight sectors above. Reject ads, sponsored content, listicles, site
  navigation pages, personnel news unrelated to tech, general economy, sports, and
  foreign news with no China angle. Be strict: a wrong sector is worse than a dropped item.

Never invent facts that are not in the supplied text."""

SHAPE_HINT = "\n\nReturn json in exactly this shape:\n" + json.dumps(
    {"title_en": "...", "title_cn": "...", "summary_en": "...", "sector": "Semiconductors",
     "news_type": "Capital", "companies": ["SMIC"], "is_relevant": True}, ensure_ascii=False)

SCHEMA = {
    "type": "object",
    "properties": {
        "title_en":    {"type": "string"},
        "title_cn":    {"type": "string"},
        "summary_en":  {"type": "string"},
        "sector":      {"type": "string", "enum": SECTORS},
        "news_type":   {"type": "string", "enum": NEWS_TYPES},
        "companies":   {"type": "array", "items": {"type": "string"}},
        "is_relevant": {"type": "boolean"},
    },
    "required": ["title_en", "title_cn", "summary_en", "sector",
                 "news_type", "companies", "is_relevant"],
    "additionalProperties": False,
}


def deepseek_is_peak(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)


def price_for(model):
    inp, out = PRICES.get(model, (0.0, 0.0))
    if model.startswith("deepseek") and not deepseek_is_peak():
        inp, out = inp / 2, out / 2
    return inp, out


def resolve(provider=None, model=None):
    provider = provider or os.environ.get("TRACKER_PROVIDER") or "qwen"
    if provider not in PROVIDERS:
        raise ValueError(f"未知 provider: {provider}，可选 {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    return provider, (model or os.environ.get("TRACKER_MODEL") or cfg["model"]), cfg


def _validate(d):
    for k in SCHEMA["required"]:
        if k not in d:
            raise ValueError(f"缺字段 {k}")
    if d["sector"] not in SECTORS:
        raise ValueError(f"板块越界: {d['sector']!r}")      # 宁可丢弃，也不错分
    if d["news_type"] not in NEWS_TYPES:
        d["news_type"] = "Corporate"
    if not isinstance(d.get("companies"), list):
        d["companies"] = []
    d["companies"] = [str(c).strip() for c in d["companies"] if str(c).strip()][:5]
    d["is_relevant"] = bool(d["is_relevant"])
    for k in ("title_en", "title_cn", "summary_en"):
        d[k] = str(d.get(k) or "")
    return d


def _user_prompt(it):
    return (f"Source: {it['source_cn']} / {it['source_en']}\n"
            f"Date: {it['date']}\nURL: {it['url']}\n"
            f"Title: {it['title_cn']}\n\nText:\n{it['raw']}")


class RateLimited(Exception):
    """限流 —— 和 key 失效区分开，便于诊断。"""


def _call_anthropic(item, model, cfg):
    import anthropic
    r = anthropic.Anthropic().messages.create(
        model=model, max_tokens=1200, system=SYSTEM_BASE,
        messages=[{"role": "user", "content": _user_prompt(item)}],
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    text = next(b.text for b in r.content if b.type == "text")
    return json.loads(text), (r.usage.input_tokens, r.usage.output_tokens)


# 百炼的限流码：Throttling.RateQuota / AllocationQuota / Concurrency，均为 429。
# 注意 403-AllocationQuota.FreeTierOnly 是「免费额度耗尽」，不是限流，重试没有意义。
_RATE_HINTS = ("429", "rate limit", "ratelimit", "too many requests",
               "resource_exhausted", "throttling", "requests rate limit",
               "allocated quota exceeded", "too many concurrent",
               "request rate increased too quickly", "limit_requests", "requests per")
_FATAL_HINTS = ("freetieronly", "free tier of the model has been exhausted",
                "arrearage", "invalidapikey", "modelnotfound", "not authorized")


def _call_openai_compatible(item, model, cfg, provider):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ[cfg["key_env"]], base_url=cfg["base_url"],
                    timeout=60.0, max_retries=0)   # 自己管重试，SDK 不要插手
    last = None
    for attempt in range(5):
        try:
            r = client.chat.completions.create(
                model=model, max_tokens=1200,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYSTEM_BASE + SHAPE_HINT},
                          {"role": "user",   "content": _user_prompt(item)}],
                **({"extra_body": cfg["extra_body"]} if cfg.get("extra_body") else {}),
            )
            u = r.usage
            return json.loads(r.choices[0].message.content or "{}"), \
                   (u.prompt_tokens, u.completion_tokens)
        except Exception as e:
            last = e
            msg = str(e).lower()
            if any(h in msg for h in _FATAL_HINTS):
                raise                               # 额度耗尽/欠费/key 无效 —— 重试无用
            if any(h in msg for h in _RATE_HINTS):
                # 指数退避 + 抖动：1.5s, 3s, 6s, 12s（避免所有线程同时重试）
                wait = 1.5 * (2 ** attempt) + random.uniform(0, 1.5)
                time.sleep(wait)
                continue
            raise                                   # 非限流错误立刻抛出，不浪费重试
    raise RateLimited(f"重试 5 次仍被限流: {last}")


def enrich(item, provider=None, model=None, with_usage=False):
    """返回 (item|None, usage|None, err|None)；err 为 None 表示没出错。"""
    provider, model, cfg = resolve(provider, model)
    try:
        if cfg["kind"] == "anthropic":
            data, usage = _call_anthropic(item, model, cfg)
        else:
            data, usage = _call_openai_compatible(item, model, cfg, provider)
        data = _validate(data)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        return (None, None, err) if with_usage else (None, err)

    if not data["is_relevant"]:
        return (None, usage, None) if with_usage else (None, None)

    item.update({k: data[k] for k in ("title_en", "title_cn", "summary_en",
                                      "sector", "news_type", "companies")})
    item.pop("raw", None)
    return (item, usage, None) if with_usage else (item, None)


DEFAULT_MODEL = {k: v["model"] for k, v in PROVIDERS.items()}
