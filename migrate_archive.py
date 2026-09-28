#!/usr/bin/env python3
"""
一次性迁移：把已有存档重新分类到新的 8 板块 + 6 类型体系。

旧存档用的是中文板块（人工智能/综合/政策/…）和 "General Tech"，
新页面按 8 个英文板块筛选，不迁移的话这些条目会从筛选器里消失。

    python migrate_archive.py            # 试跑，只统计不写入
    python migrate_archive.py --write    # 真正写入 data/items.json

成本：353 条 × qwen3.7-flash ≈ $0.002，可以忽略。
"""
import sys, json, pathlib, collections
from concurrent.futures import ThreadPoolExecutor
from providers import enrich, resolve, SECTORS, NEWS_TYPES

DATA = pathlib.Path("data/items.json")
WRITE = "--write" in sys.argv
PROVIDER, MODEL, _ = resolve()

items = json.loads(DATA.read_text("utf-8"))
print(f"存档 {len(items)} 条，当前板块分布：")
for s, n in collections.Counter(i.get("sector") for i in items).most_common():
    print(f"   {str(s):<16}{n}")

need = [i for i in items if i.get("sector") not in SECTORS or not i.get("news_type")]
print(f"\n需要重新分类 {len(need)} 条，用 {PROVIDER}/{MODEL}…")

def redo(it):
    # raw 字段在入库时已被删掉，用标题+摘要重建输入
    probe = dict(it)
    probe["raw"] = (it.get("summary_en") or "")[:1200] or it.get("title_cn", "")
    out, err = enrich(probe)
    if out:
        return it["id"], out["sector"], out["news_type"]
    return it["id"], None, err

with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(redo, need))

fixed = {rid: (sec, ty) for rid, sec, ty in results if sec}
failed = [r for r in results if not r[1]]
print(f"成功 {len(fixed)} 条，失败/判定不相关 {len(failed)} 条")

kept = []
for it in items:
    if it["id"] in fixed:
        it["sector"], it["news_type"] = fixed[it["id"]]
        kept.append(it)
    elif it.get("sector") in SECTORS and it.get("news_type"):
        kept.append(it)
    # 既不在新体系里、又重分类失败的，丢弃（多半本来就是噪声）

print(f"\n迁移后 {len(kept)} 条（丢弃 {len(items)-len(kept)} 条无法归类的）")
print("  板块:", dict(collections.Counter(i["sector"] for i in kept).most_common()))
print("  类型:", dict(collections.Counter(i["news_type"] for i in kept).most_common()))

if WRITE:
    DATA.write_text(json.dumps(kept, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n已写入 {DATA}")
else:
    print("\n（试跑，未写入。确认无误后加 --write 重跑）")
