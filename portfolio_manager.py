# -*- coding: utf-8 -*-
"""
portfolio_manager.py
多组合并行管理：组合的创建/读取/删除/结果保存，每个组合一个独立存档文件
（.cache/portfolios/<id>.json）。资金量、持仓、调仓状态都挂在组合对象上，
不再依赖全局 config.TOTAL_CAPITAL / config.STATE_FILE，组合之间互不污染。

只做"组合容器"，内核（防雷网/因子打分/组合优化/调仓触发规则）不在此改动。
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import re
import uuid
from dataclasses import asdict
from pathlib import Path

import config
import rebalancer
from backtest_compare import PredictionBaseline

logger = logging.getLogger("portfolio_manager")

PORTFOLIOS_DIR = config.PROJECT_ROOT / ".cache" / "portfolios"


def _safe_id(pid: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", str(pid))


def portfolio_path(pid: str) -> Path:
    return PORTFOLIOS_DIR / f"{_safe_id(pid)}.json"


def load_portfolio(pid: str) -> dict | None:
    p = portfolio_path(pid)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_portfolio(portfolio: dict) -> None:
    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    with open(portfolio_path(portfolio["id"]), "w", encoding="utf-8") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2, default=str)


def list_portfolios() -> list[dict]:
    if not PORTFOLIOS_DIR.exists():
        return []
    out = []
    for p in sorted(PORTFOLIOS_DIR.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"组合文件读取失败 {p.name}: {e}")
    return sorted(out, key=lambda x: x.get("created_at", ""), reverse=True)


def create_portfolio(name: str, capital: float, mode: str = "etf_only") -> str:
    """创建空组合壳（资金独立于全局 config.TOTAL_CAPITAL，mode 由用户自选）"""
    pid = uuid.uuid4().hex
    save_portfolio({
        "id": pid,
        "name": name.strip(),
        "capital": float(capital),
        "mode": mode,
        "created_at": dt.date.today().isoformat(),
        "final_pool": [],
        "target_weights": {},
        "realized_weights": {},
        "shares": {},
        "latest_prices": {},
        "baseline": None,           # 建仓时的预测基准（backtest_compare.PredictionBaseline）
        "weight_history": [],       # [[生效日期, {code:权重}], ...]，建仓及每次调仓追加一段
        "last_rebalance_date": None,
        "holdings": {},
    })
    logger.info(f"新建组合: {name} (id={pid}, 资金={capital}, mode={mode})")
    return pid


def delete_portfolio(pid: str) -> bool:
    p = portfolio_path(pid)
    if p.exists():
        p.unlink()
        logger.info(f"删除组合: {pid}")
        return True
    return False


def _append_weight_segment(pf: dict, weights: dict, date: str = None) -> bool:
    """往 weight_history 追加一段(生效日期, 权重)；同日且同权重则去重跳过。"""
    date = date or dt.date.today().isoformat()
    wh = pf.setdefault("weight_history", [])
    new_w = {str(k): float(v) for k, v in weights.items()}
    if wh and wh[-1][0] == date and wh[-1][1] == new_w:
        return False
    wh.append([date, new_w])
    return True


def finalize_portfolio(pid: str, final_pool: list, target_weights: dict,
                       shares: dict, realized_weights: dict, latest_prices: dict,
                       mode: str = None,
                       prediction_baseline: PredictionBaseline = None) -> dict:
    """
    保存一次"选完的池 + 权重 + 整手结果"到组合，并完成首次建仓：
    空仓→目标权重偏离必然 > 阈值，run_rebalance_check 会写入 holdings 与
    last_rebalance_date（视为当天建仓）。
    prediction_baseline: 建仓时的预测基准（backtest_compare 生成），存为组合档案不再变化。
    返回落盘后的组合对象。
    """
    pf = load_portfolio(pid)
    if pf is None:
        raise ValueError(f"组合不存在: {pid}")

    if mode is not None:
        pf["mode"] = mode
    pf.update({
        "final_pool": list(final_pool),
        "target_weights": {str(k): float(v) for k, v in target_weights.items()},
        "realized_weights": {str(k): float(v) for k, v in realized_weights.items()},
        "shares": {str(k): int(v) for k, v in shares.items()},
        "latest_prices": {str(k): float(v) for k, v in latest_prices.items()},
    })
    if prediction_baseline is not None:
        pf["baseline"] = asdict(prediction_baseline)
    _append_weight_segment(pf, pf["realized_weights"])
    save_portfolio(pf)

    # 首次建仓：空仓→目标权重，触发调仓检测并记录持仓与日期
    rebalancer.run_rebalance_check(
        current_weights={},
        target_weights=pf["realized_weights"],
        current_shares={},
        current_prices=pf["latest_prices"],
        target_shares=pf["shares"],
        total_capital=pf["capital"],
        state_file=portfolio_path(pid),
    )
    return load_portfolio(pid)


def record_rebalance(pid: str, new_weights: dict) -> None:
    """调仓触发后调用：往 weight_history 追加一段(当天, 新权重)，供实际净值曲线分段。"""
    pf = load_portfolio(pid)
    if pf is None:
        return
    _append_weight_segment(pf, new_weights)
    save_portfolio(pf)


def rebalance_days(pf: dict) -> int | None:
    """距上次调仓天数（未建仓返回 None）"""
    last = pf.get("last_rebalance_date")
    if not last:
        return None
    return (dt.date.today() - dt.date.fromisoformat(str(last))).days


def compute_reminder(pf: dict) -> tuple[bool, str]:
    """
    基于组合已存价格估算当前是否触发再平衡提醒（展示用）。
    定时任务 scheduler_check.py 会用最新行情刷新 latest_prices 后再判断。
    """
    if not pf.get("holdings"):
        return True, "尚未建仓"
    current_weights = {}
    if pf["capital"] > 0:
        for code, sh in pf["holdings"].items():
            px = pf["latest_prices"].get(code)
            if px and sh:
                current_weights[code] = round((sh * px) / pf["capital"], 6)
    drift = rebalancer.compute_drift(current_weights, pf.get("realized_weights", {}))
    triggered, reason = rebalancer.should_trigger_rebalance(
        drift, pf.get("last_rebalance_date"))
    return triggered, reason
