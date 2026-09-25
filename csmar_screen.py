# -*- coding: utf-8 -*-
"""
csmar_screen.py
基于 CSMAR 数据的【全市场防雷筛选】，供"扩大股票池"和"股票段回测"使用。

与 data_loader.risk_screen_stock 的区别：
  · risk_screen_stock —— 走 tushare 实时接口，逐只查，用于【实盘建仓】（几十只规模）
  · 本模块           —— 读 CSMAR 离线数据，一次筛全市场，用于【扩池 / 回测】

九条防雷红线（任一命中即剔除）：
  1. ST / 退市 / PT
  2. 停牌（最近交易日无行情）
  3. 次新股（上市不满 config.EXCLUDE_NEW_STOCK_DAYS 天）
  4. PE 异常（PE-TTM ≤ 0 或 > config.PE_TTM_MAX）
  5. 近3年平均 ROE < config.ROE_AVG3Y_MIN
  6. 最新毛利率 < config.GROSS_MARGIN_MIN
  7. 现金流背离（近3年 每股经营现金流/每股收益 均值 < config.OCF_NI_MIN_RATIO）
  8. 资产负债率 > config.MAX_DEBT_RATIO（金融行业豁免）
  9. 流动性不足（近20日日均成交额 < config.MIN_AVG_TURNOVER_20D）

所有财务数据都按【point-in-time】取：优先用 CSMAR 的"实际披露日"，缺失时按
config.REPORT_LAG_RULES（年报次年5/1、中报8/31、一季报4/20、三季报10/20）保守滞后。
"""
from __future__ import annotations

import datetime as dt
import logging

import numpy as np
import pandas as pd

import config
import csmar_loader as cl

logger = logging.getLogger("csmar_screen")


# ---------------------------------------------------------------------------
# PIT：取截至 as_of【已披露】的最近一期财务数据
# ---------------------------------------------------------------------------

def _lag_usable(accper: pd.Timestamp) -> pd.Timestamp:
    """没有实际披露日时，按监管最晚披露日保守估算可用日。"""
    if pd.isna(accper):
        return pd.NaT
    m, y = accper.month, accper.year
    if m == 12:
        return pd.Timestamp(y + 1, 5, 1)
    if m == 6:
        return pd.Timestamp(y, 8, 31)
    if m == 3:
        return pd.Timestamp(y, 4, 20)
    if m == 9:
        return pd.Timestamp(y, 10, 20)
    return accper + pd.Timedelta(days=120)


def _attach_usable(fin: pd.DataFrame, disc: pd.DataFrame = None) -> pd.DataFrame:
    """给财务表加 usable 列（最早可用日）。优先实际披露日，缺失用滞后规则。"""
    f = fin.copy()
    if disc is not None and not disc.empty:
        f = f.merge(disc[["code", "accper", "actudt"]], on=["code", "accper"], how="left")
        est = f["accper"].apply(_lag_usable)
        f["usable"] = f["actudt"].where(f["actudt"].notna(), est)
        f = f.drop(columns=["actudt"])
    else:
        f["usable"] = f["accper"].apply(_lag_usable)
    return f


def _pit_latest(fin: pd.DataFrame, as_of: dt.date, cols: list[str],
                disc: pd.DataFrame = None) -> pd.DataFrame:
    """取每只股票在 as_of 时点【已披露】的最近一期数据，只保留 cols。index=code。"""
    f = _attach_usable(fin, disc)
    f = f[f["usable"] <= pd.Timestamp(as_of)]
    if f.empty:
        return pd.DataFrame(columns=cols)
    f = f.sort_values(["code", "accper"]).groupby("code", as_index=False).tail(1)
    return f.set_index("code")[cols]


def _pit_avg3(fin: pd.DataFrame, as_of: dt.date, cols: list[str],
              disc: pd.DataFrame = None, agg: str = "mean",
              ratio_of: tuple[str, str] = None) -> pd.Series:
    """取每只股票截至 as_of 已披露的【最近3期年报】数据的均值（用于 ROE / 现金流比）。
    ratio_of=(分子列, 分母列) 时，先逐行算比值再求均值。
    """
    f = _attach_usable(fin, disc)
    f = f[f["usable"] <= pd.Timestamp(as_of)]
    f = f[f["accper"].dt.month == 12]                       # 只看年报
    if f.empty:
        return pd.Series(dtype=float)
    f = f.sort_values(["code", "accper"]).groupby("code").tail(3)
    if ratio_of:
        num, den = ratio_of
        v = pd.to_numeric(f[num], errors="coerce") / pd.to_numeric(f[den], errors="coerce").replace(0, np.nan)
        f = f.assign(_v=v)
    else:
        f = f.assign(_v=pd.to_numeric(f[cols[0]], errors="coerce"))
    g = f.groupby("code")["_v"].agg(agg)
    return g


# ---------------------------------------------------------------------------
# 主入口：全市场9条筛选
# ---------------------------------------------------------------------------

def screen_market(as_of: dt.date = None, snapshot: pd.DataFrame = None,
                  verbose: bool = True) -> pd.DataFrame:
    """对全市场跑9条防雷规则。

    as_of:    以哪天为基准做 PIT 取数（默认今天）
    snapshot: cl.get_latest_snapshot() 的结果（不给则自动加载，较慢）
    返回 DataFrame，index=code，列含各规则原始值 + passed(bool) + reason(str,首个命中的原因)
    """
    as_of = as_of or dt.date.today()
    if verbose:
        logger.info(f"全市场防雷筛选，基准日 {as_of}")

    # ---- 基础：退市名单（含上市日期）作为全市场骨架 ----
    dl = cl.load_delisting()
    df = dl.set_index("code")[["name", "list_date", "delist_date"]].copy()
    df["delisted"] = df["delist_date"].notna()
    df["days_listed"] = (pd.Timestamp(as_of) - df["list_date"]).dt.days

    # ---- 行情快照：最新价 / 20日均成交额 / 交易状态 ----
    if snapshot is None:
        snapshot = cl.get_latest_snapshot()
    for c in ["latest_close", "avg_amount_20d", "trade_status", "total_mv", "last_date"]:
        if c in snapshot.columns:
            df[c] = snapshot[c]

    # ---- PIT 财务：负债率 / ROE / 毛利率 / PE / 现金流比 ----
    disc = cl.load_disclosure_dates()
    dr = cl.load_debt_ratio()
    pf = cl.load_profitability()
    ps = cl.load_per_share()
    pe = cl.load_valuation_pe()

    df = df.join(_pit_latest(dr, as_of, ["debt_ratio", "ind_code", "ind_name"], disc))
    df = df.join(_pit_latest(pf, as_of, ["roe_b", "gross_margin"], disc))
    df = df.join(_pit_latest(pe, as_of, ["pe_ttm"], disc))
    df["roe_avg3"] = _pit_avg3(pf, as_of, ["roe_b"], disc)
    df["ocf_ni_ratio"] = _pit_avg3(ps, as_of, [], disc, ratio_of=("ocfps", "eps"))

    # ---- 逐条判定 ----
    reasons = pd.Series("", index=df.index, dtype=object)

    def _mark(mask: pd.Series, msg: str):
        hit = mask.fillna(False) & (reasons == "")
        reasons.loc[hit] = msg

    # 1) ST / 退市 / PT  —— 用特殊处理表做 PIT 判断 + 名称兜底
    st_map = cl.get_st_map(list(df.index), as_of)
    st_series = pd.Series(st_map, dtype=object).reindex(df.index)
    name_has = df["name"].astype(str).str.contains(r"ST|退|PT", na=False)
    _mark(st_series.notna() | name_has | df["delisted"], "ST/退市/PT")

    # 2) 停牌：最近交易日没有行情
    _mark(df["latest_close"].isna(), "无行情(疑似停牌/未上市)")

    # 3) 次新股
    _mark(df["days_listed"] < config.EXCLUDE_NEW_STOCK_DAYS, "次新股(上市不满1年)")

    # 4) PE 异常（缺失不剔除，与原 risk_screen_stock 语义一致）
    _mark(df["pe_ttm"].notna() & ((df["pe_ttm"] <= config.PE_TTM_MIN)
                                  | (df["pe_ttm"] > config.PE_TTM_MAX)), "PE异常")

    # 5) ROE
    _mark(df["roe_avg3"].notna() & (df["roe_avg3"] < config.ROE_AVG3Y_MIN), "ROE为负")

    # 6) 毛利率
    _mark(df["gross_margin"].notna()
          & (df["gross_margin"] < config.GROSS_MARGIN_MIN), "毛利率过低")

    # 7) 现金流背离（缺失不剔除，避免数据不全误杀）
    _mark(df["ocf_ni_ratio"].notna()
          & (df["ocf_ni_ratio"] < config.OCF_NI_MIN_RATIO), "现金流背离")

    # 8) 资产负债率（金融豁免）
    fin_kw = ("银行", "保险", "证券", "金融")
    is_fin = df["ind_name"].fillna("").astype(str).apply(
        lambda s: any(k in s for k in fin_kw))
    _mark((~is_fin) & df["debt_ratio"].notna()
          & (df["debt_ratio"] > config.MAX_DEBT_RATIO), "资产负债率过高")

    # 9) 流动性
    _mark(df["avg_amount_20d"].notna()
          & (df["avg_amount_20d"] < config.MIN_AVG_TURNOVER_20D), "流动性不足")

    df["reason"] = reasons
    df["passed"] = df["reason"] == ""

    if verbose:
        n = len(df)
        logger.info(f"全市场 {n:,} 只 → 通过 {df['passed'].sum():,} 只，剔除 {(~df['passed']).sum():,} 只")
    return df


# ---------------------------------------------------------------------------
# 从 CSMAR 构建【因子打分所需的 profiles】（结构对齐 fundamentals.fetch_one_profile）
# ---------------------------------------------------------------------------

def build_profiles(codes: list[str], as_of: dt.date = None,
                   price_map: dict[str, float] = None,
                   snapshot: pd.DataFrame = None,
                   pe_lookback: int = 16) -> list[dict]:
    """用 CSMAR 数据构建打分 profiles（供 factor_engine.build_stock_factor_table）。

    需要的字段：roe_avg3 / gross_margin / debt_ratio / pe_ttm / pe_pctile /
                div_spread / total_mv / industry(申万一级)
    price_map: {code: 最新价}，用于算股息率；不给则从 snapshot 取 latest_close。
    """
    import fundamentals  # 申万 PIT 在 fundamentals 里（走 tushare）

    as_of = as_of or dt.date.today()
    codes = [str(c).zfill(6) for c in codes]
    if snapshot is not None:
        sub = snapshot.reindex(codes)
        if price_map is None:
            price_map = sub["latest_close"].dropna().to_dict()
        mv_map = sub["total_mv"].dropna().to_dict()        # 千元
    else:
        mv_map = {}

    disc = cl.load_disclosure_dates()
    roe3 = _pit_avg3(cl.load_profitability(), as_of, ["roe_b"], disc)
    gm = _pit_latest(cl.load_profitability(), as_of, ["gross_margin"], disc)["gross_margin"]
    debt = _pit_latest(cl.load_debt_ratio(), as_of, ["debt_ratio"], disc)["debt_ratio"]

    # PE-TTM 当前值 + 历史分位（用 CSMAR 的季度 PE 序列，取最近 pe_lookback 期）
    pe_all = _attach_usable(cl.load_valuation_pe(), disc)
    pe_all = pe_all[pe_all["usable"] <= pd.Timestamp(as_of)]
    pe_all = pe_all[pe_all["pe_ttm"] > 0].sort_values(["code", "accper"])
    pe_hist = pe_all.groupby("code").tail(pe_lookback)
    pe_now = pe_hist.groupby("code")["pe_ttm"].last()
    pe_pct = pe_hist.groupby("code")["pe_ttm"].apply(
        lambda s: float((s <= s.iloc[-1]).mean()) if len(s) > 2 else np.nan)

    # 申万一级行业（point-in-time）
    sw = fundamentals.get_sw_l1_pit(codes, as_of)
    # 每股股利（一次性批量取，避免逐个读表）
    dps_map = cl.get_dps_map(codes, as_of)

    out = []
    for c in codes:
        px = price_map.get(c)
        pe_v = pe_now.get(c)
        _dps = dps_map.get(c)
        dv = (_dps / float(px)) if (_dps is not None and px) else None
        out.append({
            "code": c,
            "name": cl.name_of(c),
            "industry": sw.get(c, ""),
            "roe_avg3": _f(roe3.get(c)),
            "gross_margin": _f(gm.get(c)),
            "debt_ratio": _f(debt.get(c)),
            "pe_ttm": _f(pe_v),
            "pe_pctile": _f(pe_pct.get(c)),
            "dv_ttm": _f(dv),
            "div_spread": (_f(dv) - config.YIELD_10Y_CN) if dv is not None else None,
            "div_source": "csmar" if dv is not None else None,
            "total_mv": (float(mv_map.get(c)) * 1e4) if c in mv_map else None,   # 千元→元
            "ok": True,
        })
    return out


def _f(v):
    """NaN/None 统一成 None（factor_engine 会按截面均值填充）。"""
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 一站式：全市场筛选 → 打分候选池（供"触发即重筛"用）
# ---------------------------------------------------------------------------

def build_candidate_pool(as_of: dt.date = None, snapshot: pd.DataFrame = None,
                         top_n: int = None, verbose: bool = True):
    """全市场9条防雷筛选 → 构建 profiles → 因子打分取 Top N。返回 (candidate_df, profiles)"""
    import factor_engine

    as_of = as_of or dt.date.today()
    if snapshot is None:
        snapshot = cl.get_latest_snapshot()
    scr = screen_market(as_of=as_of, snapshot=snapshot, verbose=verbose)
    passed = scr[scr["passed"] & scr["latest_close"].notna()]
    codes = list(passed.index)
    if verbose:
        logger.info(f"通过防雷: {len(codes):,} 只 → 开始构建 profiles 与打分")
    profiles = build_profiles(codes, as_of=as_of, snapshot=passed)
    pool = factor_engine.score_stocks_fundamental(profiles)
    n = top_n or config.STOCK_TOP_N
    return pool.head(n), profiles


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO)
    r = screen_market()
    print(r["passed"].value_counts())
    print(r[~r["passed"]]["reason"].value_counts())
