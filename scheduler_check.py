# -*- coding: utf-8 -*-
"""
scheduler_check.py
独立运行的定时检查脚本，不依赖 Streamlit，专门给 cron / Windows 任务计划程序调用。

多组合版：遍历所有已建组合（portfolio_manager.list_portfolios()），
每个组合按自己的标的池、资金、持仓、目标权重单独做调仓检测与通知，互不影响。

【调仓语义 —— 2026-09 用户拍板】
触发调仓时**不是**"调回旧目标"，而是**重新跑一遍完整筛选**：
    全市场9条防雷筛选 → 基本面因子打分 → 取 Top N → 组合优化 → 得到【全新目标权重】
这样标的池会自动新陈代谢（新上市/退市/基本面变化的公司自动进出），
不会出现"池子慢慢老化而我们察觉不到"的问题。

【数据来源】
  · 筛选/打分：CSMAR 离线数据（config.CSMAR_DATA_DIR，需用户定期重新下载以保持新鲜）
  · 价格历史（用于协方差与优化）：tushare（实盘走实时源）
若 CSMAR 数据缺失或重筛失败，**兜底沿用旧目标**（不会因为重筛报错而不调仓）。

用法示例（Linux/Mac，用 cron 每天下午3:30收盘后跑一次）：
    30 15 * * 1-5 cd /path/to/quant_portfolio && /path/to/venv/bin/python scheduler_check.py >> .cache/scheduler.log 2>&1

Windows 用"任务计划程序"，触发器设为每个工作日 15:30，操作里指向：
    python.exe  scheduler_check.py
    起始于（工作目录）：填 quant_portfolio 项目目录
"""
from __future__ import annotations
import datetime as dt
import logging

import config
import data_loader
import optimizer
import rebalancer
import notifier
import portfolio_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scheduler_check")


# ---------------------------------------------------------------------------
# 触发调仓时：重跑完整筛选，得到【全新】目标权重
# ---------------------------------------------------------------------------

def _re_screen(pf: dict) -> dict | None:
    """重新跑【筛选 → 打分 → 优化】，返回
    {"final_pool": [...], "weights": {...}, "realized_weights": {...}, "shares": {...}}
    失败返回 None（调用方兜底沿用旧目标）。
    """
    import csmar_screen

    pid = pf["id"]
    name = pf["name"]
    capital = float(pf["capital"])
    mode = pf.get("mode", "etf_only")
    as_of = dt.date.today()
    end = as_of.strftime("%Y%m%d")
    start = (as_of - dt.timedelta(days=500)).strftime("%Y%m%d")

    # 1) 筛选 + 打分 → 候选池
    if mode == "etf_only":
        codes_pool = list(config.ETF_POOL)
        final_pool = codes_pool
    else:
        pool, _profiles = csmar_screen.build_candidate_pool(as_of=as_of)
        final_pool = list(pool.index)
        if len(final_pool) < 2:
            logger.warning(f"[{name}] 重筛后候选池不足2只，放弃重筛")
            return None

    # 2) 拉标的池的价格历史（用于协方差/优化）；单只失败只跳过它，不中断整体
    price_data = {}
    etf_set = set(config.ETF_POOL)
    for c in final_pool:
        try:
            # ETF 走 ETF 接口，其余走个股接口（避免对股票先试探ETF接口白白多一轮请求）
            df = data_loader.get_etf_daily(c, start, end) if c in etf_set \
                else data_loader.get_stock_daily(c, start, end)
        except Exception as e:
            logger.warning(f"[{name}] {c} 行情获取异常，跳过该标的: {e}")
            continue
        if not df.empty:
            price_data[c] = df
    final_pool = [c for c in final_pool if c in price_data]
    if len(final_pool) < 2:
        logger.warning(f"[{name}] 重筛后有效行情不足2只，放弃重筛")
        return None

    # 3) 优化 + 整手
    price_matrix = optimizer.build_weekly_returns({c: price_data[c] for c in final_pool})
    weights, method, dropped = optimizer.solve_portfolio(price_matrix)
    latest_prices = {c: float(price_data[c]["close"].iloc[-1]) for c in final_pool}
    lot = optimizer.lot_size_check_and_round(weights, latest_prices, capital)

    logger.info(f"[{name}] 重筛完成：方法={method}，候选{len(final_pool)}只 → "
                f"最终持仓{len(lot['shares'])}只（1手不足剔除{len(lot['excluded'])}，"
                f"权重过小过滤{len(dropped)}）")
    return {
        "final_pool": final_pool,
        "weights": weights,
        "realized_weights": lot["realized_weights"],
        "shares": lot["shares"],
        "latest_prices": latest_prices,
    }


def _check_one(pf: dict) -> None:
    pid = pf["id"]
    name = pf["name"]
    capital = float(pf["capital"])
    final_pool = pf.get("final_pool") or []
    holdings = pf.get("holdings") or {}
    target_shares = pf.get("shares") or {}
    target_weights = pf.get("realized_weights") or {}

    if not final_pool and not holdings:
        logger.warning(f"组合[{name}] 尚未选池/建仓，跳过")
        return
    if len(target_shares) < 2 and len(holdings) < 2:
        logger.warning(f"组合[{name}] 可调标的不足2只，跳过")
        return

    logger.info(f"--- 检查组合[{name}] id={pid} 资金={capital:.0f} ---")

    # 拉取组合涉及的标的（池 + 持仓）最新行情
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=45)).strftime("%Y%m%d")
    price_data = {}
    for c in set(final_pool) | set(holdings.keys()):
        df = data_loader.get_etf_daily(c, start, end)
        if df.empty:
            df = data_loader.get_stock_daily(c, start, end)
        if not df.empty:
            price_data[c] = df
    latest_prices = {c: float(df["close"].iloc[-1]) for c, df in price_data.items() if not df.empty}
    if not latest_prices:
        logger.warning(f"组合[{name}] 无有效最新行情，跳过")
        return

    # 读取该组合真实当前持仓权重（各组合自己的状态文件）
    current_weights, current_shares = rebalancer.get_current_weights_from_state(
        latest_prices, capital, state_file=portfolio_manager.portfolio_path(pid))
    logger.info(f"[{name}] 当前持仓: {current_shares}")

    # 用【旧目标】做调仓检测（判断是否触发）
    rebalance_result = rebalancer.run_rebalance_check(
        current_weights=current_weights,
        target_weights=target_weights,
        current_shares=current_shares,
        current_prices=latest_prices,
        target_shares=target_shares,
        total_capital=capital,
        state_file=portfolio_manager.portfolio_path(pid),
    )
    logger.info(f"[{name}] 触发={rebalance_result['triggered']} | {rebalance_result['reason']}")

    if rebalance_result.get("state_updated"):
        # ★ 触发即【重筛】：重新跑 全市场筛选 → 打分 → 优化，得到全新目标
        new_target = None
        try:
            new_target = _re_screen(pf)
        except Exception as e:
            logger.error(f"[{name}] 重筛失败，兜底沿用旧目标: {e}", exc_info=True)

        if new_target:
            upd = portfolio_manager.load_portfolio(pid) or pf
            upd["final_pool"] = new_target["final_pool"]
            upd["shares"] = new_target["shares"]
            upd["weights"] = new_target["weights"]
            upd["realized_weights"] = new_target["realized_weights"]
            upd["latest_prices"] = new_target["latest_prices"]
            upd["last_rebalance_date"] = dt.date.today().isoformat()
            portfolio_manager.save_portfolio(upd)
            portfolio_manager.record_rebalance(pid, new_target["realized_weights"])
            rebalance_result["reason"] = (rebalance_result.get("reason", "") +
                                          "（已重跑完整筛选，目标为模型最新结果）").strip()
        else:
            # 兜底：沿用旧目标（保持原有行为，不至于因重筛出错而卡住）
            portfolio_manager.record_rebalance(pid, target_weights)

    # 用最新价刷新组合存档里的 latest_prices，供 app 首页提醒展示
    pf2 = portfolio_manager.load_portfolio(pid)
    if pf2 is not None and not pf2.get("latest_prices"):
        pf2["latest_prices"] = latest_prices
        portfolio_manager.save_portfolio(pf2)

    # 通知（邮件/webhook，按 config.NOTIFY_METHOD）
    subject, content = notifier.format_rebalance_message(rebalance_result)
    notifier.notify(subject, content)


def main():
    logger.info("=== 定时调仓检查开始 ===")
    portfolios = portfolio_manager.list_portfolios()
    if not portfolios:
        logger.info("没有任何已建组合，跳过本次检查")
        return

    for pf in portfolios:
        try:
            _check_one(pf)
        except Exception as e:
            logger.error(f"组合[{pf.get('name', pf.get('id'))}] 检查失败: {e}", exc_info=True)

    logger.info("=== 定时调仓检查结束 ===")


if __name__ == "__main__":
    main()
