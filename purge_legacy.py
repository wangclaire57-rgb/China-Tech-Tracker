#!/usr/bin/env python3
"""
一次性清理：删除白名单上线之前抓取的历史条目。

为什么必须整体删除而不能挑着留：
  旧条目的 source_cn 记录的是**信源配置名**（例如「芯片·出口管制与博弈」
  「中国大模型动态」），不是真实发布媒体。即使某条标着「36氪」，它也可能
  实际来自新浪财经 —— 当时根本没有记录发布媒体。所以旧条目的来源一律不可信，
  无法逐条判断是否在白名单内。

  新版条目带 source_domain 字段（来自 Google News 的 <source url>），
  并且已经过白名单硬过滤，全部可信。

删掉的近期条目会在下次运行时被重新抓回来（回溯窗口 14 天），
这次会带上正确的发布媒体。超过 14 天的会永久丢失 —— 但它们本来就来源不明。

    python purge_legacy.py           # 试跑，只看统计
    python purge_legacy.py --write   # 真正写入
"""
import sys, json, pathlib, collections

DATA = pathlib.Path("data/items.json")
WRITE = "--write" in sys.argv

items = json.loads(DATA.read_text("utf-8"))
new = [i for i in items if i.get("source_domain")]
old = [i for i in items if not i.get("source_domain")]

print(f"存档共 {len(items)} 条")
print(f"  保留 · 新版（有 source_domain，已过白名单）: {len(new)}")
print(f"  删除 · 旧版（无 source_domain，来源不可信）: {len(old)}")

if old:
    print("\n将被删除的条目按标称来源分布（这些标称多为查询名，非真实媒体）：")
    for s, n in collections.Counter(
            i.get("source_cn") or i.get("source_en") or "?" for i in old).most_common(12):
        print(f"    {s:<28}{n:>4}")
    days = sorted({i["date"] for i in old})
    print(f"  日期范围: {days[0]} ~ {days[-1]}")

if new:
    print("\n保留下来的条目：")
    print("  板块:", dict(collections.Counter(i["sector"] for i in new).most_common()))
    print("  媒体:", dict(collections.Counter(i.get("source_cn","?") for i in new).most_common(8)))

if WRITE:
    new.sort(key=lambda i: (i["date"], i.get("source_en","")), reverse=True)
    DATA.write_text(json.dumps(new, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n已写入 {DATA}：{len(items)} -> {len(new)} 条")
    print("下次运行会把 14 天内的内容重新抓回来，并带上正确的发布媒体。")
else:
    print("\n（试跑，未写入。确认后加 --write 重跑）")
