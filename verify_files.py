#!/usr/bin/env python3
"""
上传前/后自检：确认每个文件装的确实是它该装的内容。

    python verify_files.py

这次出的事故：build.py 里被贴成了 providers.py 的内容。
因为 providers.py 没有 __main__ 入口，python build.py 会静默退出 0，
workflow 显示绿色成功，但页面永远不更新。这个脚本就是防这个的。
"""
import sys, pathlib

# 每个文件必须出现的特征串
REQUIRED = {
    "build.py":           ["def main()", "feed_url", "TRACKER_MAX_NEW",
                           'if __name__ == "__main__"'],
    "providers.py":       ["PROVIDERS = {", "NEWS_TYPES", "def enrich("],
    "sources.yml":        ["sources:", "Robotics", "default_sector"],
    "template.html":      ["<!doctype", "news_type", "TYPE_CN", "{{UPDATED_ISO}}"],
    "notify.py":          ["NOTIFY_WEBHOOK_URL", "def send("],
    "migrate_archive.py": ["--write", "migrate", "SECTORS"],
    "compare.py":         ["compare.html", "providers.enrich"],
    "requirements.txt":   ["openai", "feedparser", "PyYAML"],
}
# 绝不该出现的（错位的典型特征）
FORBIDDEN = {
    "build.py":     ["模型适配层", "raw_news.json"],
    "sources.yml":  ["General Tech"],
    "template.html": ["SECTOR_MAP"],
}

ok = True
print(f"{'文件':<22}{'状态'}")
print("-" * 62)
for name, needles in REQUIRED.items():
    p = pathlib.Path(name)
    if not p.exists():
        print(f"{name:<22}✗ 文件不存在"); ok = False; continue
    txt = p.read_text(encoding="utf-8", errors="ignore")
    missing = [n for n in needles if n not in txt]
    bad = [n for n in FORBIDDEN.get(name, []) if n in txt]
    if missing:
        print(f"{name:<22}✗ 缺少特征: {missing}"); ok = False
    elif bad:
        print(f"{name:<22}✗ 出现了不该有的内容（贴错文件？）: {bad}"); ok = False
    else:
        print(f"{name:<22}✓ {p.stat().st_size:,} B")

# build.py 必须能作为脚本真正产出东西
b = pathlib.Path("build.py")
if b.exists() and 'if __name__ == "__main__"' not in b.read_text(encoding="utf-8", errors="ignore"):
    print("\n✗ 致命：build.py 没有 __main__ 入口 —— 跑起来会静默退出 0，"
          "\n        workflow 会显示绿色成功但页面永远不更新。")
    ok = False

print("-" * 62)
print("✓ 全部通过，可以上传" if ok else "✗ 有问题，先修再传")
sys.exit(0 if ok else 1)
