#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自反思回放脚本（方案 v2.1 P2）。

干什么：读 recommendation_journal / score_journal，回放模拟盘并统计评分有效性。
怎么跑：python scripts/replay_journals.py [--stock 600519] [--days 20]
需要什么：本地 SQLite（data/stock_analysis.db）+ 日线数据（load_history_df 回源）。

撮合规则（方案 C7 定稿）：
- 限价指令 T+1 日起：日内最低价 <= 买价 → 记买入成交；最高价 >= 卖价 → 记卖出成交；
- 止损优先：最低价 <= 止损价 → 止损出场（优先于卖价判定）；
- 最长持仓 --days 个交易日，到期按最后收盘价强制平仓；
- 评分相关性：总分 vs 未来 5/10/20 日收益 Spearman；样本 <30 只提示不出结论。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))


def _spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None

    def _ranks(vals: List[float]) -> List[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            r = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = r
            i = j + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(
        sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)
    )
    if den == 0:
        return None
    return round(num / den, 4)


def _future_bars(stock_code: str, after_date: str, days: int) -> List[Dict[str, Any]]:
    from src.services.history_loader import load_history_df

    df, _source = load_history_df(stock_code, days=days + 30)
    if df is None or df.empty:
        return []
    records = df.to_dict(orient="records")
    out = []
    for r in records:
        d = str(r.get("date") or "")[:10]
        if d > after_date[:10]:
            out.append(r)
    return out[:days]


def replay_recommendations(stock_code: Optional[str], max_days: int) -> Dict[str, Any]:
    from src.storage import get_db

    rows = get_db().list_recommendation_journal(stock_code=stock_code, limit=500)
    results = []
    for row in rows:
        buy_at = row.get("ideal_buy")
        sell_at = row.get("sell_price") or row.get("take_profit")
        stop = row.get("stop_loss")
        if not buy_at:
            results.append({**row, "outcome": "skipped_no_buy_price"})
            continue
        created = str(row.get("created_at") or "")
        bars = _future_bars(row["stock_code"], created, max_days)
        if len(bars) < 2:
            results.append({**row, "outcome": "no_future_data"})
            continue
        position: Optional[Dict[str, Any]] = None
        exit_price: Optional[float] = None
        exit_reason = ""
        # bars[0] 即创建日后的首个交易日 = T+1（撮合规则：限价指令 T+1 日起）
        for bar in bars:
            low = float(bar.get("low") or bar.get("close") or 0)
            high = float(bar.get("high") or bar.get("close") or 0)
            if position is None:
                if low <= float(buy_at):
                    position = {"entry": float(buy_at), "date": str(bar.get("date"))}
                continue
            if stop and low <= float(stop):
                exit_price, exit_reason = float(stop), "stop_loss"
                break
            if sell_at and high >= float(sell_at):
                exit_price, exit_reason = float(sell_at), "take_profit"
                break
        if position is None:
            results.append({**row, "outcome": "not_filled", "bars_checked": len(bars)})
            continue
        if exit_price is None:
            exit_price = float(bars[-1].get("close") or position["entry"])
            exit_reason = f"timeout_{max_days}d"
        pnl_pct = round((exit_price - position["entry"]) / position["entry"] * 100, 2)
        results.append(
            {
                **row,
                "outcome": exit_reason,
                "entry": position["entry"],
                "exit": exit_price,
                "pnl_pct": pnl_pct,
            }
        )
    filled = [r for r in results if "pnl_pct" in r]
    wins = [r for r in filled if r["pnl_pct"] > 0]
    return {
        "total": len(rows),
        "filled": len(filled),
        "win_rate": round(len(wins) / len(filled) * 100, 1) if filled else None,
        "avg_pnl_pct": round(sum(r["pnl_pct"] for r in filled) / len(filled), 2)
        if filled
        else None,
        "results": results,
    }


def replay_score_correlation(stock_code: Optional[str]) -> Dict[str, Any]:
    from src.storage import get_db

    rows = get_db().list_score_journal(stock_code=stock_code, limit=500)
    per_horizon: Dict[int, Dict[str, List[float]]] = {
        5: {"scores": [], "returns": []},
        10: {"scores": [], "returns": []},
        20: {"scores": [], "returns": []},
    }
    for row in rows:
        score = float(row.get("total_score") or 50.0)
        created = str(row.get("created_at") or "")
        for horizon in (5, 10, 20):
            bars = _future_bars(row["stock_code"], created, horizon)
            if len(bars) < horizon:
                continue
            base = float(bars[0].get("close") or 0)
            end = float(bars[horizon - 1].get("close") or 0)
            if base <= 0:
                continue
            per_horizon[horizon]["scores"].append(score)
            per_horizon[horizon]["returns"].append(round((end - base) / base * 100, 3))
    out: Dict[str, Any] = {"samples": len(rows)}
    for horizon, data in per_horizon.items():
        n = len(data["scores"])
        corr = _spearman(data["scores"], data["returns"]) if n >= 30 else None
        out[f"{horizon}d"] = {
            "n": n,
            "spearman": corr,
            "note": "样本<30，不出结论" if n < 30 else None,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="自反思回放（模拟盘 + 评分相关性）")
    parser.add_argument("--stock", default=None, help="限定股票代码（默认全部）")
    parser.add_argument("--days", type=int, default=20, help="最长持仓交易日（默认 20）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    rec = replay_recommendations(args.stock, args.days)
    corr = replay_score_correlation(args.stock)
    summary = {"recommendations": rec, "score_correlation": corr}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return
    print("=== 模拟盘回放 ===")
    print(f"指令总数 {rec['total']}，成交 {rec['filled']}", end="")
    if rec["win_rate"] is not None:
        print(f"，胜率 {rec['win_rate']}%，均值盈亏 {rec['avg_pnl_pct']}%")
    else:
        print("（暂无成交样本）")
    print("=== 评分相关性（Spearman）===")
    for horizon in ("5d", "10d", "20d"):
        item = corr[horizon]
        line = f"{horizon}: n={item['n']}"
        if item["spearman"] is not None:
            line += f"，相关性 {item['spearman']}"
        if item.get("note"):
            line += f"（{item['note']}）"
        print(line)


if __name__ == "__main__":
    main()
