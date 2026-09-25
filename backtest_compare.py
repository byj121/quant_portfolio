# -*- coding: utf-8 -*-
"""
backtest_compare.py
职责：组合建仓后的"预测 vs 实际"净值曲线对比。

核心概念区分（容易算错的地方，务必按这个逻辑走）：
1. "预测线"是建仓那一刻定死的基准，用当时 optimizer 求解时算出的
   预期年化收益(mu)和协方差矩阵(S)，推算出一条理论净值路径。
   这条线建仓后不再变化，是静态的参照系。
2. "实际线"是用组合实际持有的标的+权重，结合建仓后每天的真实收盘价，
   逐日复利滚算出的真实净值路径。如果中途触发过调仓（权重变化），
   实际线要能正确反映"分段的权重"，不能全程用同一套权重去算。

不依赖 factor_engine.py / optimizer.py 内部逻辑，只需要它们已经算出的
mu / 协方差 / 权重这些结果作为输入，属于纯计算模块，可独立测试。
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class PredictionBaseline:
    """组合建仓那一刻要存下来的预测基准，后续在同一个组合的生命周期内不再变化"""
    inception_date: str              # 建仓日期 ISO格式
    weights: dict                    # 建仓时的权重 {code: weight}
    expected_annual_return: float    # optimizer 算出的组合预期年化收益(基于mu和weights)
    expected_annual_vol: float       # optimizer 算出的组合预期年化波动率(基于S和weights)


def build_prediction_baseline(weights: dict, mu: pd.Series, cov_matrix: pd.DataFrame,
                               inception_date: dt.date = None) -> PredictionBaseline:
    """
    组合建仓时调用一次，把 optimizer 已经算出的 mu(预期收益) 和协方差矩阵，
    结合最终权重，折算成"组合层面"的预期年化收益/波动率，存为基准。

    mu, cov_matrix 直接传 optimizer.get_expected_returns_and_cov(price_matrix) 的返回值即可。
    """
    inception_date = inception_date or dt.date.today()
    codes = [c for c in weights if c in mu.index]
    w = pd.Series({c: weights[c] for c in codes})
    w = w / w.sum()  # 防止传入的权重口径和mu/cov里的标的不完全对齐时，归一化一下

    expected_return = float((w * mu.reindex(w.index)).sum())
    sub_cov = cov_matrix.loc[w.index, w.index]
    expected_vol = float(np.sqrt(w.values @ sub_cov.values @ w.values.T))

    return PredictionBaseline(
        inception_date=inception_date.isoformat(),
        weights=dict(weights),
        expected_annual_return=expected_return,
        expected_annual_vol=expected_vol,
    )


def build_predicted_nav_curve(baseline: PredictionBaseline, trading_dates: pd.DatetimeIndex) -> pd.Series:
    """
    用建仓时的预期年化收益，推算一条"理论平滑增长"的预测净值曲线，
    作为对比基准（注意：这是一条确定性曲线，不含波动，纯粹是"如果按预期匀速增长"的参照线，
    波动率信息保留在 baseline.expected_annual_vol 里，用于另外标注置信区间，不体现在这条线本身）。
    净值起点固定为 1.0。
    """
    inception = pd.Timestamp(baseline.inception_date)
    dates = trading_dates[trading_dates >= inception]
    if len(dates) == 0:
        return pd.Series(dtype=float)

    days_elapsed = (dates - inception).days.values
    daily_rate = (1 + baseline.expected_annual_return) ** (1 / 365) - 1
    nav = (1 + daily_rate) ** days_elapsed
    return pd.Series(nav, index=dates, name="predicted_nav")


def build_actual_nav_curve(weight_history: list[tuple[str, dict]],
                            price_data: dict[str, pd.DataFrame]) -> pd.Series:
    """
    用组合实际持仓（可能分段变化）+ 真实行情，逐日滚算真实净值曲线。

    weight_history: [(effective_date_iso, weights_dict), ...]，按时间升序排列。
        第一段是建仓时的权重，后续每次触发调仓后append一段新权重+生效日期。
    price_data: {code: df(index=date, columns含close)}，需覆盖 weight_history 里最早日期到今天。

    返回：净值曲线 Series，起点为 1.0，index 为交易日期。
    """
    if not weight_history:
        return pd.Series(dtype=float)

    all_codes = set()
    for _, w in weight_history:
        all_codes.update(w.keys())

    # 构建对齐的每日收益率矩阵
    returns_matrix = pd.DataFrame({
        code: price_data[code]["close"].pct_change()
        for code in all_codes if code in price_data and not price_data[code].empty
    })
    if returns_matrix.empty:
        return pd.Series(dtype=float)

    inception = pd.Timestamp(weight_history[0][0])
    returns_matrix = returns_matrix[returns_matrix.index >= inception].fillna(0)

    # 给每一天分配"当天生效的权重段"
    change_points = sorted([pd.Timestamp(d) for d, _ in weight_history])
    weights_by_date = [w for _, w in weight_history]

    def weights_for_date(date) -> dict:
        idx = 0
        for i, cp in enumerate(change_points):
            if date >= cp:
                idx = i
        return weights_by_date[idx]

    nav = [1.0]
    nav_dates = [inception]
    for date, row in returns_matrix.iterrows():
        if date <= inception:
            continue
        w = weights_for_date(date)
        w_series = pd.Series(w).reindex(row.index).fillna(0)
        daily_port_return = float((row * w_series).sum())
        nav.append(nav[-1] * (1 + daily_port_return))
        nav_dates.append(date)

    return pd.Series(nav, index=pd.DatetimeIndex(nav_dates), name="actual_nav")


def compare_predicted_vs_actual(baseline: PredictionBaseline,
                                 weight_history: list[tuple[str, dict]],
                                 price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    主入口：返回一个含 predicted_nav / actual_nav 两列的 DataFrame，供 app.py 画图对比。
    """
    actual = build_actual_nav_curve(weight_history, price_data)
    if actual.empty:
        return pd.DataFrame(columns=["predicted_nav", "actual_nav"])

    predicted = build_predicted_nav_curve(baseline, actual.index)
    combined = pd.concat([predicted, actual], axis=1).sort_index()
    combined["predicted_nav"] = combined["predicted_nav"].ffill().fillna(1.0)
    return combined


def summarize_deviation(comparison_df: pd.DataFrame) -> dict:
    """给一份简明的偏差摘要：实际比预测好还是差，差多少"""
    if comparison_df.empty:
        return {"status": "无数据"}
    latest = comparison_df.iloc[-1]
    diff_pct = (latest["actual_nav"] - latest["predicted_nav"]) / latest["predicted_nav"]
    return {
        "predicted_nav": round(float(latest["predicted_nav"]), 4),
        "actual_nav": round(float(latest["actual_nav"]), 4),
        "diff_pct": round(float(diff_pct), 4),
        "status": "实际跑赢预测" if diff_pct > 0 else ("实际跑输预测" if diff_pct < 0 else "与预测持平"),
    }
