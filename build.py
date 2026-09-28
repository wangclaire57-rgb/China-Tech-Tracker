import os
import sys
import json
import time
from typing import Dict, List, Any, Optional
from openai import OpenAI

# ==========================================
# 1. 初始化 API 客户端 (DashScope 兼容模式)
# ==========================================
API_KEY = os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY")

if not API_KEY:
    print("❌ [错误] 未设置 API Key，请检查 GitHub Secrets 或环境变量！")
    sys.exit(1)

client = OpenAI(
    api_key=API_KEY,
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
)

# ==========================================
# 2. 单条数据 AI 解析函数 (含重试与兜底逻辑)
# ==========================================
def process_single_item(user_input: str, max_retries: int = 2) -> Optional[Dict[str, Any]]:
    """
    调用 Qwen 模型解析新闻，自带 JSON 强制约束与字段缺失容错
    """
    system_prompt = (
        "你是一个专业的新闻解析助手。请务必输出合法的纯 JSON 对象，"
        "且必须严格包含以下三个字段：\n"
        "- `title`: 新闻标题（中文）\n"
        "- `title_en`: 新闻英文标题（若无法翻译，直接使用原文标题，严禁遗漏该字段）\n"
        "- `summary`: 核心摘要（中文）\n\n"
        "请确保返回格式格式为：{\"title\": \"...\", \"title_en\": \"...\", \"summary\": \"...\"}"
    )

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model="qwen3.7-flash",
                response_format={"type": "json_object"},  # 强制要求返回 JSON 格式
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input},
                ],
                max_tokens=4096,  # 防止 JSON 截断
                temperature=0.3,
            )

            raw_content = response.choices[0].message.content.strip()
            data = json.loads(raw_content)

            # --- 核心兜底校验逻辑 ---
            # 1. 确保 title 存在
            title = data.get("title") or "无标题新闻"
            
            # 2. 确保 title_en 存在（若缺失，自动使用 title 兜底）
            title_en = data.get("title_en") or title or "Untitled"
            
            # 3. 确保 summary 存在
            summary = data.get("summary") or ""

            return {
                "title": title,
                "title_en": title_en,
                "summary": summary,
                "raw_input": user_input
            }

        except json.JSONDecodeError as e:
            print(f"⚠️ [重试 {attempt + 1}/{max_retries + 1}] JSON 解析异常: {e}")
        except Exception as e:
            print(f"⚠️ [重试 {attempt + 1}/{max_retries + 1}] API 请求失败: {e}")

        time.sleep(1)  # 简短等待后重试

    return None

# ==========================================
# 3. 主流程与批处理控制
# ==========================================
def load_input_data(file_path: str = "raw_news.json") -> List[Any]:
    """读取待处理数据文件"""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    print(f"⚠️ 未找到输入文件 {file_path}，使用测试数据")
    return ["样例新闻 1：AI 技术在 2026 年取得重大突破", "样例新闻 2：开源模型市场快速发展"]

def save_output_data(data: List[Dict[str, Any]], file_path: str = "processed_news.json"):
    """保存处理完的数据"""
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 处理完毕，成功存入 {len(data)} 条数据至 {file_path}")

def main():
    print("🚀 开始新闻解析构建任务...")
    
    # 1. 获取数据列表
    items = load_input_data("raw_news.json")
    total_items = len(items)
    print(f"📦 共计需处理 {total_items} 条待分析内容")

    successful_results = []
    failed_count = 0

    # 2. 顺序/批处理调用 AI 接口
    for idx, item in enumerate(items, 1):
        content_text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        
        result = process_single_item(content_text)
        if result:
            successful_results.append(result)
            print(f"[{idx}/{total_items}] 成功解析: {result['title'][:15]}...")
        else:
            failed_count += 1
            print(f"[{idx}/{total_items}] ❌ 解析失败")

        # 适当微延时防止触发高 QPS 限流
        time.sleep(0.1)

    # 3. 计算报错率与退出断言
    failure_rate = (failed_count / total_items) if total_items > 0 else 0
    print(f"\n统计结果: 总数 {total_items} | 成功 {len(successful_results)} | 失败 {failed_count} | 报错率 {failure_rate:.1%}")

    # 4. 保存成功的数据
    save_output_data(successful_results)

    # 报错率过高判定（例如高于 50% 抛出错误终止 GitHub Action）
    if failure_rate > 0.5:
        print(f"❌ AI 接口报错率过高（{failure_rate:.1%}），终止构建！")
        sys.exit(1)

    print("🎉 任务执行成功！")

if __name__ == "__main__":
    main()
