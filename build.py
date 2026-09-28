import os
import sys
import json
import time
from typing import Dict, List, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI

# ==========================================
# 1. 配置参数
# ==========================================
MAX_WORKERS = 10  # 并发线程数（同时请求 10 条，可大幅提升速度且不易触发限流）
INPUT_FILE = "raw_news.json"
OUTPUT_FILE = "processed_news.json"

API_KEY = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")

if not API_KEY:
    print("❌ [错误] 未设置 API Key，请检查 GitHub Secrets 或环境变量！")
    sys.exit(1)

client = OpenAI(
    api_key=API_KEY,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# ==========================================
# 2. 单条数据处理函数
# ==========================================
def process_single_item(index: int, total: int, user_input_item: Any, max_retries: int = 2) -> Optional[Dict[str, Any]]:
    user_input = user_input_item if isinstance(user_input_item, str) else json.dumps(user_input_item, ensure_ascii=False)
    
    system_prompt = (
        "你是一个专业的新闻解析助手。请务必输出合法的纯 JSON 对象，"
        "且必须严格包含以下三个字段：\n"
        "- `title`: 新闻标题（中文）\n"
        "- `title_en`: 新闻英文标题（若无法翻译，直接使用原文标题，严禁遗漏该字段）\n"
        "- `summary`: 核心摘要（中文）\n\n"
        "请确保返回格式为：{\"title\": \"...\", \"title_en\": \"...\", \"summary\": \"...\"}"
    )

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model="qwen3.7-flash",
                response_format={"type": "json_object"},  # 强制要求返回 JSON
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input},
                ],
                max_tokens=4096,
                temperature=0.3,
            )

            raw_content = response.choices[0].message.content.strip()
            data = json.loads(raw_content)

            # --- 兜底逻辑 ---
            title = data.get("title") or "无标题新闻"
            title_en = data.get("title_en") or title or "Untitled"
            summary = data.get("summary") or ""

            print(f"[{index}/{total}] ✅ 解析成功: {title[:15]}...")
            return {
                "id": index,
                "title": title,
                "title_en": title_en,
                "summary": summary,
                "raw_input": user_input
            }

        except Exception as e:
            if attempt < max_retries:
                time.sleep(1)
            else:
                print(f"[{index}/{total}] ❌ 解析失败 ({e})")

    return None

# ==========================================
# 3. 多线程主流程
# ==========================================
def main():
    start_time = time.time()
    print("🚀 开始并发新闻解析任务...")

    # 读取输入文件
    if os.path.exists(INPUT_FILE):
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
    else:
        print(f"⚠️ 未找到 {INPUT_FILE}，使用测试数据")
        items = ["测试新闻 1", "测试新闻 2"]

    total_items = len(items)
    print(f"📦 共计需处理 {total_items} 条内容，开启 {MAX_WORKERS} 线程并发处理...")

    successful_results = []
    failed_count = 0

    # 使用线程池并发请求
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_single_item, idx, total_items, item): idx 
            for idx, item in enumerate(items, 1)
        }

        for future in as_completed(futures):
            res = future.result()
            if res:
                successful_results.append(res)
            else:
                failed_count += 1

    # 按原始顺序重新排序结果
    successful_results.sort(key=lambda x: x["id"])

    # 保存处理结果
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(successful_results, f, ensure_ascii=False, indent=2)

    elapsed_time = time.time() - start_time
    failure_rate = (failed_count / total_items) if total_items > 0 else 0
    
    print("\n================ 运行报告 ================")
    print(f"⏱️ 耗时: {elapsed_time:.1f} 秒")
    print(f"📊 总数: {total_items} | 成功: {len(successful_results)} | 失败: {failed_count}")
    print(f"📉 报错率: {failure_rate:.1%}")

    if failure_rate > 0.5:
        print("❌ AI 接口报错率过高（>50%），终止构建！")
        sys.exit(1)

    print("🎉 任务顺利完成！")

if __name__ == "__main__":
    main()
