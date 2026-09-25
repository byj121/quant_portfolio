# -*- coding: utf-8 -*-
"""
backtest_engine.py
真正的历史回测引擎：验证"如果过去几年就按这套因子/防雷/调仓规则运行，历史表现如何"。
独立于任何已建组合，纯粹用历史数据回放。

本版为【纯 ETF 段】（8 只池），与用户确认后的语义：
  1. 初始日：用当时(≤D)的周线数据求解一次目标权重 → 建仓。
  2. 之后逐交易日检查是否触发再平衡(rebalancer 的 5%/30天、10%紧急)：
     触发了就【用当时(≤D)的数据重新求解】新目标权重并调仓(模型算成啥就调成啥)，
     不是调回最初目标。
  3. 每次调仓按真实费率扣成本：ETF 佣金万0.5(0.5元起) + 单边滑点，无印花税。
  4. 防幸存者偏差：只在"该标的当日已有价格数据"时允许其进入/交易(天然排除尚未上市)；
     若某只标的在回测起点前数据不足(未上市/刚上市)，自动延后或剔除并在报告中注明。
     注：ETF 池是人工精选且全为 3 年前已上市老标的，池级幸存者偏差几乎为0。
  5. 财报 point-in-time(filter_financials_pit)在股票段接入；纯ETF段不涉及财报。
"""
from __future__ import annotations
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

import config
import data_loader
import optimizer
import rebalancer

logger = None  # 占位

# ---------------------------------------------------------------------------
# 交易成本（真实费率，用户提供）
# ---------------------------------------------------------------------------
COMMISSION_RATE = 0.00005      # 场内基金佣金 万0.5
COMMISSION_MIN = 0.5           # 单笔最低 0.5 元
STAMP_TAX_RATE = 0.0           # ETF 无印花税（A股卖出印花税在股票段另行处理）
SLIPPAGE_RATE = 0.0005         # 单边滑点 0.05%（宽基ETF务实估计；保守可调高到0.001）

_FORCE_REFRESH = False         # True=忽略当天缓存强制重新拉 tushare（命令行 --refresh）


def _order_cost(amount: float) -> float:
    """单笔交易成本 = max(佣金下限, 成交额×佣金率) + 成交额×滑点 + 卖出印花税"""
    comm = max(COMMISSION_MIN, amount * COMMISSION_RATE)
    return comm + amount * SLIPPAGE_RATE  # 印花税在 sell 时按成交额另计


def _fetch_close_history(codes: list[str], hist_start: str, end: str,
                         bt_start: dt.date = None) -> dict[str, pd.DataFrame]:
    """拉每只标的从 hist_start 到 end 的日线。
    只用 tushare fund_daily（严格单源，保证可复现），分 600 天窗口拉再拼接。
    bt_start = 回测正式起点（不含）：bt_start 之前的段是纯协方差预热窗口，
    标的若尚未上市/数据缺失可跳过（晚上市标的正常延后进池）；
    bt_start 之后的段必须连续非空，否则中止（避免静默缺段导致结果失真）。
    当天已缓存的数据直接复用（回测以后要常做，避免每次都烧共享账号的调用），
    需要强制刷新可传 refresh=True 或删除 .cache/backtest_data/。"""
    cache_dir = config.CACHE_DIR / "backtest_data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    step = 600
    s0 = dt.date.fromisoformat(hist_start)
    e0 = dt.date.fromisoformat(end)
    for code in codes:
        fp = cache_dir / f"{code}_{hist_start}_{end}.parquet"
        cache_fresh = False
        if fp.exists():
            mtime = dt.date.fromtimestamp(fp.stat().st_mtime)
            if mtime == dt.date.today() and not _FORCE_REFRESH:
                try:
                    out[code] = pd.read_parquet(fp).sort_index()
                    cache_fresh = True
                except Exception as ex:
                    print(f"[warn] {code} 缓存读取失败，重新拉取: {ex}")
        if cache_fresh:
            continue
        frames = []
        cur = s0
        while cur <= e0:
            seg_end = min(cur + dt.timedelta(days=step - 1), e0)
            seg = f"{cur.strftime('%Y%m%d')}~{seg_end.strftime('%Y%m%d')}"
            # 预热段：整个窗口在回测正式起点之前，标的未上市可跳过
            is_prewarm = bt_start is not None and seg_end < bt_start
            df = None
            last_err = None
            for attempt in (1, 2):
                try:
                    df = data_loader.get_etf_daily_strict(code, cur.strftime("%Y%m%d"),
                                                           seg_end.strftime("%Y%m%d"))
                    break
                except Exception as ex:
                    last_err = ex
                    if attempt == 1:
                        print(f"[retry] {code} {seg} 第1次失败({str(ex)[:60]})，重试一次")
            if df is None:
                if is_prewarm:
                    print(f"[note] {code} {seg} 预热段无数据({str(last_err)[:40]})，跳过"
                          f"(标的可能当时未上市)")
                    cur = seg_end + dt.timedelta(days=1)
                    continue
                raise RuntimeError(f"{code} {seg} 拉取两次仍失败: {last_err}")
            if not df.empty:
                frames.append(df)
            cur = seg_end + dt.timedelta(days=1)
        if frames:
            full = pd.concat(frames)
            full = full[~full.index.duplicated(keep="last")].sort_index()
            out[code] = full
            try:
                full.to_parquet(fp)
            except Exception as ex:
                print(f"[warn] {code} 缓存写入失败（不影响本次结果）: {ex}")
    return out


def _fetch_dividends(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """拉每只 ETF 的现金分红序列（除息日+每股现金），磁盘缓存复用（回测常做，省调用）。
    某只拉取失败按无分红处理（除息日分红现金少，不影响结论，但会打印警告）。"""
    cache_dir = config.CACHE_DIR / "backtest_data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for code in codes:
        fp = cache_dir / f"{code}_div_{start}_{end}.parquet"
        if fp.exists() and not _FORCE_REFRESH:
            try:
                out[code] = pd.read_parquet(fp)
                continue
            except Exception:
                pass
        try:
            dv = data_loader.get_etf_dividends_ts(code, start, end)
        except Exception as ex:
            print(f"[warn] {code} 分红拉取失败，按无分红处理: {str(ex)[:60]}")
            dv = pd.DataFrame(columns=["cash_per_share"])
        out[code] = dv
        try:
            dv.to_parquet(fp)
        except Exception:
            pass
    return out


def _slice(df: pd.DataFrame, as_of: dt.date, min_rows: int = 120) -> pd.DataFrame:
    """取截至 as_of 的历史，且至少 min_rows 天（不足返回空）"""
    d = df[df.index <= pd.Timestamp(as_of)]
    return d if len(d) >= min_rows else pd.DataFrame()


def _solve_target(hist: dict[str, pd.DataFrame], as_of: dt.date,
                  capital: float) -> dict:
    """
    用截至 as_of 的数据重算目标权重+整手股数。
    返回 {"weights": 理论权重, "realized": 整手后真实权重, "shares": 股数, "prices": {code: 价格}}
    """
    usable = {c: _slice(df, as_of) for c, df in hist.items()}
    usable = {c: df for c, df in usable.items() if not df.empty}
    pmx = optimizer.build_weekly_returns(usable)
    if pmx.shape[1] < 2:
        raise ValueError(f"{as_of} 可投标的不足2只，无法求解")
    weights, method, dropped = optimizer.solve_portfolio(pmx)
    prices = {c: float(df["close"].iloc[-1]) for c, df in usable.items() if not df.empty}
    prices = {c: p for c, p in prices.items() if p > 0}
    lot = optimizer.lot_size_check_and_round(weights, prices, capital)
    return {"weights": weights, "realized": lot["realized_weights"],
            "shares": lot["shares"], "prices": prices}


def run_backtest(codes: list[str] = None, capital: float = 50000.0,
                 years: float = 3.0, start_date: dt.date = None,
                 end_date: dt.date = None, hist: dict[str, pd.DataFrame] = None,
                 dividends: dict[str, pd.DataFrame] = None,
                 slippage_rate: float = None, commission_rate: float = None,
                 commission_min: float = None, verbose: bool = True) -> dict:
    """
    纯ETF历史回测主入口。
    codes: 标的池（默认 config.ETF_POOL）。
    capital: 起始资金。
    years: 回测年数（从今天往前推；仅当未显式给 start_date/end_date 时生效）。
    start_date / end_date: 显式起止日期（dt.date），用于滚动/极端窗口回测。
    hist / dividends: 外部预取的行情与分红 dict（避免每个窗口重复拉接口）；
            提供则跳过内部拉取。行情需含 close 列，index 为 DatetimeIndex。
    slippage_rate / commission_rate / commission_min: 覆盖全局成本假设（None=用模块默认）。
    verbose: False 时静默常规日志（批量回测用）。
    """
    codes = codes or config.ETF_POOL
    if start_date is None or end_date is None:
        e0 = end_date or dt.date.today()
        s0 = start_date or (e0 - dt.timedelta(days=int(years * 365)))
    else:
        e0, s0 = end_date, start_date
    # 回测起点前需 ~2年周线做协方差，故历史再往前多拉（仅自拉行情时用到）
    hist_start = s0 - dt.timedelta(days=int(config.WEEKLY_LOOKBACK_WEEKS * 7) + 90)

    # 成本假设局部覆盖（遮蔽模块级 _order_cost 默认值，滚动/敏感性批量跑用）
    cr = commission_rate if commission_rate is not None else COMMISSION_RATE
    cm = commission_min if commission_min is not None else COMMISSION_MIN
    sr = slippage_rate if slippage_rate is not None else SLIPPAGE_RATE

    def _order_cost(amount: float) -> float:
        return max(cm, amount * cr) + amount * sr

    if verbose:
        yrs = (e0 - s0).days / 365.25
        print(f"回测区间: {s0} ~ {e0}（{yrs:.2f} 年），资金 {capital:.0f}，"
              f"标的 {len(codes)} 只，滑点 {sr:.4%}")

    if hist is None:
        hist = _fetch_close_history(codes, hist_start.isoformat(), e0.isoformat(),
                                    bt_start=s0)
    else:
        hist = {c: df for c, df in hist.items() if c in codes
                and df is not None and len(df)}
    if len(hist) < 2:
        raise RuntimeError("有效历史数据不足2只，终止")

    # 现金分红：除息日+每股现金，预转 {code: {Timestamp: 每股现金}}（当日到账可再配置）
    if dividends is None:
        dividends = _fetch_dividends(codes, hist_start.strftime("%Y%m%d"),
                                     e0.strftime("%Y%m%d"))
    div_map = {c: {pd.Timestamp(i): float(row["cash_per_share"])
                   for i, row in dv.iterrows()}
               for c, dv in dividends.items() if len(dv)}
    if verbose:
        for c in codes:
            if c in hist:
                first = hist[c].index.min().date()
                print(f"  {c} 历史起点: {first} 行数 {len(hist[c])}")
                if first > s0:
                    print(f"  [note] {c} 在回测起点时数据尚不足(未满上市/数据缺失)，将延后参与")

    # 统一交易日历 = 所有可交易标的的日期并集（从 start_date 起）
    idx_list = [hist[c].index for c in hist if not hist[c].empty]
    if not idx_list:
        raise RuntimeError("无交易日历")
    cal = idx_list[0]
    for x in idx_list[1:]:
        cal = cal.union(x)
    cal = cal[(cal >= pd.Timestamp(s0)) & (cal <= pd.Timestamp(e0))]
    if len(cal) == 0:
        raise RuntimeError("回测区间内无交易日")

    # ---------------- 模拟状态 ----------------
    holdings: dict[str, int] = {}   # {code: 股数}
    cash = capital
    target_realized: dict = {}
    last_rebalance: dt.date | None = None
    nav_series = []                 # (date, 净值=总市值/本金)
    value_series = []               # (date, 总市值)
    fee_paid = 0.0
    div_total = 0.0              # 累计收到现金分红（已进 cash 可再配置）
    n_rebalance = 0
    weight_history: list[tuple[str, dict]] = []
    trades: list[dict] = []         # 每笔成交记录

    def _total_value(d: pd.Timestamp) -> float:
        v = cash
        for c, sh in holdings.items():
            df = hist.get(c)
            if df is None or df.empty:
                continue
            px = df.loc[:d]["close"]
            if len(px):
                v += sh * float(px.iloc[-1])
        return v

    def _trade_to(target: dict, d: pd.Timestamp, value: float):
        """把 holdings 调到 target(含shares/prices)。返回本笔花费的摩擦成本。"""
        nonlocal cash, fee_paid, holdings, trades
        tgt_shares = target["shares"]
        cost_sum = 0.0
        # 先卖后买，避免资金不足；交易顺序按代码排序保证可复现（不能遍历 set，哈希随机）
        for code in sorted(set(holdings) | set(tgt_shares)):
            df = hist.get(code)
            if df is None or df.empty:
                continue
            px_row = df.loc[:d]["close"]
            if not len(px_row):
                continue
            px = float(px_row.iloc[-1])
            cur = holdings.get(code, 0)
            tgt = int(tgt_shares.get(code, 0))
            delta = tgt - cur
            if delta == 0:
                continue
            amount = abs(delta) * px
            cost = _order_cost(amount)
            if delta > 0:  # 买
                need = amount + cost
                if need > cash:
                    # 现金不足，最多买 cash 能买到的整手
                    buyable_lots = int((cash - _order_cost(px)) // (px * 100)) if cash > cost else 0
                    lots = min(abs(delta) // 100, buyable_lots)
                    delta = lots * 100
                    amount = delta * px
                    cost = _order_cost(amount) if delta else 0.0
                    if delta <= 0:
                        continue
                cash -= amount + cost
                holdings[code] = holdings.get(code, 0) + delta
            else:  # 卖
                cash += amount - cost
                holdings[code] = holdings.get(code, 0) + delta
                if holdings[code] <= 0:
                    holdings.pop(code, None)
            trades.append({"date": str(d.date()), "code": code,
                           "action": "买" if delta > 0 else "卖",
                           "shares": abs(delta), "price": round(px, 3),
                           "amount": round(amount, 2), "cost": round(cost, 2)})
            cost_sum += cost
        fee_paid += cost_sum

    # ---------------- 模拟循环 ----------------
    first = True
    for d in cal:
        # 当日除息的持仓标的：现金分红到账（收盘后），计入账户现金，后续调仓可再配置
        for code, m in div_map.items():
            sh = holdings.get(code, 0)
            cps = m.get(d)
            if sh and cps:
                amt = sh * cps
                cash += amt
                div_total += amt
        if first:
            # 建仓：第一个交易日，用截至当天的数据求解并建仓
            target = _solve_target(hist, d.date(), capital)
            _trade_to(target, d, capital)
            last_rebalance = d.date()
            target_realized = target["realized"]
            weight_history.append((d.date().isoformat(), dict(target["realized"])))
            n_rebalance += 1
            first = False
            nav_series.append((d, _total_value(d) / capital))
            continue

        value = _total_value(d)
        if value <= 0:
            nav_series.append((d, 0.0))
            continue

        # 当前真实权重（整手持仓市值占比）
        cur_w = {}
        for c, sh in holdings.items():
            df = hist.get(c)
            if df is None or df.empty:
                continue
            px_row = df.loc[:d]["close"]
            if len(px_row):
                cur_w[c] = round((sh * float(px_row.iloc[-1])) / value, 6)
        drift = rebalancer.compute_drift(cur_w, target_realized)
        trig, reason = rebalancer.should_trigger_rebalance(
            drift, last_rebalance.isoformat(), today=d.date())

        if trig:
            try:
                new_target = _solve_target(hist, d.date(), value)
            except Exception as ex:
                if verbose:
                    print(f"[warn] {d.date()} 重算目标失败({ex})，跳过本次调仓")
                nav_series.append((d, value / capital))
                continue
            if new_target["shares"]:
                _trade_to(new_target, d, value)
                target_realized = new_target["realized"]
                last_rebalance = d.date()
                weight_history.append((d.date().isoformat(), dict(new_target["realized"])))
                n_rebalance += 1
        nav_series.append((d, _total_value(d) / capital))

    # ---------------- 汇总（尽量记录全） ----------------
    nav = pd.Series({dd: v for dd, v in nav_series}).sort_index()
    nav.index = pd.to_datetime(nav.index)
    value_abs = nav * capital
    rets = nav.pct_change().dropna()

    # 基准：510300 沪深300，归一化起点1.0（价格基准；ETF分红少，近似）
    bench_close = hist.get("510300")
    b = pd.Series(dtype=float)
    if bench_close is not None:
        b = bench_close.loc[nav.index.min():]["close"]
        b = b / float(b.iloc[0])

    # 红利ETF总回报基准（510880，含现金分红）：分红现金逐日累计进净值，
    # 与策略"分红进账户"同口径，避免用纯价格线低估红利ETF的收益。
    red_bench = pd.Series(dtype=float)
    red_close = hist.get("510880")
    div_df = dividends.get("510880")
    if red_close is not None and div_df is not None and len(div_df):
        seg = red_close.loc[nav.index.min():]["close"]
        if len(seg):
            base0 = float(seg.iloc[0])
            dsub = div_df["cash_per_share"].astype(float)
            dsub = dsub[dsub.index >= nav.index.min()].cumsum().sort_index()
            cum = (dsub.reindex(seg.index.union(dsub.index)).ffill()
                       .reindex(seg.index).fillna(0.0))
            red_bench = (seg + cum) / base0
    red_ret = float(red_bench.iloc[-1] - 1) if len(red_bench) else None
    red_dd = float((red_bench / red_bench.cummax() - 1).min()) if len(red_bench) else None

    total_ret = float(nav.iloc[-1] - 1)
    n = len(rets)
    ann_ret = float(nav.iloc[-1] ** (252 / n) - 1) if n > 0 and nav.iloc[-1] > 0 else -1.0
    ann_vol = float(rets.std() * (252 ** 0.5)) if n > 1 else 0.0
    sharpe = (ann_ret - config.RISK_FREE_RATE) / ann_vol if ann_vol > 0 else 0.0
    drawdown = nav / nav.cummax() - 1.0
    max_dd = float(drawdown.min()) if len(drawdown) else 0.0
    win_rate = float((rets > 0).mean()) if n else 0.0
    best_day = float(rets.max()) if n else 0.0
    worst_day = float(rets.min()) if n else 0.0
    calmar = ann_ret / abs(max_dd) if max_dd < 0 else float("nan")
    monthly = ((1 + rets).resample("ME").prod() - 1) if n else pd.Series(dtype=float)
    bench_ret = float(b.iloc[-1] - 1) if len(b) else None
    bench_dd = float((b / b.cummax() - 1).min()) if len(b) else None

    # 期末持仓快照
    end_holdings = dict(holdings)
    end_value = value_abs.iloc[-1] if len(value_abs) else float("nan")

    result = {
        # 区间与资金
        "start": str(cal[0].date()), "end": str(cal[-1].date()),
        "capital": capital, "final_value": float(end_value),
        # 收益指标
        "total_ret": total_ret, "annual_ret": ann_ret, "annual_vol": ann_vol,
        "sharpe": sharpe, "max_dd": max_dd, "calmar": calmar,
        "win_rate": win_rate, "best_day": best_day, "worst_day": worst_day,
        # 基准对照
        "bench_ret": bench_ret, "bench_max_dd": bench_dd,
        "excess_ret": (total_ret - bench_ret) if bench_ret is not None else None,
        "bench_red_ret": red_ret, "bench_red_max_dd": red_dd,
        # 成本与交易
        "fees_paid": fee_paid, "dividends_received": div_total,
        "rebalances": n_rebalance,
        "n_trades": len(trades),
        # 序列（供画图/存档）
        "nav": nav, "bench": b, "bench_red": red_bench,
        "drawdown": drawdown, "monthly_returns": monthly,
        # 过程记录
        "weight_history": weight_history, "trades": trades,
        "end_holdings": end_holdings,
        "candidates": list(hist.keys()),
    }
    return result


def _save_report(r: dict, out_html: str = None, out_json: str = None) -> None:
    """把回测结果落盘：HTML 图报（plotly）+ JSON 汇总。"""
    try:
        import json
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        return

    has_red = len(r.get("bench_red", pd.Series(dtype=float)))
    sub_titles = ["策略 vs 沪深300 vs 红利ETF(含分红)", "回撤"] if has_red \
        else ["策略 vs 沪深300 净值", "回撤"]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3],
                        subplot_titles=sub_titles)
    fig.add_trace(go.Scatter(x=r["nav"].index, y=r["nav"], name="策略净值"), row=1, col=1)
    if len(r["bench"]):
        fig.add_trace(go.Scatter(x=r["bench"].index, y=r["bench"], name="沪深300"), row=1, col=1)
    if has_red:
        fig.add_trace(go.Scatter(x=r["bench_red"].index, y=r["bench_red"],
                                 name="红利ETF(含分红)", line=dict(color="rgb(178,34,34)")),
                      row=1, col=1)
    fig.add_trace(go.Scatter(x=r["drawdown"].index, y=r["drawdown"], name="策略回撤",
                             fill="tozeroy"), row=2, col=1)
    fig.update_layout(height=720, title="quant_portfolio 历史回测",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02))

    html = fig.to_html(include_plotlyjs="cdn", full_html=True,
                       div_id="backtest")
    html_path = Path(out_html or (config.CACHE_DIR / "backtest_report.html"))
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")

    # JSON 汇总（只存可序列化内容；不能用 hasattr(v,"index")——字符串也有 .index() 会误杀 start/end）
    summary = {k: v for k, v in r.items()
               if not isinstance(v, (pd.Series, pd.DataFrame))
               and k not in ("weight_history", "trades")}
    summary["trades_count"] = len(r["trades"])
    summary["weight_history"] = r["weight_history"]
    summary["trades"] = r["trades"]
    json_path = Path(out_json or (config.CACHE_DIR / "backtest_result.json"))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                         encoding="utf-8")
    return str(html_path), str(json_path)


def _print_summary(r: dict) -> None:
    print("\n========== 回测结果 ==========")
    print(f"区间: {r['start']} ~ {r['end']}（{r['rebalances']} 次调仓，{r['n_trades']} 笔交易）")
    print(f"初始资金: {r['capital']:.0f} | 期末: {r['final_value']:.0f} | "
          f"累计收益: {r['total_ret']:+.2%} | 年化: {r['annual_ret']:+.2%}")
    cal_txt = 'N/A' if r['calmar'] != r['calmar'] else f"{r['calmar']:.2f}"
    print(f"年化波动: {r['annual_vol']:.2%} | 夏普: {r['sharpe']:.2f} | Calmar: {cal_txt}")
    print(f"最大回撤: {r['max_dd']:.2%} | 日胜率: {r['win_rate']:.1%} | "
          f"最佳日: {r['best_day']:+.2%} | 最差日: {r['worst_day']:+.2%}")
    print(f"累计摩擦成本: ￥{r['fees_paid']:.0f}（占总期末值 {r['fees_paid']/r['final_value']:.2%}）")
    print(f"累计现金分红: ￥{r['dividends_received']:.0f}（占总期末值 {r['dividends_received']/r['final_value']:.2%}）")
    if r['bench_ret'] is not None:
        print(f"同期沪深300: {r['bench_ret']:+.2%} | 超额: {r['excess_ret']:+.2%} | "
              f"基准最大回撤: {r['bench_max_dd']:.2%}")
    if r.get('bench_red_ret') is not None:
        print(f"同期红利ETF(含分红): {r['bench_red_ret']:+.2%} | "
              f"红利最大回撤: {r['bench_red_max_dd']:.2%}")
    print(f"期末持仓: {r['end_holdings']}")


if __name__ == "__main__":
    import sys
    import argparse
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="quant_portfolio 历史回测（纯ETF段）")
    parser.add_argument("--years", type=float, default=3.0, help="回测年数（默认3）")
    parser.add_argument("--capital", type=float, default=50000.0, help="起始本金（默认50000）")
    parser.add_argument("--refresh", action="store_true", help="忽略当天缓存强制重新拉数据")
    args = parser.parse_args()
    _FORCE_REFRESH = args.refresh
    print(f"运行参数: years={args.years} capital={args.capital:.0f} refresh={args.refresh}")
    r = run_backtest(years=args.years, capital=args.capital)
    _print_summary(r)
    paths = _save_report(r)
    if paths:
        print(f"\n报告已保存: {paths[0]}")
        print(f"JSON 汇总已保存: {paths[1]}")
