# -*- coding: utf-8 -*-
"""
backtest_mixed.py
混合模式（ETF + 个股）【历史回测】引擎。

语义：
  · 预热期 2015-01-01 ~ 2016-12-31（协方差预热）
  · 正式期 2017-01-01 ~ 今日
  · 每期候选池 = 8只ETF（全保留）+ 股票Top15（全市场防雷筛选）→ 23只合并
  · 调仓规则：每日查偏离，>5% 触发（距上次≥30天）、>10% 紧急触发
  · **触发即重筛**：用【当时(≤D)】的数据重跑筛选 → 合并 → 优化 → 整手
  · 真实成本：ETF佣金万0.5，股票佣金万0.9 + 过户费 + 印花税
  · 分红处理：ETF用除息日现金，股票用含红利收益率(ret_wd)

数据来源：
  - ETF：tushare fund_daily（8只，磁盘缓存）
  - 股票：CSMAR（PIT 财报/ST/退市/申万 + 25年日行情）
"""
from __future__ import annotations
import datetime as dt
import logging
from pathlib import Path

import numpy as np
import pandas as pd

import config
import csmar_loader as cl
import csmar_screen as cs
import optimizer
import rebalancer
import data_loader

logger = logging.getLogger("backtest_mixed")

# ---- 成本 ----
COMMISSION_RATE_ETF = 0.00005       # ETF 佣金万0.5
COMMISSION_RATE_STOCK = 0.00009     # 股票佣金万0.9
COMMISSION_MIN = 0.5                # 最低0.5元
TRANSFER_FEE_RATE = 0.00001         # 过户费万0.1（双向）

# 诊断用：False = 只跑8只ETF（纯ETF基准，不含个股），True = 正常混合模式
INCLUDE_STOCKS = True
STAMP_TAX_RATE = 0.0005             # 印花税万5（仅股票卖出）
SLIPPAGE_RATE = 0.0005              # 滑点0.05%

ETF_POOL = config.ETF_POOL          # 8只ETF

def _cost(amount: float, is_sell: bool, is_etf: bool) -> float:
    """单笔交易成本"""
    rate = COMMISSION_RATE_ETF if is_etf else COMMISSION_RATE_STOCK
    comm = max(COMMISSION_MIN, amount * rate)
    fee = comm + amount * TRANSFER_FEE_RATE + amount * SLIPPAGE_RATE
    if is_sell and not is_etf:  # 股票卖出印花税
        fee += amount * STAMP_TAX_RATE
    return fee


# ---------------------------------------------------------------------------
# ETF 数据加载（磁盘缓存，参考 backtest_engine.py）
# ---------------------------------------------------------------------------

def _fetch_etf_history(codes: list[str], hist_start: str, end: str,
                       bt_start: dt.date = None) -> dict[str, pd.DataFrame]:
    """拉ETF历史（600天窗口拼接，磁盘缓存）"""
    cache_dir = config.CACHE_DIR / "backtest_data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    step = 600
    s0 = dt.date.fromisoformat(hist_start)
    e0 = dt.date.fromisoformat(end)
    for code in codes:
        fp = cache_dir / f"{code}_{hist_start}_{end}.parquet"
        if fp.exists():
            try:
                out[code] = pd.read_parquet(fp).sort_index()
                continue
            except Exception:
                pass
        frames = []
        cur = s0
        while cur <= e0:
            seg_end = min(cur + dt.timedelta(days=step - 1), e0)
            is_prewarm = bt_start is not None and seg_end < bt_start
            df = None
            for attempt in (1, 2):
                try:
                    df = data_loader.get_etf_daily_strict(code, cur.strftime("%Y%m%d"),
                                                          seg_end.strftime("%Y%m%d"))
                    break
                except Exception as ex:
                    if attempt == 1:
                        logger.info(f"[retry] {code} 第1次失败，重试")
            if df is None:
                # 还没拿到过任何数据 → 该标的当时尚未上市（如588000科创50 2020-11才上市），
                # 跳过该段继续往后拉；一旦已有数据又出现空段，才算真异常。
                if is_prewarm or not frames:
                    logger.info(f"[note] {code} {cur}~{seg_end} 无数据(当时未上市)，跳过")
                    cur = seg_end + dt.timedelta(days=1)
                    continue
                raise RuntimeError(f"{code} 拉取失败")
            if not df.empty:
                frames.append(df)
            cur = seg_end + dt.timedelta(days=1)
        if frames:
            full = pd.concat(frames)
            full = full[~full.index.duplicated(keep="last")].sort_index()
            out[code] = full
            try:
                full.to_parquet(fp)
            except Exception:
                pass
    return out


def _fetch_etf_dividends(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """拉ETF分红（磁盘缓存）"""
    cache_dir = config.CACHE_DIR / "backtest_data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for code in codes:
        fp = cache_dir / f"{code}_div_{start}_{end}.parquet"
        if fp.exists():
            try:
                out[code] = pd.read_parquet(fp)
                continue
            except Exception:
                pass
        try:
            dv = data_loader.get_etf_dividends_ts(code, start, end)
        except Exception as ex:
            logger.warning(f"{code} 分红拉取失败: {str(ex)[:60]}")
            dv = pd.DataFrame(columns=["cash_per_share"])
        out[code] = dv
        try:
            dv.to_parquet(fp)
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# 股票行情（复用 backtest_stock.py 的 Market 类）
# ---------------------------------------------------------------------------

_MKT_COLS = ["code", "trade_date", "close", "amount", "trade_status", "ret_wd", "total_mv"]

def load_stock_market() -> pd.DataFrame:
    """加载CSMAR全市场股票行情"""
    df = cl.load_market_daily()
    df = df[_MKT_COLS].copy()
    for c in ("close", "amount", "trade_status", "ret_wd", "total_mv"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["trade_date", "code"]).reset_index(drop=True)


class StockMarket:
    """股票行情按日切片访问"""
    def __init__(self, df: pd.DataFrame):
        self.df = df.sort_values(["trade_date", "code"]).reset_index(drop=True)
        self.dates = np.array(sorted(df["trade_date"].unique()))
        self._pos = np.searchsorted(self.df["trade_date"].values, self.dates)
        self._pos = np.append(self._pos, len(self.df))

    def _slice(self, i0: int, i1: int) -> pd.DataFrame:
        return self.df.iloc[self._pos[i0]:self._pos[i1]]

    def calendar(self, start: dt.date, end: dt.date) -> list[pd.Timestamp]:
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        return [d for d in self.dates if s <= d <= e]

    def snapshot(self, as_of: pd.Timestamp, lookback_trading_days: int = 25) -> pd.DataFrame:
        """截至 as_of 的股票快照"""
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


# ---------------------------------------------------------------------------
# 候选池预计算（股票部分）
# ---------------------------------------------------------------------------

_REFRESH_MD = [(4, 21), (5, 1), (8, 31), (10, 20)]

def pool_refresh_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    for y in range(start.year, end.year + 1):
        for m, d in _REFRESH_MD:
            dt_ = dt.date(y, m, d)
            if start <= dt_ <= end:
                out.append(dt_)
    return sorted(out)


def precompute_stock_pools(mkt: StockMarket, start: dt.date, end: dt.date,
                           top_n: int = None, verbose: bool = True) -> dict:
    """预计算股票候选池（每季度刷新）"""
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
            logger.info(f"  股票候选池 [{i}/{len(dates)}] {d} Top{top_n}")
    return out


# ---------------------------------------------------------------------------
# 价格账本（ETF + 股票）
# ---------------------------------------------------------------------------

class PriceBook:
    """统一访问ETF和股票的价格/TR"""
    def __init__(self, etf_hist: dict, stock_mkt: StockMarket, calendar: list[pd.Timestamp]):
        self.etf_hist = etf_hist
        self.stock_mkt = stock_mkt
        self.cal = pd.DatetimeIndex(calendar)
        self._etf_px = {}
        self._etf_tr = {}
        self._stock_px = {}
        self._stock_tr = {}
        self._stock_df = stock_mkt.df.set_index(["code", "trade_date"]).sort_index()

    def _load_etf(self, code: str):
        if code in self._etf_px:
            return
        df = self.etf_hist.get(code)
        if df is None or df.empty:
            self._etf_px[code] = pd.Series(np.nan, index=self.cal)
            self._etf_tr[code] = pd.Series(1.0, index=self.cal)
            return
        px = df["close"].reindex(self.cal).ffill()
        self._etf_px[code] = px
        # ETF TR简化：用价格（分红后续单独处理）
        self._etf_tr[code] = (px / px.iloc[0]).fillna(1.0)

    def _load_stock(self, code: str):
        if code in self._stock_px:
            return
        try:
            sub = self._stock_df.loc[code].reindex(self.cal)
        except KeyError:
            self._stock_px[code] = pd.Series(np.nan, index=self.cal)
            self._stock_tr[code] = pd.Series(1.0, index=self.cal)
            return
        px = pd.to_numeric(sub["close"], errors="coerce").ffill()
        r = pd.to_numeric(sub["ret_wd"], errors="coerce").fillna(0.0)
        self._stock_px[code] = px
        self._stock_tr[code] = (1.0 + r).cumprod()

    def px(self, code: str, i: int) -> float:
        if code in ETF_POOL:
            self._load_etf(code)
            v = self._etf_px[code].iloc[i]
        else:
            self._load_stock(code)
            v = self._stock_px[code].iloc[i]
        return float(v) if pd.notna(v) else np.nan

    def tr(self, code: str, i: int) -> float:
        if code in ETF_POOL:
            self._load_etf(code)
            v = self._etf_tr[code].iloc[i]
        else:
            self._load_stock(code)
            v = self._stock_tr[code].iloc[i]
        return float(v) if pd.notna(v) else np.nan

    def hist_close(self, code: str, i: int, lookback: int = 500) -> pd.Series:
        """截至第i天、往前lookback天的收盘价序列"""
        if code in ETF_POOL:
            self._load_etf(code)
            lo = max(0, i - lookback)
            return self._etf_px[code].iloc[lo:i + 1].dropna()
        else:
            self._load_stock(code)
            lo = max(0, i - lookback)
            return self._stock_px[code].iloc[lo:i + 1].dropna()


# ---------------------------------------------------------------------------
# 回测主循环
# ---------------------------------------------------------------------------

def _weekly_matrix(pdata: dict[str, pd.Series]) -> pd.DataFrame:
    out = {}
    for c, s in pdata.items():
        w = s.resample("W-FRI").last().dropna()
        if len(w) >= 20:
            out[c] = w.tail(config.WEEKLY_LOOKBACK_WEEKS)
    return pd.DataFrame(out).dropna(how="all")


def run_backtest(capital: float, stock_mkt: StockMarket, stock_pools: dict,
                 etf_hist: dict, etf_div: dict, start: dt.date, end: dt.date,
                 label: str = "", verbose: bool = True,
                 warm_start: dt.date = None, alpha_k: float = None) -> dict:
    """
    混合模式回测主入口
    alpha_k: 因子影响配权的系数（None=用config.ALPHA_K, 0=不影响配权）
    """
    warm_start = warm_start or start
    cal_all_stock = stock_mkt.calendar(warm_start, end)
    # ETF日历 = 8只ETF交易日并集
    etf_cals = [df.index for df in etf_hist.values() if not df.empty]
    if not etf_cals:
        raise RuntimeError("ETF历史数据为空")
    cal_etf = etf_cals[0]
    for x in etf_cals[1:]:
        cal_etf = cal_etf.union(x)
    cal_etf = cal_etf[(cal_etf >= pd.Timestamp(warm_start)) & (cal_etf <= pd.Timestamp(end))]

    # 统一日历 = 股票交易日 ∩ ETF交易日
    cal_all = sorted(set(cal_all_stock) & set(cal_etf))
    cal = [d for d in cal_all if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    if len(cal) < 50:
        raise RuntimeError(f"{label} 回测区间内交易日不足")

    offset = cal_all.index(cal[0])
    pool_dates = sorted(stock_pools.keys())
    if not pool_dates:
        raise RuntimeError(f"{label} 无可用股票候选池")

    pb = PriceBook(etf_hist, stock_mkt, cal_all)

    # ETF分红映射：{code: {Timestamp: 每股现金}}
    div_map = {c: {pd.Timestamp(i): float(row["cash_per_share"])
                   for i, row in dv.iterrows()}
               for c, dv in etf_div.items() if len(dv)}

    holdings: dict[str, dict] = {}  # code -> {shares, k}
    cash = capital
    target: dict = {}
    last_reb: dt.date | None = None
    nav_log, rebal_log, trades = [], [], []
    div_total = 0.0

    # 如果alpha_k显式传入，覆盖config
    if alpha_k is not None:
        old_k = config.ALPHA_K
        config.ALPHA_K = alpha_k

    def value(i: int) -> float:
        v = cash
        for c, h in holdings.items():
            tr = pb.tr(c, i)
            if pd.notna(tr) and h["shares"]:
                v += h["shares"] * h["k"] * tr
        return v

    def pool_at(d: pd.Timestamp):
        avail = [x for x in pool_dates if x <= d]
        return stock_pools[avail[-1]] if avail else None

    for j, d in enumerate(cal):
        i = offset + j

        # ETF除息分红
        for code, m in div_map.items():
            sh = holdings.get(code, {}).get("shares", 0)
            cps = m.get(d)
            if sh and cps:
                amt = sh * cps
                cash += amt
                div_total += amt

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
            # 合并池：8只ETF + 股票Top15（INCLUDE_STOCKS=False 时退化为纯ETF基准）
            entry = pool_at(d)
            stock_codes = (entry["pool"] if entry else []) if INCLUDE_STOCKS else []
            codes = list(ETF_POOL) + stock_codes

            # 因子打分（仅股票部分）
            import factor_engine
            prof_map = entry.get("profiles") or {} if entry else {}
            profs = [prof_map[c] for c in stock_codes if c in prof_map]
            alpha = {}
            if len(profs) >= 2:
                try:
                    alpha = factor_engine.score_stocks_fundamental(profs)["alpha_score"].to_dict()
                except Exception as e:
                    if verbose:
                        logger.warning(f"  {d.date()} 打分失败({str(e)[:50]})，本次不叠加因子")

            # 提取历史价格
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
                if verbose:
                    logger.info(f"  {d.date()} 求解失败({str(e)[:50]})，跳过调仓")
                nav_log.append((d, val / capital))
                continue

            latest = {c: pb.px(c, i) for c in weights}
            latest = {c: p for c, p in latest.items() if pd.notna(p) and p > 0}
            if len(latest) < 2:
                nav_log.append((d, val / capital))
                continue

            lot = optimizer.lot_size_check_and_round(weights, latest, val)
            tgt_sh = lot["shares"]

            # 交易：先卖后买
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
                    is_etf = c in ETF_POOL
                    cost = _cost(amount, delta < 0, is_etf)
                    if delta > 0:
                        if amount + cost > cash:
                            lots = int((cash - _cost(p * 100, False, is_etf)) // (p * 100))
                            delta = min(delta // 100, max(lots, 0)) * 100
                            amount = delta * p
                            cost = _cost(amount, False, is_etf) if delta > 0 else 0.0
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

            _trade_pass(True)
            _trade_pass(False)

            rebal_log.append({
                "date": str(d.date()), "reason": reason,
                "n_etf": len([c for c in tgt_sh if c in ETF_POOL]),
                "n_stock": len([c for c in tgt_sh if c not in ETF_POOL]),
                "n_excluded": len(lot["excluded"]),
                "n_small": len(dropped), "fee": round(fee, 2), "method": method,
                "holdings": dict(tgt_sh),
            })
            target = lot["realized_weights"]
            last_reb = d.date()

        nav_log.append((d, value(i) / capital))

    # 恢复config
    if alpha_k is not None:
        config.ALPHA_K = old_k

    return _summarize(nav_log, rebal_log, trades, capital, label,
                      {c: h["shares"] for c, h in holdings.items()}, div_total)


def _summarize(nav_log, rebal_log, trades, capital, label, end_holdings, div_total) -> dict:
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
        "dividends": div_total,
        "nav": nav, "rebal_log": rebal_log, "trades": trades,
        "end_holdings": end_holdings,
    }


def _fmt_pct(x):
    return "N/A" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.2%}"


def _print_compare(results: list[dict]) -> None:
    print("\n" + "=" * 92)
    print("三档资金量 · 混合模式回测对比（ETF + 个股）")
    print("=" * 92)
    print(f"\n{'资金':>6} | {'区间':>23} | {'累计':>9} | {'年化':>8} | {'夏普':>6} | {'最大回撤':>9} | {'调仓':>4}")
    print("-" * 100)
    for r in results:
        print(f"{r['capital']/10000:>5.0f}万 | {r['start']} ~ {r['end']} | "
              f"{_fmt_pct(r['total_ret']):>9} | {_fmt_pct(r['annual_ret']):>8} | "
              f"{r['sharpe']:>6.2f} | {r['max_dd']:>9.2%} | {r['n_rebalance']:>4}")

    print(f"\n【持仓构成】")
    print(f"{'资金':>6} | {'平均ETF数':>10} | {'平均股票数':>10} | {'累计成本':>10} | {'累计分红':>10}")
    print("-" * 70)
    for r in results:
        rl = r["rebal_log"]
        n = max(len(rl), 1)
        print(f"{r['capital']/10000:>5.0f}万 | {sum(x['n_etf'] for x in rl)/n:>10.1f} | "
              f"{sum(x['n_stock'] for x in rl)/n:>10.1f} | ¥{r['fees']:>9,.0f} | ¥{r['dividends']:>9,.0f}")


def main():
    import sys, pickle, argparse
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    ap = argparse.ArgumentParser(description="混合模式回测（ETF + 个股，三档资金对比）")
    ap.add_argument("--warm-start", default="2015-01-01")
    ap.add_argument("--start", default="2017-01-01")
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--refresh", action="store_true", help="强制重算候选池")
    ap.add_argument("--caps", default="50000,200000,1000000")
    ap.add_argument("--alpha-k", type=float, default=None, help="因子影响配权系数（0=不影响，None=用config默认）")
    ap.add_argument("--etf-only", action="store_true", help="只跑8只ETF（纯ETF基准，不含个股）")
    args = ap.parse_args()

    global INCLUDE_STOCKS
    if args.etf_only:
        INCLUDE_STOCKS = False

    ws = dt.date.fromisoformat(args.warm_start)
    sd = dt.date.fromisoformat(args.start)
    ed = dt.date.fromisoformat(args.end)

    print("加载股票行情...", flush=True)
    stock_mkt = StockMarket(load_stock_market())
    print(f"  股票行情 {len(stock_mkt.df):,} 行", flush=True)

    cache = config.CACHE_DIR / f"stock_pools_{ws}_{ed}.pkl"
    if cache.exists() and not args.refresh:
        print(f"载入股票候选池缓存", flush=True)
        with open(cache, "rb") as f:
            stock_pools = pickle.load(f)
    else:
        print("预计算股票候选池...", flush=True)
        stock_pools = precompute_stock_pools(stock_mkt, ws, ed)
        with open(cache, "wb") as f:
            pickle.dump(stock_pools, f)
    print(f"  股票候选池 {len(stock_pools)} 期", flush=True)

    print("加载ETF行情...", flush=True)
    hist_start = ws - dt.timedelta(days=int(config.WEEKLY_LOOKBACK_WEEKS * 7) + 90)
    etf_hist = _fetch_etf_history(ETF_POOL, hist_start.isoformat(), ed.isoformat(), bt_start=sd)
    etf_div = _fetch_etf_dividends(ETF_POOL, hist_start.strftime("%Y%m%d"), ed.strftime("%Y%m%d"))
    print(f"  ETF {len(etf_hist)} 只", flush=True)

    caps = [float(x) for x in args.caps.split(",")]
    results = []
    for cap in caps:
        print(f"\n=== 回测 资金 {cap:,.0f} (alpha_k={args.alpha_k}) ===", flush=True)
        r = run_backtest(cap, stock_mkt, stock_pools, etf_hist, etf_div, sd, ed,
                        label=f"{cap/10000:.0f}万", warm_start=ws, alpha_k=args.alpha_k)
        print(f"  年化 {_fmt_pct(r['annual_ret'])}  夏普 {r['sharpe']:.2f}  最大回撤 {r['max_dd']:.2%}  "
              f"调仓 {r['n_rebalance']} 次", flush=True)
        results.append(r)

    _print_compare(results)


if __name__ == "__main__":
    main()
