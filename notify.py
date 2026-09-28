"""
失败通知 —— 支持飞书 / 钉钉 / 企业微信 / Slack，以及兜底的 GitHub Issue。

环境变量（都可选，配了才发）：
    NOTIFY_WEBHOOK_URL    机器人 webhook 地址
    NOTIFY_WEBHOOK_TYPE   feishu | dingtalk | wecom | slack   （默认 feishu）
    NOTIFY_PAGE_URL       你的 Pages 地址，附在消息里方便点开看

GitHub Issue 那条路不需要任何配置，见 .github/workflows/update.yml
"""
import os, json, urllib.request

PAYLOAD = {
    "feishu":   lambda t: {"msg_type": "text", "content": {"text": t}},
    "dingtalk": lambda t: {"msgtype": "text", "text": {"content": t}},
    "wecom":    lambda t: {"msgtype": "text", "text": {"content": t}},
    "slack":    lambda t: {"text": t},
}


def send(title, lines):
    url = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
    if not url:
        return False
    kind = os.environ.get("NOTIFY_WEBHOOK_TYPE", "feishu").strip().lower()
    if kind not in PAYLOAD:
        print(f"  ! 未知的 NOTIFY_WEBHOOK_TYPE: {kind}")
        return False

    page = os.environ.get("NOTIFY_PAGE_URL", "").strip()
    run  = (f"{os.environ.get('GITHUB_SERVER_URL','https://github.com')}/"
            f"{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/"
            f"{os.environ.get('GITHUB_RUN_ID','')}"
            if os.environ.get("GITHUB_RUN_ID") else "")

    text = "\n".join([f"【中国科技追踪】{title}", *lines,
                      *( [f"页面：{page}"] if page else [] ),
                      *( [f"日志：{run}"]  if run  else [] )])
    body = json.dumps(PAYLOAD[kind](text), ensure_ascii=False).encode()
    try:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"  · 已发送 {kind} 通知 ({r.status})")
        return True
    except Exception as e:
        print(f"  ! 通知发送失败：{e}")
        return False
