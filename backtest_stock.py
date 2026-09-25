# -*- coding: utf-8 -*-
"""
backtest_stock.py
纯股票段【历史回测】引擎。

语义（2026-09 用户拍板）：
  · 预热期 2001-01-01 ~ 2002-12-31（不计收益，让"近3年ROE"等回溯因子在正式期首日就有值）
  · 正式期 2003-01-01 ~ 今日
  · 调仓规则沿用实盘：每日查偏离，>5% 触发（距上次≥30天）、>10% 紧急触发
  · **触发即重筛**：用【当时(≤D)】的数据重跑 全市场防雷 → 因子打分 → Top15 → 优化 → 整手
  · 真实成本：佣金(万0.5, 5元起) + 印花税(卖出万5) + 单边滑点0.05%

数据来源：CSMAR（PIT 财报/ST/退市/申万 + 25年日行情）。
候选池与资金量无关 → 一次预计算，多档资金复用。
"""
from __future__ import annotations
import datetime as dt
import logging

import numpy as np
import pandas as pd

import config
import csmar_loader as cl
import csmar_screen as cs
import optimizer
import rebalancer

logger = logging.getLogger("backtest_stock")

# ---- 成本（用户账户真实费率，2026-09-19 用户提供）----
# 沪深股票：佣金万0.9、0.5元起步；过户费万0.1（买卖双向）；印花税万5（仅卖出，国家规定）
COMMISSION_RATE = 0.00009     # 万0.9
COMMISSION_MIN = 0.5          # 0.5 元起步
TRANSFER_FEE_RATE = 0.00001   # 过户费 万0.1（双向）
STAMP_TAX_RATE = 0.0005       # 印花税 万5（仅卖出）
SLIPPAGE_RATE = 0.0005        # 滑点 0.05%（用户未指定，保守估计）


def _cost(amount: float, is_sell: bool) -> float:
    """单笔交易成本 = 佣金(0.5元起) + 过户费 + 滑点 + 卖出印花税"""
    comm = max(COMMISSION_MIN, amount * COMMISSION_RATE)
    return (comm + amount * TRANSFER_FEE_RATE + amount * SLIPPAGE_RATE
            + (amount * STAMP_TAX_RATE if is_sell else 0.0))


# ---------------------------------------------------------------------------
# 行情：加载 + 按日切片
# ---------------------------------------------------------------------------

_MKT_COLS = ["code", "trade_date", "close", "amount", "trade_status", "ret_wd", "total_mv"]


def load_market() -> pd.DataFrame:
    """加载全部25年行情（只保留回测需要的列），按 trade_date 排序。
    ret_wd = 考虑现金红利再投资的日个股回报率（小数），用于处理股票分红。
    total_mv = 日个股总市值（千元），打分做市值中性化要用。"""
    df = cl.load_market_daily()
    df = df[_MKT_COLS].copy()
    for c in ("close", "amount", "trade_status", "ret_wd", "total_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["trade_date", "code"]).reset_index(drop=True)


class Market:
    """按日切片访问器。用 searchsorted 定位日期边界切片，不复制整表（省内存）。"""

    def __init__(self, df: pd.DataFrame):
        self.df = df.sort_values(["trade_date", "code"]).reset_index(drop=True)
        self.dates = np.array(sorted(df["trade_date"].unique()))
        # 每个交易日的行号边界（左闭右开）
        self._pos = np.searchsorted(self.df["trade_date"].values, self.dates)
        self._pos = np.append(self._pos, len(self.df))
        self._pb = None

    def _slice(self, i0: int, i1: int) -> pd.DataFrame:
        return self.df.iloc[self._pos[i0]:self._pos[i1]]

    def calendar(self, start: dt.date, end: dt.date) -> list[pd.Timestamp]:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        return [d for d in self.dates if s <= d <= e]

    def snapshot(self, as_of: pd.Timestamp, lookback_trading_days: int = 25) -> pd.DataFrame:
        """截至 as_of 的每只股票：latest_close / avg_amount_20d / trade_status。"""
        idx = int(np.searchsorted(self.dates, np.datetime64(as_of), side="right"))
        idx = min(idx, len(self.dates))
        lo = max(0, idx - lookback_trading_days)
        sub = self._slice(lo, idx).sort_values(["code", "trade_date"])
        g = sub.groupby("code", sort=False)
        return pd.DataFrame({
            "latest_close": g["close"].last(),
            "avg_amount_20d": g["amount"].apply(lambda s: s.tail(20).mean()),
            "trade_status": g["trade_status"].last(),
            "total_mv": g["total_mv"].last(),
        })

    def pricebook(self, calendar: list[pd.Timestamp]) -> "PriceBook":
        """复用同一个 PriceBook（同一区间多档回测时只构建一次）。"""
        if self._pb is None:
            self._pb = PriceBook(self, calendar)
        return self._pb


# ---------------------------------------------------------------------------
# 候选池预计算（与资金无关）
# ---------------------------------------------------------------------------

_REFRESH_MD = [(4, 21), (5, 1), (8, 31), (10, 20)]   # 一季报4/20后、年报5/1后、中报8/31后、三季报10/20后


def pool_refresh_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    for y in range(start.year, end.year + 1):
        for m, d in _REFRESH_MD:
            dt_ = dt.date(y, m, d)
            if start <= dt_ <= end:
                out.append(dt_)
    return sorted(out)


def precompute_pools(mkt: Market, start: dt.date, end: dt.date,
                     top_n: int = None, verbose: bool = True) -> dict[str, dict]:
    """对每个刷新日做一次：全市场防雷筛选 → 打分 → Top N。返回 {日期: {pool, profiles}}"""
    top_n = top_n or config.STOCK_TOP_N
    out = {}
    dates = pool_refresh_dates(start, end)
    for i, d in enumerate(dates, 1):
        ts = pd.Timestamp(d)
        if ts > mkt.dates[-1]:
            continue
        snap = mkt.snapshot(ts)
        scr = cs.screen_market(as_of=d, snapshot=snap, verbose=False)
        passed = scr[scr["passed"] & scr["latest_close"].notna()]
        if len(passed) < top_n:
            continue
        prof = cs.build_profiles(list(passed.index), as_of=d, snapshot=passed)
        import factor_engine
        scored = factor_engine.score_stocks_fundamental(prof)
        out[ts] = {"pool": list(scored.head(top_n).index),
                   "profiles": {p["code"]: p for p in prof}}
        if verbose and (i % 10 == 0 or i == len(dates)):
            logger.info(f"  预计算候选池 [{i}/{len(dates)}] {d} 通过{len(passed)}只→Top{top_n}")
    return out


# ---------------------------------------------------------------------------
# 价格/收益访问器
# ---------------------------------------------------------------------------

class PriceBook:
    """按代码缓存：实际收盘价序列 + 含红利再投资的累计收益指数 TR。
    估值走"总收益口径"，以正确处理股票分红（我们没有分红除息日，但有 Dretwd）。
    """

    def __init__(self, mkt: Market, calendar: list[pd.Timestamp]):
        self.mkt = mkt
        self.cal = pd.DatetimeIndex(calendar)
        self._px: dict[str, pd.Series] = {}
        self._tr: dict[str, pd.Series] = {}
        self._df = mkt.df.set_index(["code", "trade_date"]).sort_index()

    def _series(self, code: str):
        if code in self._px:
            return
        try:
            sub = self._df.loc[code].reindex(self.cal)
        except KeyError:
            self._px[code] = pd.Series(np.nan, index=self.cal)
            self._tr[code] = pd.Series(np.nan, index=self.cal)
            return
        px = pd.to_numeric(sub["close"], errors="coerce").ffill()
        r = pd.to_numeric(sub["ret_wd"], errors="coerce").fillna(0.0)
        self._px[code] = px
        self._tr[code] = (1.0 + r).cumprod()

    def px(self, code: str, i: int) -> float:
        self._series(code)
        v = self._px[code].iloc[i]
        return float(v) if pd.notna(v) else np.nan

    def tr(self, code: str, i: int) -> float:
        self._series(code)
        v = self._tr[code].iloc[i]
        return float(v) if pd.notna(v) else np.nan

    def hist_close(self, code: str, i: int, lookback: int = 500) -> pd.Series:
        """截至第 i 天、往前 lookback 天的收盘价序列（用于协方差/优化）。"""
        self._series(code)
        lo = max(0, i - lookback)
        return self._px[code].iloc[lo:i + 1].dropna()


# ---------------------------------------------------------------------------
# 回测主循环
# ---------------------------------------------------------------------------

def _weekly_matrix(pdata: dict[str, pd.Series]) -> pd.DataFrame:
    """{code: 收盘价Series(日线)} → 周线收盘价矩阵（协方差/优化用）。
    回测里只有收盘价，故不复用 data_loader.to_weekly（那个要 OHLCV 全列）。"""
    out = {}
    for c, s in pdata.items():
        w = s.resample("W-FRI").last().dropna()
        if len(w) >= 20:
            out[c] = w.tail(config.WEEKLY_LOOKBACK_WEEKS)
    return pd.DataFrame(out).dropna(how="all")


def run_backtest(capital: float, mkt: Market, pools: dict, start: dt.date, end: dt.date,
                 label: str = "", verbose: bool = True,
                 warm_start: dt.date = None) -> dict:
    warm_start = warm_start or start
    cal_all = mkt.calendar(warm_start, end)          # 含预热期（供 hist_close 往前取）
    cal = [d for d in cal_all if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    if len(cal) < 50:
        raise RuntimeError(f"{label} 回测区间内交易日不足")
    offset = cal_all.index(cal[0])                    # 正式期在 cal_all 中的起始下标
    pool_dates = sorted(pools.keys())
    if not pool_dates:
        raise RuntimeError(f"{label} 无可用候选池")

    pb = mkt.pricebook(cal_all)   # 同一区间多档回测复用同一个 PriceBook（省内存/时间）
    holdings: dict[str, dict] = {}      # code -> {shares, k}；k = 买入价/买入时TR，用于总收益估值
    cash = capital
    target: dict = {}
    last_reb: dt.date | None = None
    nav_log, rebal_log, trades = [], [], []

    def value(i: int) -> float:
        v = cash
        for c, h in holdings.items():
            tr = pb.tr(c, i)
            if pd.notna(tr) and h["shares"]:
                v += h["shares"] * h["k"] * tr
        return v

    def pool_at(d: pd.Timestamp):
        avail = [x for x in pool_dates if x <= d]
        return pools[avail[-1]] if avail else None

    for j, d in enumerate(cal):
        i = offset + j              # 在 cal_all（含预热期）中的下标，供 hist_close 回溯
        val = value(i)

        if not target:
            trig, reason = True, "首次建仓"
        else:
            cur_w = {}
            if val > 0:
                for c, h in holdings.items():
                    tr = pb.tr(c, i)
                    if pd.notna(tr):
                        cur_w[c] = h["shares"] * h["k"] * tr / val
            drift = rebalancer.compute_drift(cur_w, target)
            trig, reason = rebalancer.should_trigger_rebalance(
                drift, last_reb.isoformat() if last_reb else None, today=d.date())

        if trig:
            entry = pool_at(d)
            codes = entry["pool"] if entry else None
            if not codes:
                nav_log.append((d, val / capital))
                continue
            # 因子打分 → 融入优化器的预期收益（问题B修复）
            import factor_engine
            prof_map = entry.get("profiles") or {}
            profs = [prof_map[c] for c in codes if c in prof_map]
            alpha = {}
            if len(profs) >= 2:
                try:
                    alpha = factor_engine.score_stocks_fundamental(profs)["alpha_score"].to_dict()
                except Exception as e:
                    if verbose:
                        logger.warning(f"  {d.date()} 打分失败({str(e)[:50]})，本次不叠加因子")
            pdata = {}
            for c in codes:
                s = pb.hist_close(c, i, 500)
                if len(s) >= 60:
                    pdata[c] = s
            if len(pdata) < 2:
                nav_log.append((d, val / capital))
                continue
            pmx = _weekly_matrix(pdata)
            try:
                weights, method, dropped = optimizer.solve_portfolio(pmx, alpha_scores=alpha)
            except Exception as e:
                # 极端情况兜底（solve_portfolio 内部已含 MaxSharpe→HRP→等权 逐级退回）
                if verbose:
                    logger.info(f"  {d.date()} 求解失败({str(e)[:50]})，跳过本次调仓")
                nav_log.append((d, val / capital))
                continue
            latest = {c: pb.px(c, i) for c in weights}
            latest = {c: p for c, p in latest.items() if pd.notna(p) and p > 0}
            if len(latest) < 2:
                nav_log.append((d, val / capital))
                continue
            lot = optimizer.lot_size_check_and_round(weights, latest, val)
            tgt_sh = lot["shares"]

            # ---- 交易：★真正的"先卖后买"（两轮），卖出的现金先回笼再买 ----
            fee = 0.0

            def _trade_pass(do_sell: bool):
                nonlocal cash, fee
                for c in sorted(set(holdings) | set(tgt_sh)):
                    p, tr = pb.px(c, i), pb.tr(c, i)
                    if pd.isna(p) or pd.isna(tr) or p <= 0:
                        continue
                    cur_sh = holdings.get(c, {}).get("shares", 0)
                    tgt = int(tgt_sh.get(c, 0))
                    delta = tgt - cur_sh
                    if delta == 0 or (delta < 0) != do_sell:
                        continue
                    amount = abs(delta) * p
                    cost = _cost(amount, delta < 0)
                    if delta > 0:
                        if amount + cost > cash:
                            lots = int((cash - _cost(p * 100, False)) // (p * 100))
                            delta = min(delta // 100, max(lots, 0)) * 100
                            amount = delta * p
                            cost = _cost(amount, False) if delta > 0 else 0.0
                            if delta <= 0:
                                continue
                        cash -= amount + cost
                        old = holdings.get(c, {"shares": 0, "k": p / tr})
                        tot = old["shares"] + delta
                        holdings[c] = {"shares": tot,
                                       "k": (old["k"] * old["shares"] + (p / tr) * delta) / tot}
                    else:
                        cash += amount - cost
                        holdings[c]["shares"] += delta
                        if holdings[c]["shares"] <= 0:
                            holdings.pop(c, None)
                    trades.append({"date": str(d.date()), "code": c,
                                   "action": "买" if delta > 0 else "卖",
                                   "shares": abs(delta), "price": round(p, 2),
                                   "amount": round(amount, 2), "cost": round(cost, 2)})
                    fee += cost

            _trade_pass(True)    # 第一轮：只卖（回笼现金）
            _trade_pass(False)   # 第二轮：只买（此时现金充足）

            pool_px = {c: pb.px(c, i) for c in codes}
            pool_px = {c: p for c, p in pool_px.items() if pd.notna(p)}
            rebal_log.append({
                "date": str(d.date()), "reason": reason, "n_pool": len(codes),
                "n_hold": len(tgt_sh), "n_excluded": len(lot["excluded"]),
                "n_small": len(dropped), "fee": round(fee, 2), "method": method,
                "pool": list(codes),
                "expensive_in_pool": [c for c, p in pool_px.items() if p * 100 > 50000],
                "holdings": dict(tgt_sh),
            })
            target = lot["realized_weights"]
            last_reb = d.date()

        nav_log.append((d, value(i) / capital))

    return _summarize(nav_log, rebal_log, trades, capital, label,
                      {c: h["shares"] for c, h in holdings.items()})


def _summarize(nav_log, rebal_log, trades, capital, label, end_holdings) -> dict:
    nav = pd.Series({d: v for d, v in nav_log}).sort_index()
    rets = nav.pct_change().dropna()
    n = len(rets)
    total = float(nav.iloc[-1] - 1)
    ann = float(nav.iloc[-1] ** (252 / n) - 1) if n > 0 and nav.iloc[-1] > 0 else -1.0
    vol = float(rets.std() * (252 ** 0.5)) if n > 1 else 0.0
    sharpe = (ann - config.RISK_FREE_RATE) / vol if vol > 0 else 0.0
    dd = nav / nav.cummax() - 1.0
    max_dd = float(dd.min())
    return {
        "label": label, "capital": capital,
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "total_ret": total, "annual_ret": ann, "annual_vol": vol,
        "sharpe": sharpe, "max_dd": max_dd,
        "calmar": ann / abs(max_dd) if max_dd < 0 else float("nan"),
        "n_rebalance": len(rebal_log), "n_trades": len(trades),
        "fees": float(sum(t["cost"] for t in trades)),
        "nav": nav, "rebal_log": rebal_log, "trades": trades,
        "end_holdings": end_holdings,
    }


# ---------------------------------------------------------------------------
# 主入口：三档资金对比
# ---------------------------------------------------------------------------

def _fmt_pct(x):
    return "N/A" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.2%}"


def _print_compare(results: list[dict], expensive_codes: list[str] = None) -> None:
    import csmar_loader as _cl
    nm = _cl.get_all_names()

    print("\n" + "=" * 92)
    print("三档资金量 · 纯股票回测对比")
    print("=" * 92)

    # 1) 核心指标
    print(f"\n{'资金':>6} | {'区间':>23} | {'累计':>9} | {'年化':>8} | {'年化波动':>8} | {'夏普':>6} | {'最大回撤':>9} | {'调仓':>4}")
    print("-" * 100)
    for r in results:
        print(f"{r['capital']/10000:>5.0f}万 | {r['start']} ~ {r['end']} | "
              f"{_fmt_pct(r['total_ret']):>9} | {_fmt_pct(r['annual_ret']):>8} | "
              f"{r['annual_vol']:>7.2%} | {r['sharpe']:>6.2f} | {r['max_dd']:>9.2%} | {r['n_rebalance']:>4}")

    # 2) 调仓/整手诊断
    print(f"\n【调仓与买入诊断】")
    print(f"{'资金':>6} | {'平均候选池':>10} | {'平均买入只数':>12} | {'1手剔除(均)':>12} | {'权重过小(均)':>13} | {'累计成本':>10}")
    print("-" * 90)
    for r in results:
        rl = r["rebal_log"]
        n = max(len(rl), 1)
        print(f"{r['capital']/10000:>5.0f}万 | {sum(x['n_pool'] for x in rl)/n:>10.1f} | "
              f"{sum(x['n_hold'] for x in rl)/n:>12.1f} | {sum(x['n_excluded'] for x in rl)/n:>12.1f} | "
              f"{sum(x['n_small'] for x in rl)/n:>13.1f} | ¥{r['fees']:>9,.0f}")

    # 3) 高价股入选频率
    if expensive_codes:
        print(f"\n【高价股入选情况】（1手价>5万 或 指定标的）")
        print(f"{'资金':>6} | {'调仓次数':>8} | " + " | ".join(f"{c} {nm.get(c,'')}" for c in expensive_codes[:4]))
        print("-" * 90)
        for r in results:
            rl = r["rebal_log"]
            row = [f"{r['capital']/10000:>5.0f}万", f"{len(rl):>8}"]
            for c in expensive_codes[:4]:
                in_pool = sum(1 for x in rl if c in x["pool"])
                in_hold = sum(1 for x in rl if c in x["holdings"])
                row.append(f"{in_pool}次/{in_hold}持")
            print(" | ".join(row))
        # 高价股（>5万/手）整体出现在候选池的频率
        print(f"\n  候选池中出现过【1手价>5万】的高价股次数：")
        for r in results:
            rl = r["rebal_log"]
            n_exp = sum(1 for x in rl if x["expensive_in_pool"])
            tot = sum(len(x["expensive_in_pool"]) for x in rl)
            print(f"    {r['capital']/10000:>5.0f}万: {n_exp}/{len(rl)} 次调仓中出现，累计 {tot} 只次")


def main():
    import sys, pickle, argparse
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ap = argparse.ArgumentParser(description="纯股票段回测（三档资金对比）")
    ap.add_argument("--warm-start", default="2001-01-01")
    ap.add_argument("--start", default="2003-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--refresh", action="store_true", help="强制重算候选池")
    ap.add_argument("--caps", default="50000,200000,1000000")
    ap.add_argument("--limit-pools", type=int, default=0, help="仅用前N个刷新日（调试用）")
    args = ap.parse_args()

    ws = dt.date.fromisoformat(args.warm_start)
    sd = dt.date.fromisoformat(args.start)
    ed = dt.date.fromisoformat(args.end)

    print("加载全市场行情...", flush=True)
    mkt = Market(load_market())
    print(f"  行情 {len(mkt.df):,} 行，{len(mkt.dates)} 个交易日", flush=True)

    cache = config.CACHE_DIR / f"stock_pools_{ws}_{ed}.pkl"
    if cache.exists() and not args.refresh:
        print(f"载入候选池缓存 {cache.name}", flush=True)
        with open(cache, "rb") as f:
            pools = pickle.load(f)
    else:
        print("预计算候选池（每季度一次，较慢）...", flush=True)
        pools = precompute_pools(mkt, ws, ed)
        with open(cache, "wb") as f:
            pickle.dump(pools, f)
    if args.limit_pools:
        pools = dict(list(sorted(pools.items()))[:args.limit_pools])
    print(f"  候选池 {len(pools)} 期", flush=True)

    caps = [float(x) for x in args.caps.split(",")]
    results = []
    for cap in caps:
        print(f"\n=== 回测 资金 {cap:,.0f} ===", flush=True)
        r = run_backtest(cap, mkt, pools, sd, ed, label=f"{cap/10000:.0f}万", warm_start=ws)
        print(f"  年化 {_fmt_pct(r['annual_ret'])}  夏普 {r['sharpe']:.2f}  最大回撤 {r['max_dd']:.2%}  "
              f"调仓 {r['n_rebalance']} 次", flush=True)
        results.append(r)

    _print_compare(results, expensive_codes=["600519"])


if __name__ == "__main__":
    main()
