# -*- coding: utf-8 -*-
"""AppTest 端到端验证（轻量版，隔离临时目录，不触碰真实组合数据）。

为避免频繁消耗他人 tushare 账号调用，此自动化测试只走"纯ETF"全流程
（新建→拉取→求解→保存→对比→调仓→删除）。混合模式的 25 只个股重型流程
已在模块级受控验证（build_investable_universe + generate_candidate_pool）。
"""
import sys
import shutil
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import portfolio_manager as pm
from streamlit.testing.v1 import AppTest

_TEST_DIR = Path(tempfile.mkdtemp(prefix="portfolio_test_"))
pm.PORTFOLIOS_DIR = _TEST_DIR

at = AppTest.from_file("app.py", default_timeout=300)
at.run()
print("初始加载 exceptions:", [str(e.value) for e in at.exception])
assert not at.exception, "初始加载失败"

# ---------- 建"纯ETF"组合 A ----------
at.text_input(key="new_name").set_value("纯ETF测试")
at.number_input(key="new_capital").set_value(30000.0)
at.selectbox(key="new_mode").set_value("etf_only")
at.button(key="btn_create").click().run()
assert not at.exception, "创建A失败"
A = [p for p in pm.list_portfolios() if p["name"] == "纯ETF测试"][0]
print("A mode:", A["mode"], "| 资金:", A["capital"])

# ---------- A 完整流程 ----------
at.button(key="btn_fetch").click().run()
assert not at.exception, "A拉取失败"
poolA = at.session_state[f"{A['id']}:candidate_pool"]
typesA = poolA["asset_type"].tolist() if hasattr(poolA, "asset_type") else []
print("A 候选池类型:", sorted(set(typesA)), "| ETF数量:", len(poolA))
assert set(typesA) == {"ETF"}, "纯ETF模式候选池应全为ETF"
assert len(poolA) == len([c for c in config.ETF_POOL]), "ETF候选池应保留全部池内标的"
at.button(key=f"solve_{A['id']}").click().run()
assert not at.exception, "A求解失败"
at.button(key=f"save_{A['id']}").click().run()
assert not at.exception, "A保存失败"
A2 = pm.load_portfolio(A["id"])
print("A baseline 有:", A2.get("baseline") is not None, "| weight_history:", len(A2.get("weight_history") or []))
assert A2.get("baseline"), "A 应有预测基准"
assert len(A2.get("weight_history") or []) >= 1, "A 应有weight_history"

# ---------- 预测vs实际对比 ----------
at.button(key=f"compare_{A['id']}").click().run()
assert not at.exception, "A 对比图生成失败"
print("A 对比图 plotly 数量:", len(at.get("plotly_chart")))

# ---------- 模拟调仓 → weight_history 多一段 ----------
pm.record_rebalance(A["id"], {"510300": 0.5, "510880": 0.5})
print("调仓后 weight_history 段数:", len(pm.load_portfolio(A["id"])["weight_history"]))
assert len(pm.load_portfolio(A["id"])["weight_history"]) == 2, "调仓后应多一段"

# ---------- 删除 A ----------
at.button(key="btn_delete").click().run()
assert not at.exception, "删除A失败"
assert len(pm.list_portfolios()) == 0, "删除后应为空"

print("\n===== 轻量端到端验证全部通过 =====")
shutil.rmtree(_TEST_DIR, ignore_errors=True)
print("测试临时目录已清理。真实组合目录全程未触碰。")
