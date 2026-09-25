# -*- coding: utf-8 -*-
"""
optimizer.py
职责：
1. 用周线数据 + EWMA 半衰期估计协方差矩阵（Ledoit-Wolf 收缩，数值更稳定）
2. 默认求解 Max Sharpe Ratio 组合，约束单资产权重 [0, 25%]
3. 集中度熔断：若触及25%上限的资产数 >= config.CONCENTRATION_BREAKER_COUNT，
   自动切换为 HRP（层次风险平价）重新求解，避免小样本下的角点解问题
4. 单手门槛校验：分配金额 < 1手所需金额则剔除该标的，权重等比例重新分配给剩余标的
"""
from __future__ import annotations
import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

from pypfopt import EfficientFrontier, risk_models, expected_returns
from pypfopt import HRPOpt
from pypfopt.exceptions import OptimizationError

import config

# 兼容垫片：scipy>=1.15 移除了 pypfopt 依赖的私有属性 scipy.cluster.hierarchy._LINKAGE_METHODS
# （HRPOpt.optimize() 仅用它做 linkage 方法名的成员校验，scipy 公开的 linkage() 自身会校验）。
# 属性缺失时按 scipy 历史取值重建，保证集中度熔断触发后的 HRP 分支可运行。
try:
    import scipy.cluster.hierarchy as _sch
    _sch._LINKAGE_METHODS  # noqa: B018
except AttributeError:
    import scipy.cluster.hierarchy as _sch
    _sch._LINKAGE_METHODS = {
        "single": 0, "complete": 1, "average": 2,
        "weighted": 3, "centroid": 4, "median": 5, "ward": 6,
    }

logger = logging.getLogger("optimizer")

# 诊断用：强制指定求解方法（None=正常流程 Max Sharpe→熔断则HRP）
# 设为 "hrp" 可强制纯HRP（跳过Max Sharpe），用于"纯数学基准"测试。
FORCE_METHOD = None


def build_weekly_returns(price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """从日线数据构建周线收盘价矩阵，用于协方差估计"""
    from data_loader import to_weekly
    closes = {}
    for code, df in price_data.items():
        weekly = to_weekly(df)
        if weekly.empty:
            continue
        closes[code] = weekly["close"].tail(config.WEEKLY_LOOKBACK_WEEKS)
    price_matrix = pd.DataFrame(closes).dropna(how="all")
    return price_matrix


def get_expected_returns_and_cov(price_matrix: pd.DataFrame) -> tuple:
    """
    独立暴露 mu(预期收益) 和协方差矩阵，供 backtest_compare.py 构建"预测基准"时使用。
    计算方式与 solve_max_sharpe() 内部完全一致，只是抽出来单独可调用，
    不改动 solve_max_sharpe() 本身的逻辑，避免影响已经跑通的求解流程。
    """
    mu = expected_returns.ema_historical_return(price_matrix, frequency=52)
    S = risk_models.CovarianceShrinkage(price_matrix, frequency=52).ledoit_wolf()
    return mu, S


def _adjust_mu(mu: pd.Series, alpha_scores: dict | None) -> pd.Series:
    """把因子打分（alpha_score）融入预期收益：mu' = mu + ALPHA_K * z(alpha_score)。

    alpha_scores: {code: alpha_score}；缺省或样本不足时原样返回。
    打分在因子层已做行业/市值中性化，这里只做截面标准化后线性叠加。
    """
    if not alpha_scores:
        return mu
    a = pd.Series(alpha_scores, dtype=float).reindex(mu.index)
    if a.notna().sum() < 2:
        return mu
    a = a.fillna(a.mean())
    sd = a.std()
    if not sd or pd.isna(sd):
        return mu
    return mu + config.ALPHA_K * ((a - a.mean()) / sd)


def solve_max_sharpe(price_matrix: pd.DataFrame, alpha_scores: dict = None) -> tuple[dict, str]:
    """
    Max Sharpe 求解。协方差用 Ledoit-Wolf 收缩估计（比样本协方差更稳健）。
    预期收益 = 历史EMA收益 + ALPHA_K * z(因子打分)（给了 alpha_scores 时）。
    无解（含"所有资产预期收益 ≤ 无风险利率"）时回退最小方差。
    返回 (weights_dict, method_used)
    """
    mu = expected_returns.ema_historical_return(price_matrix, frequency=52)
    mu = _adjust_mu(mu, alpha_scores)
    S = risk_models.CovarianceShrinkage(price_matrix, frequency=52).ledoit_wolf()
    bounds = (config.MIN_WEIGHT_PER_ASSET, config.MAX_WEIGHT_PER_ASSET)
    try:
        ef = EfficientFrontier(mu, S, weight_bounds=bounds)
        ef.max_sharpe(risk_free_rate=config.RISK_FREE_RATE)
        return ef.clean_weights(), "max_sharpe"
    except Exception as e:
        # 含 OptimizationError，以及"至少一个资产预期收益须超过无风险利率"的 ValueError
        logger.warning(f"Max Sharpe 求解失败: {str(e)[:80]}，回退至最小方差组合")
        ef2 = EfficientFrontier(mu, S, weight_bounds=bounds)
        ef2.min_volatility()
        return ef2.clean_weights(), "min_vol"


def solve_hrp(price_matrix: pd.DataFrame) -> tuple[dict, str]:
    """HRP 层次风险平价求解，不依赖预期收益估计，小样本下更稳定"""
    returns = price_matrix.pct_change().dropna(how="all")
    hrp = HRPOpt(returns)
    weights = hrp.optimize()
    # HRP 原生不支持硬性上限约束，这里手动做一次截断+归一化后处理
    weights = _apply_weight_cap(weights, config.MAX_WEIGHT_PER_ASSET)
    return weights, "hrp"


def _apply_weight_cap(weights: dict, cap: float) -> dict:
    """对不支持原生约束的方法（如HRP）做权重截断后归一化"""
    w = pd.Series(weights)
    for _ in range(20):  # 迭代截断直至收敛
        over = w[w > cap]
        if over.empty:
            break
        excess = (over - cap).sum()
        w[w > cap] = cap
        under = w[w < cap]
        if under.sum() == 0:
            break
        w[under.index] += excess * (under / under.sum())
    return (w / w.sum()).round(6).to_dict()


def _count_capped_assets(weights: dict, cap: float, tol: float = 1e-4) -> int:
    return sum(1 for v in weights.values() if v >= cap - tol)


def _tilt_by_alpha(weights: dict, alpha_scores: dict | None) -> dict:
    """按因子打分对权重做倾斜：w_i ∝ w_i * (1 + ALPHA_K * z(alpha_i))，再归一化。

    HRP / 最小方差 / 等权 这些方法**完全不使用预期收益**，光靠 _adjust_mu 影响不到它们。
    而实盘中集中度熔断触发频繁（HRP 是常态），故对非 Max Sharpe 的结果统一做这次倾斜，
    保证"打分高的权重更大"始终成立。Max Sharpe 路径已在 mu 里体现过，不重复倾斜。
    """
    if not alpha_scores or not weights:
        return weights
    idx = list(weights.keys())
    a = pd.Series(alpha_scores, dtype=float).reindex(idx)
    if a.notna().sum() < 2:
        return weights
    a = a.fillna(a.mean())
    sd = a.std()
    if not sd or pd.isna(sd):
        return weights
    z = (a - a.mean()) / sd
    f = (1.0 + config.ALPHA_K * z).clip(lower=0.0)
    w = pd.Series(weights, dtype=float) * f
    if w.sum() <= 0:
        return weights
    return (w / w.sum()).to_dict()


def solve_portfolio(price_matrix: pd.DataFrame, alpha_scores: dict = None) -> tuple[dict, str, list]:
    """
    主入口。求解链（逐级退回，**保证不跳过调仓**）：
        ① Max Sharpe（内部已含"无解→最小方差"回退）
        ② 上一步整体失败 → HRP
        ③ HRP 也失败 → 等权（每只 1/N）
    集中度熔断：Max Sharpe 触及25%上限的资产数达标 → 切换 HRP。
    alpha_scores: {code: alpha_score}，融入预期收益（见 _adjust_mu）。
    返回 (weights, method_used, dropped)
    dropped: [(code, reason), ...] 优化后权重被判定为极小值(<0.5%)而过滤掉的标的。
    """
    if price_matrix.shape[1] < 2:
        raise ValueError("可投资标的不足2只，无法构建组合")

    # 诊断用：强制指定方法（如 "hrp" = 纯HRP基准，跳过Max Sharpe）
    if FORCE_METHOD == "hrp":
        weights, method = solve_hrp(price_matrix)
        # HRP 不看预期收益 → 仍按打分倾斜（与正常流程里 HRP 路径一致）
        if alpha_scores:
            weights = _tilt_by_alpha(weights, alpha_scores)
            method = "hrp(forced)+alpha"
        else:
            method = "hrp(forced)"
        dropped = []
        kept = {k: v for k, v in weights.items() if v > 0.005}
        for k, v in weights.items():
            if v <= 0.005:
                dropped.append((k, f"优化后权重{v:.2%}，低于0.5%阈值，已过滤"))
        total = sum(kept.values())
        return ({k: round(v / total, 6) for k, v in kept.items()} if total > 0 else kept), method, dropped

    weights, method = None, None
    try:
        weights, method = solve_max_sharpe(price_matrix, alpha_scores)
    except Exception as e:
        logger.warning(f"Max Sharpe 分支失败: {str(e)[:80]}，退回 HRP")

    if weights is None:
        try:
            weights, method = solve_hrp(price_matrix)
        except Exception as e:
            logger.warning(f"HRP 也失败: {str(e)[:80]}，退回等权")

    if weights is None:
        codes = list(price_matrix.columns)
        weights = {c: 1.0 / len(codes) for c in codes}
        method = "equal"

    # 集中度熔断：Max Sharpe 解若过度集中，切 HRP（HRP 失败则保留原解）
    if method == "max_sharpe":
        capped_count = _count_capped_assets(weights, config.MAX_WEIGHT_PER_ASSET)
        if capped_count >= config.CONCENTRATION_BREAKER_COUNT:
            logger.info(f"Max Sharpe 触及25%上限标的数={capped_count}，触发集中度熔断，切换至 HRP")
            try:
                weights, method = solve_hrp(price_matrix)
            except Exception as e:
                logger.warning(f"熔断切换 HRP 失败({str(e)[:60]})，保留 Max Sharpe 解")

    # 非 Max Sharpe 路径（HRP/最小方差/等权）不使用预期收益 → 统一按打分倾斜，
    # 保证因子打分始终影响权重。Max Sharpe 已在 mu 里体现过，不重复倾斜。
    if method in ("hrp", "min_vol", "equal"):
        weights = _tilt_by_alpha(weights, alpha_scores)
        if alpha_scores:
            method = method + "+alpha"

    # 过滤掉极小权重（<0.5%视为噪音），并记录被过滤的标的（供界面展示去向）
    dropped = []
    kept = {}
    for k, v in weights.items():
        if v > 0.005:
            kept[k] = v
        else:
            dropped.append((k, f"优化后权重{v:.2%}，低于0.5%阈值，已过滤"))
    weights = kept
    total = sum(weights.values())
    weights = {k: round(v / total, 6) for k, v in weights.items()} if total > 0 else weights
    return weights, method, dropped


# ---------------------------------------------------------------------------
# 单手门槛校验与整手取整
# ---------------------------------------------------------------------------

def lot_size_check_and_round(weights: dict, latest_prices: dict[str, float],
                              total_capital: float = None) -> dict:
    """
    校验每个标的的分配金额是否 >= 1手(100股)所需金额，
    不足则剔除，权重等比例重新分摊给剩余标的；
    最终按整手（100股倍数）向下取整分配股数，零头留作现金。

    返回: {
        "final_weights": {code: weight},       # 理论目标权重（整手取整前）
        "realized_weights": {code: weight},    # 整手取整后的真实权重（用于调仓对比）
        "shares": {code: 整手股数},
        "cash_left": 剩余现金,
        "excluded": [(code, reason), ...]
    }
    """
    total_capital = total_capital or config.TOTAL_CAPITAL
    w = dict(weights)
    excluded = []

    changed = True
    while changed:
        changed = False
        for code in list(w.keys()):
            price = latest_prices.get(code)
            if price is None or price <= 0:
                excluded.append((code, "无有效价格"))
                del w[code]
                changed = True
                continue
            allocated = total_capital * w[code]
            one_lot_cost = price * config.LOT_SIZE
            if allocated < one_lot_cost:
                excluded.append((code, f"分配金额{allocated:.0f}元 < 1手所需{one_lot_cost:.0f}元"))
                del w[code]
                changed = True
        if changed and w:
            total_w = sum(w.values())
            w = {k: v / total_w for k, v in w.items()} if total_w > 0 else w
        elif not w:
            break

    shares = {}
    cash_used = 0.0
    for code, weight in w.items():
        price = latest_prices[code]
        allocated = total_capital * weight
        lots = math.floor(allocated / (price * config.LOT_SIZE))
        shares[code] = lots * config.LOT_SIZE
        cash_used += shares[code] * price

    cash_left = total_capital - cash_used
    realized_weights = compute_realized_weights(shares, latest_prices, total_capital)

    return {
        "final_weights": w,               # 理论目标权重（整手取整前）
        "realized_weights": realized_weights,  # 实际买到的真实权重（整手取整后，用于后续调仓对比）
        "shares": shares,
        "cash_left": round(cash_left, 2),
        "excluded": excluded,
    }


def compute_realized_weights(shares: dict, latest_prices: dict, total_capital: float = None) -> dict:
    """
    根据实际持有股数(整手)与最新价格，反算真实权重。
    与 lot_size_check_and_round 里的 final_weights（理论目标权重）不同——
    整手取整必然产生零头现金，真实权重 = 持仓市值 / 总资金，而不是理论分配比例。
    这是后续调仓触发判断应该使用的"当前权重"基准，而不是理论 final_weights。
    """
    total_capital = total_capital or config.TOTAL_CAPITAL
    if total_capital <= 0:
        return {}
    weights = {}
    for code, sh in shares.items():
        price = latest_prices.get(code)
        if price is None or sh == 0:
            continue
        weights[code] = round((sh * price) / total_capital, 6)
    return weights
