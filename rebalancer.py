# -*- coding: utf-8 -*-
"""
rebalancer.py
职责：
1. 双重触发：偏离度>5% 且 距上次调仓>30天 -> 常规触发；偏离度>10% -> 忽略天数限制紧急触发
2. 微量拦截：调整金额 < 1000元的买卖自动忽略
3. 增量资金优先：新资金注入时优先买入权重缺口最大的标的
4. 状态持久化：记录上次调仓日期与当前持仓，存于 config.STATE_FILE
"""
from __future__ import annotations
import datetime as dt
import json
import logging
from dataclasses import dataclass, field

import config
from pathlib import Path

logger = logging.getLogger("rebalancer")


@dataclass
class TradeInstruction:
    code: str
    action: str          # "买入" / "卖出"
    amount: float         # 金额（元）
    shares: int           # 股数（100的整数倍）
    reason: str = ""


def load_state(state_file: Path = None) -> dict:
    """读取持仓状态。state_file 缺省时为旧的全局 config.STATE_FILE（向后兼容）。"""
    path = state_file or config.STATE_FILE
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_rebalance_date": None, "holdings": {}}


def save_state(state: dict, state_file: Path = None) -> None:
    path = state_file or config.STATE_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, default=str)


def get_current_weights_from_state(current_prices: dict, total_capital: float = None,
                                   state_file: Path = None) -> tuple[dict, dict]:
    """
    从上次保存的持仓状态（state["holdings"]）+ 最新价格，反算"当前真实权重"。
    这个函数就是用来替代 app.py 里之前那个占位符 current_weights_sim = {} 的——
    没有它，调仓检测算的永远是"从0建仓"的偏离度，而不是真实持仓的偏离度。

    返回: (current_weights, current_shares)
    """
    total_capital = total_capital or config.TOTAL_CAPITAL
    state = load_state(state_file)
    holdings = state.get("holdings", {})  # {code: shares}

    current_weights = {}
    if total_capital > 0:
        for code, shares in holdings.items():
            price = current_prices.get(code)
            if price is None or shares == 0:
                continue
            current_weights[code] = round((shares * price) / total_capital, 6)

    return current_weights, holdings


def compute_drift(current_weights: dict, target_weights: dict) -> dict:
    """计算每个标的当前权重与目标权重的绝对偏离度"""
    all_codes = set(current_weights) | set(target_weights)
    return {
        code: abs(target_weights.get(code, 0.0) - current_weights.get(code, 0.0))
        for code in all_codes
    }


def should_trigger_rebalance(drift: dict, last_rebalance_date: str | None,
                              today: dt.date = None) -> tuple[bool, str]:
    """
    判断是否触发再平衡：
    - max_drift > 10% -> 紧急触发，忽略天数限制
    - max_drift > 5% 且 距上次调仓 > 30天 -> 常规触发
    """
    today = today or dt.date.today()
    max_drift = max(drift.values()) if drift else 0.0

    if max_drift > config.REBALANCE_URGENT_THRESHOLD:
        return True, f"紧急触发：最大偏离度{max_drift:.1%} > {config.REBALANCE_URGENT_THRESHOLD:.0%}"

    if max_drift > config.REBALANCE_DRIFT_THRESHOLD:
        if last_rebalance_date is None:
            return True, f"常规触发：首次调仓，偏离度{max_drift:.1%}"
        last_date = dt.date.fromisoformat(last_rebalance_date)
        days_elapsed = (today - last_date).days
        if days_elapsed > config.REBALANCE_MIN_INTERVAL_DAYS:
            return True, f"常规触发：偏离度{max_drift:.1%}，距上次调仓{days_elapsed}天"
        return False, f"偏离度{max_drift:.1%}已超阈值，但距上次调仓仅{days_elapsed}天，未满{config.REBALANCE_MIN_INTERVAL_DAYS}天"

    return False, f"最大偏离度{max_drift:.1%}，未超5%阈值"


def generate_trade_instructions(current_shares: dict, current_prices: dict,
                                 target_shares: dict, total_capital: float) -> list[TradeInstruction]:
    """
    对比当前持仓股数与目标股数，生成交易指令列表。
    微量拦截：调整金额 < config.MIN_TRADE_AMOUNT 的指令自动忽略。
    """
    instructions = []
    all_codes = set(current_shares) | set(target_shares)

    for code in all_codes:
        price = current_prices.get(code)
        if price is None or price <= 0:
            continue
        cur = current_shares.get(code, 0)
        tgt = target_shares.get(code, 0)
        delta_shares = tgt - cur
        if delta_shares == 0:
            continue
        amount = abs(delta_shares) * price
        if amount < config.MIN_TRADE_AMOUNT:
            logger.info(f"[{code}] 调整金额{amount:.0f}元 < {config.MIN_TRADE_AMOUNT}元，微量拦截，忽略")
            continue
        action = "买入" if delta_shares > 0 else "卖出"
        instructions.append(TradeInstruction(
            code=code, action=action, amount=round(amount, 2),
            shares=abs(delta_shares),
            reason=f"当前{cur}股 -> 目标{tgt}股"
        ))
    return instructions


def allocate_incremental_capital(current_shares: dict, current_prices: dict,
                                  target_weights: dict, new_capital: float) -> dict:
    """
    增量资金优先策略：当有新资金注入时，不动用已有持仓，
    仅用新资金单向买入"当前权重缺口最大"的标的，按缺口比例分配新资金，
    尽量减少卖出交易（降低手续费损耗）。
    返回 {code: 新增股数}
    """
    current_value = {c: current_shares.get(c, 0) * current_prices.get(c, 0)
                      for c in target_weights}
    total_value = sum(current_value.values()) + new_capital
    if total_value <= 0:
        return {}

    target_value = {c: total_weight * total_value for c, total_weight in target_weights.items()}
    gap = {c: max(0.0, target_value[c] - current_value.get(c, 0)) for c in target_weights}
    total_gap = sum(gap.values())
    if total_gap <= 0:
        return {}

    new_shares = {}
    remaining_capital = new_capital
    # 按缺口占比分配，同时做整手取整
    for code, g in sorted(gap.items(), key=lambda x: -x[1]):
        price = current_prices.get(code)
        if not price or price <= 0:
            continue
        allocated = new_capital * (g / total_gap)
        allocated = min(allocated, remaining_capital)
        lots = int(allocated // (price * config.LOT_SIZE))
        shares = lots * config.LOT_SIZE
        cost = shares * price
        if cost >= config.MIN_TRADE_AMOUNT and shares > 0:
            new_shares[code] = shares
            remaining_capital -= cost

    return new_shares


def run_rebalance_check(current_weights: dict, target_weights: dict,
                         current_shares: dict, current_prices: dict,
                         target_shares: dict, total_capital: float,
                         state_file: Path = None) -> dict:
    """
    完整调仓决策流程入口。
    state_file 缺省时为旧的全局 config.STATE_FILE；多组合场景传各组合自己的文件。
    返回: {"triggered": bool, "reason": str, "instructions": [...], "state_updated": bool}
    """
    state = load_state(state_file)
    drift = compute_drift(current_weights, target_weights)
    triggered, reason = should_trigger_rebalance(drift, state.get("last_rebalance_date"))

    result = {"triggered": triggered, "reason": reason, "drift": drift, "instructions": []}

    if triggered:
        instructions = generate_trade_instructions(
            current_shares, current_prices, target_shares, total_capital)
        result["instructions"] = instructions
        state["last_rebalance_date"] = dt.date.today().isoformat()
        state["holdings"] = target_shares
        save_state(state, state_file)
        result["state_updated"] = True
    else:
        result["state_updated"] = False

    return result
