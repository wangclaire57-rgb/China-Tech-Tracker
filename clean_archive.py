#!/usr/bin/env python3
import json, pathlib

data_file = pathlib.Path("data/items.json")

if data_file.exists():
    items = json.loads(data_file.read_text("utf-8"))
    
    # 过滤掉 TechNode 以及包含“治沙/植树”等非科技关键词的历史条目
    cleaned_items = [
        item for item in items
        if item.get("source_en") != "TechNode"
        and item.get("source_cn") != "TechNode"
        and "治沙" not in item.get("title_cn", "")
        and "植树" not in item.get("title_cn", "")
    ]
    
    data_file.write_text(json.dumps(cleaned_items, ensure_ascii=False, indent=1), "utf-8")
    print(f"清理完成！原数据: {len(items)} 条，清理后剩余: {len(cleaned_items)} 条。")
