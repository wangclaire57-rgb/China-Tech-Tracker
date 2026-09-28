"""
模型适配层 —— 同一套 prompt，四家可切换：claude / deepseek / qwen / gemini

环境变量：
    TRACKER_PROVIDER   claude | deepseek | qwen（默认建议） | gemini
    TRACKER_MODEL      覆盖具体型号（各家型号名变得很快，留空用下面的默认值）

各家的 key 环境变量见 PROVIDERS 表。除 Claude 外全部走 OpenAI 兼容协议，
所以只需要 openai 这一个 SDK。
"""
import os, json, time, datetime as dt

SECTORS = ["人工智能", "半导体", "数字互联", "绿色科技", "生物医药",
           "航天", "量子", "政策", "市场与交易", "综合"]

PROVIDERS = {
    "claude": {
        "kind": "anthropic", "key_env": "ANTHROPIC_API_KEY",
        "model": "claude-opus-5",
        "json": "schema",          # API 强约束返回结构
    },
    "deepseek": {
        "kind": "openai", "key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "json": "object",          # 只有 json_object，不支持 json_schema
    },
    "qwen": {
        "kind": "openai", "key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen3.7-flash",
        "json": "object",
    },
    "gemini": {
        "kind": "openai", "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash",
        "json": "object",
    },
}

# 每百万 token 价格（USD，输入/输出）
PRICES = {
    "claude-opus-5":     (5.00, 25.00),
    "claude-sonnet-5":   (2.00, 10.00),
    "claude-haiku-4-5": (1.00,  5.00),
    "deepseek-flash":    (0.30,  1.20),   # 高峰价，平峰减半，见 deepseek_is_peak()
    "deepseek-v4-pro":   (1.32,  3.96),   # 同上
    "qwen3.7-flash":     (0.03,  0.13),   # ≤32K 上下文档位
    "qwen3.7-plus":      (0.28,  1.13),   # ≈¥2/¥8，汇率近似
    "gemini-3.5-flash":  (0.00,  0.00),   # 免费额度内为 0；超出后按 Google 当时价目
}

SYSTEM_BASE = f"""You process Chinese and English tech-news items for a researcher who
studies Chinese innovation. You always reply with a single json object, nothing else.

Fields:
- title_en: a faithful English title. If the source title is already English, clean it up but keep its meaning.
- title_cn: the Chinese title. If the source is English and you do not have a reliable Chinese title, return "".
- summary_en: under 100 English words. State what happened, who did it, and the key
  numbers. No hype, no speculation, no "this shows that". If the feed text is too thin
  to summarize accurately, say only what is known.
- sector: exactly one of {SECTORS}
- companies: company or institution names the item is actually about, in English, max 5.
  Use the standard English name (中芯国际 -> SMIC, 长鑫存储 -> CXMT). Empty list if none.
- is_relevant: false if the item is not about Chinese technology, industry, policy or
  markets (ads, sports, unrelated foreign news, listicles).

Never invent facts that are not in the supplied text."""

SHAPE_HINT = "\n\nReturn json in exactly this shape:\n" + json.dumps(
    {"title_en": "...", "title_cn": "...", "summary_en": "...",
     "sector": "人工智能", "companies": ["..."], "is_relevant": True}, ensure_ascii=False)

SCHEMA = {
    "type": "object",
    "properties": {
        "title_en":    {"type": "string"},
        "title_cn":    {"type": "string"},
        "summary_en":  {"type": "string"},
        "sector":      {"type": "string", "enum": SECTORS},
        "companies":   {"type": "array", "items": {"type": "string"}},
        "is_relevant": {"type": "boolean"},
    },
    "required": ["title_en", "title_cn", "summary_en", "sector", "companies", "is_relevant"],
    "additionalProperties": False,
}


def deepseek_is_peak(now=None):
    """DeepSeek 高峰：UTC 周一至周五 01:00-04:00 与 06:00-10:00，平峰价减半。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)


def price_for(model):
    inp, out = PRICES.get(model, (0.0, 0.0))
    if model.startswith("deepseek") and not deepseek_is_peak():
        inp, out = inp / 2, out / 2
    return inp, out


def resolve(provider=None, model=None):
    provider = provider or os.environ.get("TRACKER_PROVIDER") or "claude"
    if provider not in PROVIDERS:
        raise ValueError(f"未知 provider: {provider}，可选 {list(PROVIDERS)}")
    cfg = PROVIDERS[provider]
    return provider, (model or os.environ.get("TRACKER_MODEL") or cfg["model"]), cfg


def _validate(d):
    """没有 schema 强约束的厂商必须自己兜底；有的也走一遍，无害。"""
    for k in SCHEMA["required"]:
        if k not in d:
            raise ValueError(f"缺字段 {k}")
    if d["sector"] not in SECTORS:
        d["sector"] = "综合"
    if not isinstance(d.get("companies"), list):
        d["companies"] = []
    d["companies"] = [str(c) for c in d["companies"]][:5]
    d["is_relevant"] = bool(d["is_relevant"])
    for k in ("title_en", "title_cn", "summary_en"):
        d[k] = str(d.get(k) or "")
    return d


def _user_prompt(it):
    return (f"Source: {it['source_cn']} / {it['source_en']}\n"
            f"Date: {it['date']}\nURL: {it['url']}\n"
            f"Title: {it['title_cn']}\n\nText:\n{it['raw']}")


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


def _call_openai_compatible(item, model, cfg, provider="openai"):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ[cfg["key_env"]], base_url=cfg["base_url"])
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            r = client.chat.completions.create(
                model=model, max_tokens=1200,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYSTEM_BASE + SHAPE_HINT},
                          {"role": "user",   "content": _user_prompt(item)}],
            )
            
            # 仅在仍然选择 Gemini 且是免费层时保留微小延迟，Qwen/DeepSeek 无需强制休眠
            if provider == "gemini":
                time.sleep(12)
            else:
                time.sleep(0.1)  # 保持轻微间隔防止网络冲击
                
            u = r.usage
            return json.loads(r.choices[0].message.content or "{}"), \
                   (u.prompt_tokens, u.completion_tokens)

        except Exception as e:
            err_msg = str(e)
            if ("429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "rate limit" in err_msg.lower()) and attempt < max_retries - 1:
                wait_time = (attempt + 1) * 3 + 2  # 触发偶尔限流时短暂停顿 5s、8s
                print(f"  ![{provider}] 触发速率限制，等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
            else:
                raise e


def enrich(item, provider=None, model=None, with_usage=False):
    provider, model, cfg = resolve(provider, model)
    try:
        if cfg["kind"] == "anthropic":
            data, usage = _call_anthropic(item, model, cfg)
        else:
            data, usage = _call_openai_compatible(item, model, cfg, provider=provider)
        data = _validate(data)
    except Exception as e:
        print(f"  ! 加工失败 {item['id']} [{provider}/{model}]: {e}")
        return (None, None) if with_usage else None

    if not data["is_relevant"]:
        return (None, usage) if with_usage else None

    item.update({k: data[k] for k in
                 ("title_en", "title_cn", "summary_en", "sector", "companies")})
    item.pop("raw", None)
    return (item, usage) if with_usage else item


# 兼容 build.py 里的旧引用
DEFAULT_MODEL = {k: v["model"] for k, v in PROVIDERS.items()}
