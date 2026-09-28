#!/usr/bin/env python3
"""
多厂商实测对比 —— 用你自己的真实信源跑一遍，自己判断谁的摘要更好。

    python compare.py 30                          # 默认四家全跑
    python compare.py 30 qwen gemini              # 只比这两家
    python compare.py 20 claude qwen              # 贵的 vs 便宜的

只需要配你要测的那几家的 key：
    ANTHROPIC_API_KEY / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / GEMINI_API_KEY

产出 compare.html —— 并排对照，附各家实际 token 用量与推算月成本。
"""
import os, sys, json, html, pathlib
from concurrent.futures import ThreadPoolExecutor

import build, providers

args = [a for a in sys.argv[1:]]
N = int(args[0]) if args and args[0].isdigit() else 20
WANT = [a for a in args if not a.isdigit()] or list(providers.PROVIDERS)
# 只跑配了 key 的那几家，没配的静默跳过
WANT = [p for p in WANT if os.environ.get(providers.PROVIDERS[p]["key_env"])]


def main():
    if not WANT:
        sys.exit("没有检测到任何可用的 API key，先 export 一个再跑。")
    print(f"本次对比：{', '.join(WANT)}\n")

    defaults, sources = build.load_sources()
    print("抓取样本…")
    pool, seen = [], set()
    for s in sources:
        for it in build.fetch(s, defaults):
            if it["id"] not in seen and len(it["raw"]) > 80:
                seen.add(it["id"]); pool.append(it)
        if len(pool) >= N * 2:
            break
    sample = pool[:N]
    print(f"取到 {len(sample)} 条\n")

    results, cost = {}, {}
    for prov in WANT:
        _, model, _ = providers.resolve(prov)
        print(f"跑 {prov}/{model} …")
        with ThreadPoolExecutor(max_workers=6) as ex:
            rows = list(ex.map(
                lambda i: providers.enrich(dict(i), prov, with_usage=True), sample))
        results[prov] = [r[0] for r in rows]
        tin  = sum((r[1] or (0, 0))[0] for r in rows)
        tout = sum((r[1] or (0, 0))[1] for r in rows)
        pin, pout = providers.price_for(model)
        c = tin / 1e6 * pin + tout / 1e6 * pout
        cost[prov] = (model, tin, tout, c)
        print(f"  成功 {sum(1 for r in rows if r[0])}/{len(sample)} | "
              f"in {tin:,} out {tout:,} tok | ${c:.4f}")

    # ---- 生成对照表 ----
    head = "".join(f"<th>{p}<div class=hm>{cost[p][0]}</div></th>" for p in WANT)
    body = []
    for i, src in enumerate(sample):
        cells = []
        for p in WANT:
            r = results[p][i]
            cells.append(
                f"<td><div class=t>{html.escape(r['title_en'])}</div>"
                f"<div class=s>{html.escape(r['summary_en'])}</div>"
                f"<div class=m>{r['sector']} · "
                f"{' / '.join(html.escape(c) for c in r['companies']) or '—'} · "
                f"{len(r['summary_en'].split())} words</div></td>"
                if r else "<td class=skip>（判定不相关 / 失败）</td>")
        body.append(
            f"<tr><td class=src><a href='{html.escape(src['url'])}' target=_blank>"
            f"{html.escape(src['title_cn'][:70])}</a>"
            f"<div class=m>{html.escape(src['source_cn'])} · {src['date']}</div></td>"
            + "".join(cells) + "</tr>")

    peak = "高峰" if providers.deepseek_is_peak() else "平峰"
    summary = "<table class=cost><tr><th>provider</th><th>model</th><th>in</th>"\
              "<th>out</th><th>本次花费</th><th>推算每月<br>(80条/天)</th></tr>" + "".join(
        f"<tr><td>{p}{'（'+peak+'价）' if p=='deepseek' else ''}</td><td>{m}</td>"
        f"<td>{ti:,}</td><td>{to:,}</td><td>${c:.4f}</td>"
        f"<td><b>{'免费额度内' if c==0 else f'${c/len(sample)*80*30:.2f}'}</b></td></tr>"
        for p, (m, ti, to, c) in cost.items()) + "</table>"

    pathlib.Path("compare.html").write_text(f"""<!doctype html><meta charset=utf-8>
<title>模型横评</title><style>
body{{font:14px/1.55 -apple-system,"PingFang SC",sans-serif;margin:24px;color:#141c24;background:#fff}}
h1{{font-size:20px;margin:0 0 12px}}
table{{border-collapse:collapse;width:100%;margin-bottom:22px}}
td{{vertical-align:top;padding:12px 14px;border-top:1px solid #e7ecf1}}
td.src{{width:26%;background:#f5f7f9}} td.src a{{color:#141c24;font-weight:600;text-decoration:none}}
th{{text-align:left;padding:9px 14px;background:#141c24;color:#fff;font:13px ui-monospace,Menlo,monospace}}
.hm{{font-weight:400;opacity:.65;font-size:11px;margin-top:2px}}
.t{{font-weight:600;margin-bottom:5px}} .s{{color:#3e4c58}}
.m{{font:11px ui-monospace,Menlo,monospace;color:#64727e;margin-top:6px}}
.skip{{color:#b93724;font-size:13px}}
table.cost{{width:auto}} table.cost td{{padding:7px 14px;font:12px ui-monospace,Menlo,monospace}}
</style>
<h1>模型横评 · {len(sample)} 条真实样本</h1>
{summary}
<table><tr><th>原文（中文）</th>{head}</tr>{''.join(body)}</table>""", encoding="utf-8")

    print("\n完成 -> compare.html")
    for p, (m, ti, to, c) in cost.items():
        est = "免费额度内" if c == 0 else f"${c/len(sample)*80*30:.2f}"
        print(f"  {p:<9}{m:<20} ${c:.4f} / {len(sample)}条  → 每月约 {est}")


if __name__ == "__main__":
    main()
