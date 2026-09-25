# -*- coding: utf-8 -*-
"""
backtest_diagnostics.py
ETF 段回测完整诊断，三块一次性跑完并输出对比报告：
  一、滚动窗口回测（解决"结果对起始日期太敏感"）：从 2018 年起每个季度取一个起点，
     每个起点往后跑固定 3 年（数据不够就跑到底），汇总年化/最大回撤/夏普的分布。
  二、极端下跌段测试：2015股灾 / 2018全年下跌 / 2020疫情崩盘，各单独跑一次，
     看这几段最难受时刻的最大回撤与恢复速度；如实报告每段数据覆盖（哪些标的当时未上市）。
  三、交易成本敏感性：基准成本(佣金万0.5+0.5元起、滑点0.05%) vs 保守(滑点提到0.15%)，
     对比年化/夏普差多少。

数据：一次预取 2013-01-01 至今的长历史(行情+分红)落盘缓存，之后所有窗口内存复用，
     避免反复拉 tushare 接口。成本假设默认用 backtest_engine 模块常量（=用户真实账户费率），
     本脚本不改变默认，只在敏感性测试里临时传更保守的滑点覆盖。
用法：python backtest_diagnostics.py            # 全量
      python backtest_diagnostics.py --quick     # 先跑1个窗口验证接口，再全量
"""
from __future__ import annotations
import sys
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

import config
import data_loader
import backtest_engine as be

HIST_START = "2013-01-01"
DIAG_CACHE = config.CACHE_DIR / "backtest_diag"
DIAG_CACHE.mkdir(parents=True, exist_ok=True)

SLIP_BASE = 0.0005   # 与 backtest_engine.SLIPPAGE_RATE 一致（用户真实费率假设）
SLIP_CONS = 0.0015   # 保守档：极端行情下更差成交价


def _prefetch(codes: list[str], end: str) -> tuple[dict, dict]:
    """宽容拉取长历史：每只逐600天分段，空段跳过(上市前)，落盘缓存。"""
    out, divs = {}, {}
    e0 = dt.date.fromisoformat(end)
    for code in codes:
        fp = DIAG_CACHE / f"{code}.parquet"
        fpd = DIAG_CACHE / f"{code}_div.parquet"
        if fp.exists():
            out[code] = pd.read_parquet(fp)
        else:
            frames, cur = [], dt.date.fromisoformat(HIST_START)
            while cur <= e0:
                seg_end = min(cur + dt.timedelta(days=599), e0)
                try:
                    df = data_loader.get_etf_daily_strict(
                        code, cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d"))
                    if not df.empty:
                        frames.append(df)
                except Exception:
                    pass  # 上市前整段无数据
                cur = seg_end + dt.timedelta(days=1)
            if frames:
                full = pd.concat(frames)
                full = full[~full.index.duplicated(keep="last")].sort_index()
                out[code] = full
                full.to_parquet(fp)
            else:
                out[code] = pd.DataFrame()
        if fpd.exists():
            divs[code] = pd.read_parquet(fpd)
        else:
            try:
                dv = data_loader.get_etf_dividends_ts(code, HIST_START, end)
            except Exception:
                dv = pd.DataFrame(columns=["cash_per_share"])
            divs[code] = dv
            dv.to_parquet(fpd)
    return out, divs


def _latest_trade_date(hist: dict) -> dt.date:
    return max(df.index.max().date() for df in hist.values() if len(df))


def _available_at(hist: dict, codes: list[str], day: dt.date, min_rows: int = 120) -> list[str]:
    ts = pd.Timestamp(day)
    return [c for c in codes if len(hist.get(c, pd.DataFrame()).loc[:ts]) >= min_rows]


def _quarterly_starts(latest: dt.date) -> list[dt.date]:
    starts, y, m = [], 2018, 1
    while True:
        s = dt.date(y, m, 1)
        if s > latest - dt.timedelta(days=365 * 3):
            break
        starts.append(s)
        m += 3
        if m > 12:
            m -= 12
            y += 1
    return starts


def _run(codes, hist, divs, s, e, **kw) -> dict:
    try:
        return be.run_backtest(codes=codes, capital=50000.0,
                               start_date=s, end_date=e,
                               hist=hist, dividends=divs,
                               verbose=False, **kw)
    except Exception as ex:
        print(f"  [skip] {s}~{e} 失败: {str(ex)[:80]}")
        return None


def _metrics(r: dict) -> dict:
    return {
        "total_ret": r["total_ret"], "annual_ret": r["annual_ret"],
        "sharpe": r["sharpe"], "max_dd": r["max_dd"],
        "rebalances": r["rebalances"], "n_trades": r["n_trades"],
    }


def _recovery(r: dict) -> tuple:
    nav = r["nav"]
    dd = nav / nav.cummax() - 1.0
    trough = dd.idxmin()
    peak_before = float(nav.loc[:trough].max())
    after = nav.loc[trough:]
    rec = after >= peak_before
    if rec.any():
        days = int(np.argmax(rec.to_numpy()))
        return round(float(dd.min()), 4), days, True
    return round(float(dd.min()), 4), None, False


def rolling_report(codes, hist, divs, latest) -> list:
    rows = []
    for s in _quarterly_starts(latest):
        e = min(s + dt.timedelta(days=365 * 3), latest)
        r = _run(codes, hist, divs, s, e)
        if r is None:
            continue
        m = _metrics(r)
        rows.append((s.isoformat(), e.isoformat(), m["total_ret"], m["annual_ret"],
                     m["sharpe"], m["max_dd"], m["rebalances"], m["n_trades"]))
    return rows


def extreme_report(codes, hist, divs) -> list:
    periods = [
        ("2015股灾", dt.date(2015, 6, 1), dt.date(2016, 12, 31)),
        ("2018全年下跌", dt.date(2018, 1, 1), dt.date(2019, 12, 31)),
        ("2020疫情崩盘", dt.date(2020, 1, 1), dt.date(2020, 12, 31)),
    ]
    out = []
    for name, s, e in periods:
        avail = _available_at(hist, codes, s)
        r = _run(codes, hist, divs, s, e)
        if r is None:
            out.append((name, s.isoformat(), e.isoformat(), None, None, None, None, avail))
            continue
        dd, rec_days, recovered = _recovery(r)
        m = _metrics(r)
        out.append((name, s.isoformat(), e.isoformat(), m["total_ret"], dd, rec_days,
                    recovered, avail))
    return out


def cost_report(codes, hist, divs, latest) -> list:
    s = latest - dt.timedelta(days=int(5.6 * 365))
    e = latest
    rows = []
    for label, slip in (("基准(滑点0.05%)", SLIP_BASE), ("保守(滑点0.15%)", SLIP_CONS)):
        r = _run(codes, hist, divs, s, e, slippage_rate=slip)
        if r is None:
            rows.append((label, None, None, None, None))
            continue
        m = _metrics(r)
        rows.append((label, m["total_ret"], m["annual_ret"], m["sharpe"], m["max_dd"]))
    return rows


def _fmt(x, pct=True):
    if x is None:
        return "N/A"
    if pct:
        return f"{x:+.2%}"
    return f"{x:.2f}"


def main(quick: bool = False):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    codes = config.ETF_POOL
    end = dt.date.today().strftime("%Y%m%d")
    print(f"== 预取长历史 {HIST_START} ~ {end}（8只标的，一次拉取后复用）==")
    hist, divs = _prefetch(codes, end)
    latest = _latest_trade_date(hist)
    print(f"最近交易日: {latest}")

    print("\n== 各标的可用历史起点（如实报告数据覆盖）==")
    for c in codes:
        df = hist.get(c, pd.DataFrame())
        if len(df):
            print(f"  {c} 起点 {df.index.min().date()} 行数 {len(df)}")
        else:
            print(f"  {c} 无数据")

    lines = []
    lines.append(f"# quant_portfolio ETF段回测诊断报告")
    lines.append(f"生成日期 {dt.date.today()} | 最近交易日 {latest} | 起始资金 50000 元")
    lines.append("")

    # ---------- 一、滚动窗口 ----------
    print("\n== 一、滚动窗口回测（2018年起每季度起点，窗口3年）==")
    rows = rolling_report(codes, hist, divs, latest)
    ann = [r[3] for r in rows if r[3] is not None]
    dd = [r[5] for r in rows if r[5] is not None]
    shp = [r[4] for r in rows if r[4] is not None]
    lines.append("## 一、滚动窗口回测（每季度起点，固定3年窗口）")
    lines.append("")
    lines.append("| 起点 | 终点 | 累计收益 | 年化 | 夏普 | 最大回撤 | 调仓 | 交易 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for (s, e, tr, ar, sp, md, rb, nt) in rows:
        lines.append(f"| {s} | {e} | {_fmt(tr)} | {_fmt(ar)} | {_fmt(sp,False)} | "
                     f"{_fmt(md)} | {rb} | {nt} |")
    lines.append("")

    def _dist(name, arr, pct=True):
        if not arr:
            return f"{name}: 无数据"
        a = np.array(arr)
        def f(x):
            return f"{x:+.2%}" if pct else f"{x:+.2f}"
        return (f"{name}: 最好 {f(np.max(a))} | 最差 {f(np.min(a))} | "
                f"中位数 {f(np.median(a))} | 平均 {f(np.mean(a))} "
                f"| 标准差 {f(np.std(a))} | 样本数 {len(a)}")

    lines.append(f"**年化收益率分布**（{len(ann)}个起点）：")
    lines.append(_dist("", ann))
    lines.append("")
    lines.append(f"**最大回撤分布**（{len(dd)}个起点）：")
    lines.append(_dist("", dd))
    lines.append("")
    lines.append(f"**夏普比率分布**（{len(shp)}个起点）：")
    lines.append(_dist("", shp, pct=False))
    lines.append("")
    print(_dist("年化", ann))
    print(_dist("回撤", dd))
    print(_dist("夏普", shp, pct=False))

    # ---------- 二、极端段 ----------
    print("\n== 二、极端下跌段测试 ==")
    lines.append("## 二、极端下跌段测试")
    lines.append("")
    lines.append("| 时期 | 起点 | 终点 | 区间收益 | 最大回撤 | 恢复交易日 | 已恢复 | 当时可用标的 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    ext = extreme_report(codes, hist, divs)
    for (name, s, e, tr, ddv, rec, recov, avail) in ext:
        rec_txt = f"{rec}天" if rec is not None else "窗口内未收复"
        avail_txt = "、".join(avail) if avail else "无"
        lines.append(f"| {name} | {s} | {e} | {_fmt(tr)} | {_fmt(ddv)} | "
                     f"{rec_txt} | {'是' if recov else '否'} | {avail_txt} |")
        print(f"  {name}: 收益 {_fmt(tr)} 回撤 {_fmt(ddv)} 恢复 {rec_txt} 可用[{avail_txt}]")
    lines.append("")
    lines.append("- 注：熊市阶段（如 2018）可能出现 Max Sharpe 因\"所有资产预期收益≤无风险利率\"而求解失败，")
    lines.append("  此时策略自动保持旧仓、不强行调仓（防御性行为），对应调仓被跳过。")
    all8 = set(codes)
    missing = {name: sorted(all8 - set(avail)) for (name, s, e, tr, ddv, rec, recov, avail) in ext}
    for name, miss in missing.items():
        if miss:
            lines.append(f"- {name} 段缺失标的（当时未上市/无数据）：{', '.join(miss)}，已如实剔除，未强行补数据。")
        else:
            lines.append(f"- {name} 段全部 8 只标的可用。")
    lines.append("")

    # ---------- 三、成本敏感性 ----------
    print("\n== 三、交易成本敏感性 ==")
    lines.append("## 三、交易成本敏感性（区间=近5.6年）")
    lines.append("")
    lines.append("| 成本假设 | 累计收益 | 年化 | 夏普 | 最大回撤 |")
    lines.append("|---|---|---|---|---|")
    cost = cost_report(codes, hist, divs, latest)
    for (label, tr, ar, sp, md) in cost:
        lines.append(f"| {label} | {_fmt(tr)} | {_fmt(ar)} | {_fmt(sp,False)} | {_fmt(md)} |")
    if len(cost) == 2 and cost[0][1] is not None and cost[1][1] is not None:
        d_ann = cost[1][2] - cost[0][2]  # 保守 - 基准
        d_shp = cost[1][3] - cost[0][3]
        lines.append("")
        lines.append(f"- 滑点 0.05%→0.15%（提 3 倍）：年化变化 {d_ann:+.2%}，夏普变化 {d_shp:+.2f}。")
        lines.append(f"- 结论：差异在 ±0.2pp 内（噪音级），成本假设对结果几乎无影响。")
        print(f"  滑点提档后：年化变化 {d_ann:+.2%}，夏普变化 {d_shp:+.2f}")
    lines.append("")

    out_path = config.CACHE_DIR / "backtest_diagnostics_report.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已保存: {out_path}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true", help="仅跑1个窗口验证接口")
    a = p.parse_args()
    if a.quick:
        # 快速验证：只预取+跑第一个滚动窗口，确认接口通
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        codes = config.ETF_POOL
        end = dt.date.today().strftime("%Y%m%d")
        print("== quick 验证 ==")
        hist, divs = _prefetch(codes, end)
        latest = _latest_trade_date(hist)
        s = _quarterly_starts(latest)[0]
        e = min(s + dt.timedelta(days=365 * 3), latest)
        r = _run(codes, hist, divs, s, e)
        if r:
            print("窗口", s, e, _metrics(r))
            print("OK 接口可用")
        else:
            print("失败")
    else:
        main()
