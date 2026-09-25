# -*- coding: utf-8 -*-
"""
backtest_k_sweep.py
四档 ALPHA_K × 三档资金 = 12 组诊断回测

目的：确认因子打分在纯股票环境下的真实表现
- k = -0.1, 0, 0.05, 0.1
- 三档资金：50000, 200000, 1000000

执行：
  python backtest_k_sweep.py --warm-start 2001-01-01 --start 2003-01-01 --end 2026-09-19

输出：
  12 组对比表格 + 结论（因子有效/反向/无效/参数问题）
"""
import datetime as dt
import logging
import sys
import pickle
from pathlib import Path

import config
import backtest_stock as bs

logger = logging.getLogger("backtest_k_sweep")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    # 参数
    ws = dt.date(2001, 1, 1)
    sd = dt.date(2003, 1, 1)
    ed = dt.date(2026, 9, 19)

    k_values = [-0.1, 0.0, 0.05, 0.1]
    caps = [50000, 200000, 1000000]

    print("=" * 100)
    print("四档 ALPHA_K × 三档资金 诊断回测")
    print("=" * 100)
    print(f"预热期: {ws} ~ {sd.replace(day=1) - dt.timedelta(days=1)}")
    print(f"正式期: {sd} ~ {ed}")
    print(f"ALPHA_K: {k_values}")
    print(f"资金档位: {[f'{c/10000:.0f}万' for c in caps]}")
    print("=" * 100 + "\n")

    # 加载行情（只需一次）
    print("加载全市场行情...", flush=True)
    mkt = bs.Market(bs.load_market())
    print(f"  行情 {len(mkt.df):,} 行，{len(mkt.dates)} 个交易日\n", flush=True)

    # 候选池（只需算一次）
    cache = config.CACHE_DIR / f"stock_pools_{ws}_{ed}.pkl"
    if cache.exists():
        print(f"载入候选池缓存 {cache.name}", flush=True)
        with open(cache, "rb") as f:
            pools = pickle.load(f)
    else:
        print("预计算候选池（每季度一次，较慢）...", flush=True)
        pools = bs.precompute_pools(mkt, ws, ed)
        with open(cache, "wb") as f:
            pickle.dump(pools, f)
    print(f"  候选池 {len(pools)} 期\n", flush=True)

    # 12 组回测
    all_results = []
    for k in k_values:
        # 临时修改 config.ALPHA_K
        original_k = config.ALPHA_K
        config.ALPHA_K = k

        print(f"\n{'='*100}")
        print(f"ALPHA_K = {k:+.2f}")
        print(f"{'='*100}\n")

        for cap in caps:
            print(f"  资金 {cap/10000:.0f}万 ...", flush=True)
            r = bs.run_backtest(
                cap, mkt, pools, sd, ed,
                label=f"k={k:+.2f}_cap={cap/10000:.0f}万",
                warm_start=ws
            )
            r["k"] = k
            r["capital"] = cap
            all_results.append(r)
            print(f"    年化 {r['annual_ret']:>7.2%}  夏普 {r['sharpe']:>6.2f}  "
                  f"最大回撤 {r['max_dd']:>7.2%}  调仓 {r['n_rebalance']} 次\n", flush=True)

        # 恢复原值
        config.ALPHA_K = original_k

    # 输出汇总表
    print("\n" + "=" * 120)
    print("【诊断结果汇总】")
    print("=" * 120)
    print(f"{'ALPHA_K':>8} | {'资金':>6} | {'年化收益':>9} | {'夏普比率':>9} | "
          f"{'最大回撤':>9} | {'年化波动':>9} | {'调仓次数':>8}")
    print("-" * 120)

    for r in all_results:
        print(f"{r['k']:>+8.2f} | {r['capital']/10000:>5.0f}万 | "
              f"{r['annual_ret']:>8.2%} | {r['sharpe']:>9.2f} | "
              f"{r['max_dd']:>9.2%} | {r['annual_vol']:>8.2%} | {r['n_rebalance']:>8}")

    # 分析结论
    print("\n" + "=" * 120)
    print("【分析结论】")
    print("=" * 120)

    # 按 k 值分组，计算平均年化收益
    k_performance = {}
    for k in k_values:
        subset = [r for r in all_results if r['k'] == k]
        avg_ret = sum(r['annual_ret'] for r in subset) / len(subset)
        avg_sharpe = sum(r['sharpe'] for r in subset) / len(subset)
        k_performance[k] = {'ret': avg_ret, 'sharpe': avg_sharpe}

    best_k = max(k_performance.items(), key=lambda x: x[1]['ret'])
    worst_k = min(k_performance.items(), key=lambda x: x[1]['ret'])

    print(f"\n各 k 值的平均表现（跨三档资金）：")
    for k in k_values:
        perf = k_performance[k]
        print(f"  k={k:+.2f}: 平均年化 {perf['ret']:>7.2%}  平均夏普 {perf['sharpe']:>6.2f}")

    print(f"\n最佳 k 值: {best_k[0]:+.2f} (年化 {best_k[1]['ret']:.2%})")
    print(f"最差 k 值: {worst_k[0]:+.2f} (年化 {worst_k[1]['ret']:.2%})")

    # 判断
    k0_perf = k_performance[0.0]
    best_perf = best_k[1]['ret']

    print("\n" + "-" * 120)
    if best_k[0] < 0:
        print("🔴 因子呈【反向】特征：负 k 值表现最佳，说明打分越高的股票表现越差")
        print("   → 建议：因子权重取反，或重新审视因子构造")
    elif abs(best_k[0]) < 0.01:  # k=0 最好
        print("🔴 因子【无效】：k=0（不用因子）表现最佳")
        print("   → 建议：放弃当前因子体系，回到混合模式测试资产配置能力")
    elif best_perf > k0_perf['ret'] + 0.01:  # 比 k=0 明显更好（>1pp）
        print("🟢 因子【有效】：最佳 k 值显著优于 k=0")
        print(f"   → 建议：采用 k={best_k[0]:.2f}，并考虑继续调优")
    else:
        print("🟡 因子效果【不明显】：各 k 值表现接近，差异可能是噪音")
        print("   → 建议：回到混合模式，测试'因子选股+资产配置'组合效果")
    print("-" * 120)

    # 保存结果
    output = config.PROJECT_ROOT / f"k_sweep_results_{ed.isoformat()}.pkl"
    with open(output, "wb") as f:
        pickle.dump(all_results, f)
    print(f"\n结果已保存: {output}")


if __name__ == "__main__":
    main()
