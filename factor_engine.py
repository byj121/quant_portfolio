# -*- coding: utf-8 -*-
"""
factor_engine.py
职责：
1. 个股：纯基本面三因子 —— 质量(近3年平均ROE + 最新销售毛利率)、
   估值(股息率-10年国债利差 vs PE-TTM历史分位)、安全(资产负债率，越低越好)
2. ETF：计算趋势强度(RSRS)、量能活跃度、相关性去重三因子
3. MAD去极值 + Z-Score标准化
4. OLS 截面回归剥离市值/行业偏误，输出纯净 Alpha 残差
5. 输出 Top N 候选池

注：compute_cross_sectional_sharpe / compute_low_vol / compute_quality_score
   是旧「动量 / 低波动 / ROIC 质量」因子体系的遗留函数，**个股打分已不再调用**
   （当前权重见 config.FACTOR_WEIGHTS_STOCK）。保留在此，供将来若改回
   动量 / 低波动因子体系时复用。
"""
from __future__ import annotations
import logging
from typing import Optional

import numpy as np
import pandas as pd
import statsmodels.api as sm

import config

logger = logging.getLogger("factor_engine")


# ---------------------------------------------------------------------------
# 通用：去极值 & 标准化
# ---------------------------------------------------------------------------

def mad_winsorize(series: pd.Series, n: float = None) -> pd.Series:
    """MAD 去极值：超出 median ± n*MAD 的值截断到边界"""
    n = n or config.MAD_THRESHOLD
    median = series.median()
    mad = (series - median).abs().median()
    if mad == 0 or pd.isna(mad):
        return series
    lower, upper = median - n * mad, median + n * mad
    return series.clip(lower=lower, upper=upper)


def zscore(series: pd.Series) -> pd.Series:
    std = series.std()
    if std == 0 or pd.isna(std):
        return series * 0
    return (series - series.mean()) / std


def clean_factor(series: pd.Series) -> pd.Series:
    return zscore(mad_winsorize(series.astype(float)))


# ---------------------------------------------------------------------------
# RSRS：阻力支撑相对强度（趋势因子，个股/ETF通用）
# ---------------------------------------------------------------------------

def compute_rsrs(price_df: pd.DataFrame, window: int = 18) -> float:
    """
    RSRS = 最高价对最低价滚动窗口内OLS回归的斜率标准化值。
    price_df 需含 'high', 'low' 列，按日期升序。
    返回最新一期的标准化 RSRS 分数（越大代表上升趋势越强）。
    """
    if len(price_df) < window + 1:
        return np.nan
    df = price_df.tail(window * 3)  # 取足够长的窗口计算标准化基准
    slopes = []
    for i in range(len(df) - window + 1):
        seg = df.iloc[i:i + window]
        X = sm.add_constant(seg["low"].values)
        y = seg["high"].values
        try:
            model = sm.OLS(y, X).fit()
            slopes.append(model.params[1])
        except Exception:
            slopes.append(np.nan)
    slopes = pd.Series(slopes).dropna()
    if len(slopes) < 5:
        return np.nan
    latest_slope = slopes.iloc[-1]
    return (latest_slope - slopes.mean()) / slopes.std() if slopes.std() > 0 else 0.0


def compute_cross_sectional_sharpe(returns: pd.Series, window: int = 60) -> float:
    """截面动量因子：过去 window 日年化 Sharpe 比率"""
    r = returns.tail(window)
    if r.std() == 0 or r.empty:
        return np.nan
    return float(r.mean() / r.std() * np.sqrt(252))


def compute_low_vol(returns: pd.Series, window: int = 60) -> float:
    """低波动因子：过去 window 日年化波动率的负值（越大越安全）"""
    r = returns.tail(window)
    if r.empty:
        return np.nan
    return float(-r.std() * np.sqrt(252))


# ---------------------------------------------------------------------------
# 个股因子：质量 ROIC / FCF（需配合 data_loader 的 point-in-time 财务数据）
# ---------------------------------------------------------------------------

def compute_quality_score(roic: Optional[float], fcf_yield: Optional[float]) -> float:
    """质量因子简单合成：ROIC 与 FCF 收益率各占一半，NaN 用 0 填充后再在截面标准化"""
    roic = roic if roic is not None else np.nan
    fcf_yield = fcf_yield if fcf_yield is not None else np.nan
    return np.nanmean([roic, fcf_yield])


# ---------------------------------------------------------------------------
# 个股综合因子表构建
# ---------------------------------------------------------------------------

def build_stock_factor_table(profiles: list[dict]) -> pd.DataFrame:
    """
    用个股基本面快照（fundamentals.build_stock_profiles() 通过防雷网后的结果）构建原始因子表。
    因子方向统一为"越大越好"：
      quality   = 近3年平均ROE 与 最新销售毛利率（各自截面标准化后等权）
      valuation = (股息率-10年期国债利差) - PE-TTM历史分位（分位越低=越便宜，取负）
      safety    = -资产负债率（负债越低越好）
    返回: 因子原始值表，index=code，含 quality/valuation/safety/market_cap/industry
    """
    raw = []
    for p in profiles:
        raw.append({
            "code": p.get("code"),
            "roe_avg3": p.get("roe_avg3"),
            "gross_margin": p.get("gross_margin"),
            "pe_pctile": p.get("pe_pctile"),
            "div_spread": p.get("div_spread"),
            "debt_ratio": p.get("debt_ratio"),
            "market_cap": p.get("total_mv"),
            "industry": p.get("industry") or "未知",
        })
    df = pd.DataFrame(raw).dropna(subset=["code"]).set_index("code")
    if df.empty:
        return df
    for col in ["roe_avg3", "gross_margin", "pe_pctile", "div_spread", "debt_ratio"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(df[col].mean())

    z_roe = clean_factor(df["roe_avg3"])
    z_gm = clean_factor(df["gross_margin"])
    z_pct = clean_factor(df["pe_pctile"])
    z_spread = clean_factor(df["div_spread"])
    z_debt = clean_factor(df["debt_ratio"])
    df["quality"] = (z_roe + z_gm) / 2.0
    df["valuation"] = z_spread - z_pct
    df["safety"] = -z_debt
    return df[["quality", "valuation", "safety", "market_cap", "industry"]]


def neutralize_and_score(factor_df: pd.DataFrame, weights: dict = None) -> pd.DataFrame:
    """
    OLS 截面回归剥离市值 + 申万行业偏误，输出纯净 Alpha 残差并加权合成综合得分。
    factor_df 需含权重键对应的因子列 + market_cap + industry；因子列名即 weights 的键。
    保留 OLS 行业/市值中性化机制：估值因子的行业差异（银行低PE vs 消费高PE）在此剥离。
    """
    weights = weights or config.FACTOR_WEIGHTS_STOCK
    df = factor_df.copy()
    factors = list(weights.keys())

    for col in factors:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = clean_factor(df[col].fillna(df[col].mean()))

    # 市值对数化 + 申万行业哑变量
    if "market_cap" in df.columns and df["market_cap"].notna().any():
        df["log_mcap"] = np.log(pd.to_numeric(df["market_cap"], errors="coerce")
                                  .fillna(df["market_cap"].median()).clip(lower=1))
    else:
        df["log_mcap"] = 0.0

    industry_dummies = pd.get_dummies(df.get("industry", pd.Series("未知", index=df.index)),
                                      drop_first=True)

    residual_scores = {}
    for factor_name in factors:
        y = df[factor_name].values
        X = pd.concat([df[["log_mcap"]], industry_dummies], axis=1)
        X = sm.add_constant(X.astype(float))
        try:
            model = sm.OLS(y, X, missing="drop").fit()
            residuals = pd.Series(model.resid, index=X.index)
            residual_scores[factor_name] = zscore(residuals)
        except Exception as e:
            logger.warning(f"OLS中性化失败({factor_name}): {e}，回退为原始Z-Score")
            residual_scores[factor_name] = df[factor_name]

    resid_df = pd.DataFrame(residual_scores).fillna(0.0)
    # 注：缺失因子按中性0处理，避免 NaN 污染 alpha_score
    df["alpha_score"] = sum(resid_df[f] * weights[f] for f in factors)
    return df.sort_values("alpha_score", ascending=False)


def score_stocks_fundamental(profiles: list[dict]) -> pd.DataFrame:
    """个股纯基本面打分入口：原始三因子 → 行业/市值中性化 → alpha_score"""
    return neutralize_and_score(build_stock_factor_table(profiles))


# ---------------------------------------------------------------------------
# ETF 因子表构建（趋势 + 量能 + 去重）
# ---------------------------------------------------------------------------

def build_etf_factor_table(price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for code, df in price_data.items():
        if df.empty or len(df) < 60:
            continue
        returns = df["close"].pct_change().dropna()
        trend = compute_rsrs(df)
        volume_activity = float(df["amount"].tail(20).mean())
        rows.append({"code": code, "trend": trend, "volume_activity": volume_activity,
                      "_returns": returns})
    df = pd.DataFrame(rows).set_index("code")
    if df.empty:
        return df

    # 相关性去重：与已选(按trend排序后逐步累加)标的相关性越高，惩罚越大
    returns_matrix = pd.DataFrame({c: r for c, r in df["_returns"].items()}).dropna(how="all")
    corr = returns_matrix.corr()
    dedup_penalty = corr.mean(axis=1)  # 与其他所有标的平均相关性，越高说明越冗余

    df["trend_z"] = clean_factor(df["trend"].fillna(df["trend"].mean()))
    df["volume_z"] = clean_factor(np.log(df["volume_activity"].clip(lower=1)))
    df["dedup_z"] = clean_factor(-dedup_penalty.reindex(df.index).fillna(0))  # 负相关性=正因子

    w = config.FACTOR_WEIGHTS_ETF
    df["alpha_score"] = (df["trend_z"] * w["trend"] + df["volume_z"] * w["volume"]
                          + df["dedup_z"] * w["dedup"])
    return df.drop(columns=["_returns"]).sort_values("alpha_score", ascending=False)


# ---------------------------------------------------------------------------
# 入口：生成 Top N 候选池
# ---------------------------------------------------------------------------

def generate_candidate_pool(price_data_etf: dict = None,
                            stock_profiles: list = None) -> pd.DataFrame:
    """
    个股与 ETF 各自单独打分排序后拼接（不放在一起比大小，避免某一类系统性挤出另一类）：
      - ETF：build_etf_factor_table 打分排序，但候选池保留全部池内 ETF（不做数量截断，
            由后续单手门槛/优化决定最终去向）
      - 个股：score_stocks_fundamental 纯基本面打分，单独取 Top config.STOCK_TOP_N（10-15只）
    stock_profiles 应是已通过防雷网的个股基本面快照。
    返回 DataFrame(alpha_score, asset_type)
    """
    frames = []

    if price_data_etf:
        etf_df = build_etf_factor_table(price_data_etf)
        etf_df["asset_type"] = "ETF"
        frames.append(etf_df[["alpha_score", "asset_type"]])

    if stock_profiles:
        stock_df = score_stocks_fundamental(list(stock_profiles))
        stock_df["asset_type"] = "STOCK"
        frames.append(stock_df[["alpha_score", "asset_type"]].head(config.STOCK_TOP_N))

    if not frames:
        return pd.DataFrame(columns=["alpha_score", "asset_type"])

    return pd.concat(frames).sort_values("alpha_score", ascending=False)
