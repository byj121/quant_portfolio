# -*- coding: utf-8 -*-
"""
app.py
Streamlit 人机交互控制台（多组合版）：
0. 组合卡片一览 + 新建 / 删除 / 切换组合
1. 展示 Top N 候选池，支持勾选剔除（黑名单）/手动加入自选（白名单）
2. 组合优化 + 单手门槛校验 + 调仓决策
3. 输出"一句话下单指南"
4. 权重饼图 + 相关性热力图

每个组合的资金、持仓、调仓状态独立存放（portfolio_manager.py），
不再依赖/修改全局 config.TOTAL_CAPITAL，组合之间互不影响。

运行方式: streamlit run app.py
"""
from __future__ import annotations
import datetime as dt

import pandas as pd
import plotly.express as px
import streamlit as st

import config
import csmar_loader
import data_loader
import factor_engine
import optimizer
import rebalancer
import backtest_compare
import fundamentals
import portfolio_manager as pm

st.set_page_config(page_title="quant_portfolio 自适应量化配置系统", layout="wide")


def _disp(code: str) -> str:
    """展示用：把代码显示成 "600519 贵州茅台"（名称取自 CSMAR 离线映射，不消耗 API）。"""
    return csmar_loader.labeled(code)


st.title("📊 quant_portfolio — 自适应量化资产配置系统")
st.caption(config.DISCLAIMER)
st.info("仅支持场内交易品种（A股 / 场内ETF），不涉及场外基金申赎。", icon="ℹ️")


# ---------------------------------------------------------------------------
# 侧边栏：组合管理 + 行情参数
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("🗂️ 组合管理")

    st.caption("新建组合（资金、投资模式由你自选）")
    new_name = st.text_input("组合名称", placeholder="例如：稳健ETF", key="new_name")
    new_capital = st.number_input("组合资金（元）", min_value=1000.0,
                                   value=float(config.TOTAL_CAPITAL), step=1000.0, key="new_capital")
    new_mode = st.selectbox("投资模式", config.INVESTMENT_MODE_OPTIONS,
                            format_func=lambda m: config.MODE_LABELS.get(m, m),
                            key="new_mode")
    if st.button("创建组合", key="btn_create"):
        if not new_name.strip():
            st.error("请填写组合名称")
        else:
            pid = pm.create_portfolio(new_name.strip(), float(new_capital), mode=new_mode)
            st.session_state["current_portfolio_id"] = pid
            st.success(f"已创建「{new_name.strip()}」，资金 {new_capital:.0f} 元，模式："
                       f"{config.MODE_LABELS.get(new_mode, new_mode)}")

    st.divider()
    portfolios = pm.list_portfolios()
    if not portfolios:
        st.info("还没有组合，先在上方新建一个")
    else:
        name_map = {p["id"]: p["name"] for p in portfolios}
        cur = st.session_state.get("current_portfolio_id")
        if cur not in name_map:
            cur = portfolios[0]["id"]
            st.session_state["current_portfolio_id"] = cur
        pid = st.selectbox(
            "选择组合",
            list(name_map.keys()),
            format_func=lambda x: f"{name_map[x]}（{pm.load_portfolio(x)['capital']:.0f}元）",
            key="sel_portfolio",
        )
        st.session_state["current_portfolio_id"] = pid
        if st.button("🗑️ 删除此组合", key="btn_delete"):
            pm.delete_portfolio(pid)
            st.session_state.pop("current_portfolio_id", None)
            st.rerun()

    st.divider()
    st.header("⚙️ 行情参数")
    lookback_days = st.slider("行情回看天数", 250, 800, 500, step=50)
    run_button = st.button("🚀 拉取数据 & 生成候选池", type="primary", key="btn_fetch")


# ---------------------------------------------------------------------------
# 主区 ①：所有组合卡片一览
# ---------------------------------------------------------------------------
portfolios = pm.list_portfolios()
if portfolios:
    st.subheader("🗂️ 我的组合（全部）")
    ncol = min(len(portfolios), 4)
    cols = st.columns(ncol)
    for i, pf in enumerate(portfolios):
        with cols[i % ncol]:
            st.markdown(f"**{pf['name']}**")
            st.metric("总资金", f"¥{pf['capital']:.0f}")
            n_hold = len([c for c, s in (pf.get("holdings") or {}).items() if s])
            st.metric("当前持仓", f"{n_hold} 只" if n_hold else "空仓")
            days = pm.rebalance_days(pf)
            st.metric("距上次调仓", f"{days} 天" if days is not None else "—")
            trig, reason = pm.compute_reminder(pf)
            st.caption("🔴 需再平衡" if trig else "🟢 正常")
    st.divider()


# ---------------------------------------------------------------------------
# 主区 ②：当前组合详情 + 选股/求解/保存流程
# ---------------------------------------------------------------------------
cur_pf = pm.load_portfolio(st.session_state.get("current_portfolio_id")) \
    if st.session_state.get("current_portfolio_id") else None

if cur_pf is None:
    st.info("👈 左侧新建或选择一个组合后再进行操作")
else:
    pid = cur_pf["id"]
    capital = float(cur_pf["capital"])
    has_result = bool(cur_pf.get("shares"))

    mode_label = config.MODE_LABELS.get(cur_pf.get("mode", "etf_only"), "纯ETF")
    st.subheader(f"当前组合：{cur_pf['name']}（资金 ¥{capital:.0f}，{mode_label}）")

    # 已建仓时展示当前持仓
    if has_result:
        holds = {c: s for c, s in (cur_pf.get("holdings") or {}).items() if s}
        if holds:
            rows = [{"标的": _disp(c), "持股": s,
                     "市值": round(s * cur_pf["latest_prices"].get(c, 0), 2)}
                    for c, s in holds.items()]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        trig, reason = pm.compute_reminder(cur_pf)
        st.caption("再平衡提醒：" + ("🔴 " if trig else "🟢 ") + reason)

    # 会话临时状态按组合隔离，避免切换组合时数据串台
    P = {k: f"{pid}:{k}" for k in
         ["universe", "candidate_pool", "price_data_all", "weights", "method",
          "price_matrix", "dropped"]}
    for k in P.values():
        if k not in st.session_state:
            st.session_state[k] = None

    # ---------- Step 1: 拉取数据 + 防雷网 + 基本面打分生成候选池 ----------
    if run_button:
        with st.spinner("正在执行全市场防雷筛选与因子打分（约30秒）..."):
            try:
                mode = cur_pf.get("mode", "etf_only")
                end = dt.date.today().strftime("%Y%m%d")
                start = (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%Y%m%d")

                # —— ETF 部分：纯股票模式不要 ETF；其余模式跑 ETF 防雷（8只全保留）——
                etf_list, rejected_etf = [], []
                if mode != "stock_only":
                    uni = data_loader.build_investable_universe(mode=mode)
                    etf_list, rejected_etf = uni["etf"], uni["rejected"]

                # ETF 因子需要其历史行情
                price_data_etf = {c: data_loader.get_etf_daily(c, start, end) for c in etf_list}
                st.session_state[P["price_data_all"]] = dict(price_data_etf)

                # —— 个股部分：全市场9条防雷筛选（CSMAR）→ 构建打分 profiles ——
                stock_profiles, screen_info = [], None
                if mode in ("mixed", "stock_only"):
                    import csmar_screen
                    scr = csmar_screen.screen_market(as_of=dt.date.today())
                    passed = scr[scr["passed"] & scr["latest_close"].notna()]
                    screen_info = {
                        "total": int(len(scr)),
                        "passed": int(len(passed)),
                        "rejected": int((~scr["passed"]).sum()),
                        "reasons": scr[~scr["passed"]]["reason"].value_counts().to_dict(),
                    }
                    stock_profiles = csmar_screen.build_profiles(list(passed.index), snapshot=passed)

                st.session_state[P["universe"]] = {
                    "mode": mode, "etf": etf_list, "rejected": rejected_etf,
                    "screen": screen_info,
                }

                candidate_pool = factor_engine.generate_candidate_pool(
                    price_data_etf=price_data_etf, stock_profiles=stock_profiles)
                st.session_state[P["candidate_pool"]] = candidate_pool

                if screen_info:
                    st.success(f"全市场防雷筛选：{screen_info['total']:,} 只 → 通过 {screen_info['passed']:,} 只"
                               f"（剔除 {screen_info['rejected']:,} 只）→ 因子打分取 Top{config.STOCK_TOP_N}；"
                               f"ETF 候选 {len(etf_list)} 只 → 入池 {len(candidate_pool)} 只")
                    with st.expander(f"⚠️ 防雷网剔除分布（共 {screen_info['rejected']:,} 只）"):
                        st.table(pd.DataFrame(
                            [{"剔除原因": k, "只数": v} for k, v in screen_info["reasons"].items()]))
                else:
                    st.success(f"候选池构建完成：ETF候选{len(etf_list)}只 → 入池 {len(candidate_pool)} 只")
                if rejected_etf:
                    with st.expander(f"⚠️ ETF 防雷剔除（{len(rejected_etf)}只）"):
                        st.table(pd.DataFrame(
                            [{"标的": _disp(c), "剔除原因": r} for c, r in rejected_etf]))
            except fundamentals.RateLimitError as e:
                st.error(f"⚠️ 数据接口限流，已停止后续请求（不重试）：{e}")
            except Exception as e:
                st.error(f"数据拉取失败: {e}")

    # ---------- Step 2: 候选池勾选 ----------
    candidate_pool = st.session_state.get(P["candidate_pool"])
    if candidate_pool is not None and not candidate_pool.empty:
        st.subheader("① Top 候选池 — 勾选剔除 / 手动加入自选")
        pool_df = candidate_pool.reset_index().rename(columns={"index": "code"})
        pool_df["标的"] = [_disp(c) for c in pool_df["code"]]
        pool_df["类型"] = pool_df["asset_type"].map({"ETF": "ETF", "STOCK": "个股"}).fillna(pool_df["asset_type"])
        pool_df["剔除"] = False

        edited = st.data_editor(
            pool_df[["标的", "code", "类型", "alpha_score", "剔除"]],
            column_config={
                "标的": "标的（代码 名称）",
                "code": None,  # 隐藏，仅供后端逻辑使用
                "类型": "类型", "alpha_score": "Alpha得分",
                "剔除": st.column_config.CheckboxColumn("勾选剔除(黑名单)"),
            },
            hide_index=True, use_container_width=True, key=f"pool_editor_{pid}",
        )

        whitelist_input = st.text_input("手动强行加入自选标的代码（逗号分隔，如 600519,510300）", "",
                                        key=f"whitelist_{pid}")
        whitelist_codes = [c.strip() for c in whitelist_input.split(",") if c.strip()]
        blacklist_codes = edited.loc[edited["剔除"], "code"].tolist()
        final_pool = [c for c in edited["code"].tolist() if c not in blacklist_codes]
        final_pool = list(dict.fromkeys(final_pool + whitelist_codes))  # 去重保序

        st.write(f"**最终标的池（{len(final_pool)}只）**: " + "、".join(_disp(c) for c in final_pool))

        # ---------- Step 3: 组合优化 ----------
        if st.button("📐 求解最优组合权重", key=f"solve_{pid}"):
            with st.spinner("计算周线协方差矩阵并求解组合权重..."):
                try:
                    price_data = st.session_state.get(P["price_data_all"]) or {}
                    end = dt.date.today().strftime("%Y%m%d")
                    start = (dt.date.today() - dt.timedelta(days=lookback_days)).strftime("%Y%m%d")
                    # 补拉最终池中缺失行情的标的（混合/纯股票模式的个股行情在此按需获取）
                    for c in final_pool:
                        if c not in price_data or price_data[c] is None or price_data[c].empty:
                            df = data_loader.get_etf_daily(c, start, end)
                            if df.empty:
                                df = data_loader.get_stock_daily(c, start, end)
                            price_data[c] = df
                    # ★ 必须写回 session_state：否则下一步"整手校验"读不到个股行情，
                    #   会导致混合模式静默丢弃全部个股、纯股票模式直接崩。
                    st.session_state[P["price_data_all"]] = price_data

                    selected_price_data = {c: price_data[c] for c in final_pool
                                            if c in price_data and not price_data[c].empty}
                    price_matrix = optimizer.build_weekly_returns(selected_price_data)
                    weights, method, dropped = optimizer.solve_portfolio(price_matrix)
                    st.session_state[P["weights"]] = weights
                    st.session_state[P["method"]] = method
                    st.session_state[P["price_matrix"]] = price_matrix
                    st.session_state[P["dropped"]] = dropped
                    st.success(f"求解完成，采用方法："
                               f"{'Max Sharpe' if method == 'max_sharpe' else 'HRP（集中度熔断已触发）'}")
                except Exception as e:
                    st.error(f"组合求解失败: {e}")

        # ---------- Step 4: 单手校验 + 保存 ----------
        weights = st.session_state.get(P["weights"])
        if weights:
            price_data = st.session_state.get(P["price_data_all"]) or {}
            st.subheader("② 组合权重与单手门槛校验")
            latest_prices = {c: price_data[c]["close"].iloc[-1] for c in weights
                              if c in price_data and not price_data[c].empty}
            lot_result = optimizer.lot_size_check_and_round(weights, latest_prices, capital)

            col1, col2 = st.columns([1, 1])
            with col1:
                weight_df = pd.DataFrame(
                    [{"标的": _disp(c), "权重": w} for c, w in lot_result["final_weights"].items()])
                st.plotly_chart(px.pie(weight_df, names="标的", values="权重", title="最终配置权重"),
                                use_container_width=True)
            with col2:
                returns_matrix = pd.DataFrame({
                    c: price_data[c]["close"].pct_change() for c in lot_result["final_weights"]
                    if c in price_data}).dropna(how="all")
                if returns_matrix.shape[1] > 1:
                    corr = returns_matrix.corr()
                    corr.index = [_disp(c) for c in corr.index]
                    corr.columns = [_disp(c) for c in corr.columns]
                    st.plotly_chart(px.imshow(corr, text_auto=".2f",
                                              title="标的相关性热力图",
                                              color_continuous_scale="RdBu_r", zmin=-1, zmax=1),
                                    use_container_width=True)

            if lot_result["excluded"]:
                st.warning("以下标的因分配金额不足1手已被剔除，权重已重新分摊：")
                st.table(pd.DataFrame([{"标的": _disp(c), "原因": r} for c, r in lot_result["excluded"]]))

            dropped = st.session_state.get(P["dropped"]) or []
            if dropped:
                st.warning("以下标的因优化后权重过小已被过滤：")
                st.table(pd.DataFrame([{"标的": _disp(c), "原因": r} for c, r in dropped]))

            # 标的去向核对：最终标的池 = 最终权重 + 1手剔除 + 权重过小过滤 + 无行情数据
            price_matrix = st.session_state.get(P["price_matrix"])
            codes_in = set(price_matrix.columns) if price_matrix is not None else set()
            n_no_data = len([c for c in final_pool if c not in codes_in])
            n_final = len(lot_result["final_weights"])
            n_lot = len(lot_result["excluded"])
            n_small = len(dropped)
            if len(final_pool) == n_final + n_lot + n_small + n_no_data:
                st.caption(f"✅ 标的去向核对：最终标的池 {len(final_pool)} 只 = 最终权重 {n_final} 只 + "
                           f"1手不足剔除 {n_lot} 只 + 权重过小过滤 {n_small} 只"
                           + (f" + 无行情数据 {n_no_data} 只" if n_no_data else "")
                           + "，无不明消失的标的")
            else:
                st.warning(f"⚠️ 标的去向核对异常：最终标的池 {len(final_pool)} 只 ≠ "
                           f"最终权重 {n_final} + 1手剔除 {n_lot} + 权重过小 {n_small}"
                           + (f" + 无行情数据 {n_no_data}" if n_no_data else ""))

            st.metric("剩余现金（零头）", f"¥{lot_result['cash_left']:.2f}")

            st.subheader("③ 一句话下单指南")
            order_rows = [{"标的": _disp(code),
                           "目标挂单手数": shares // config.LOT_SIZE,
                           "目标股数": shares,
                           "预计金额(元)": round(shares * latest_prices.get(code, 0), 2)}
                          for code, shares in lot_result["shares"].items()]
            st.dataframe(pd.DataFrame(order_rows), use_container_width=True, hide_index=True)

            if st.button(f"✅ 保存此结果为组合「{cur_pf['name']}」", type="primary", key=f"save_{pid}"):
                price_matrix = st.session_state.get(P["price_matrix"])
                baseline = None
                if price_matrix is not None and not price_matrix.empty:
                    try:
                        mu, S = optimizer.get_expected_returns_and_cov(price_matrix)
                        baseline = backtest_compare.build_prediction_baseline(
                            lot_result["realized_weights"], mu, S, dt.date.today())
                    except Exception as e:
                        st.warning(f"预测基准生成失败（不影响保存）：{e}")
                pm.finalize_portfolio(pid, final_pool, weights, lot_result["shares"],
                                      lot_result["realized_weights"], latest_prices,
                                      prediction_baseline=baseline)
                st.success("已保存并完成建仓（记录持仓、调仓日期与预测基准）")
                st.rerun()

            if has_result:
                st.caption("💡 此组合已有已保存结果；重新求解并点击保存会覆盖更新。")

            # ---------- 组合历史回测诊断（中文，多图）----------
            st.subheader("④ 组合历史回测诊断")
            port_returns = (returns_matrix.fillna(0) *
                             pd.Series(lot_result["final_weights"])).sum(axis=1)
            port_returns = port_returns.replace([float("inf"), float("-inf")], float("nan")).dropna()
            if len(port_returns) > 5:
                equity = (1 + port_returns).cumprod()
                n = len(port_returns)
                total_ret = float(equity.iloc[-1] - 1)
                annual_ret = float(equity.iloc[-1] ** (252 / n) - 1) if total_ret > -1 else -1.0
                annual_vol = float(port_returns.std() * (252 ** 0.5))
                sharpe = (annual_ret - config.RISK_FREE_RATE) / annual_vol if annual_vol > 0 else 0.0
                max_dd = float((equity / equity.cummax() - 1).min())
                win_rate = float((port_returns > 0).mean())

                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("累计收益", f"{total_ret:.2%}")
                m2.metric("年化收益", f"{annual_ret:.2%}")
                m3.metric("年化波动", f"{annual_vol:.2%}")
                m4.metric("夏普比率", f"{sharpe:.2f}")
                m5.metric("最大回撤", f"{max_dd:.2%}")
                st.caption(f"样本区间 {port_returns.index[0].date()} ~ {port_returns.index[-1].date()}，"
                           f"共 {n} 个交易日，日胜率 {win_rate:.1%}")

                # ① 净值曲线 + 回撤
                fig1 = px.line(equity, title="① 净值曲线")
                fig1.add_scatter(x=equity.index, y=equity / equity.cummax() - 1,
                                 name="回撤", yaxis="y2", fill="tozeroy")
                fig1.update_layout(yaxis2={"overlaying": "y", "side": "right", "title": "回撤"},
                                   legend=dict(orientation="h", yanchor="bottom", y=1.02))
                st.plotly_chart(fig1, use_container_width=True)

                # ② 月度收益热力图
                monthly = (1 + port_returns).resample("ME").prod() - 1
                mtable = monthly.to_frame("ret")
                mtable["year"] = mtable.index.year
                mtable["month"] = mtable.index.month
                mheat = mtable.pivot(index="year", columns="month", values="ret").reindex(
                    columns=list(range(1, 13)))
                fig2 = px.imshow(mheat * 100, text_auto=".1f", title="② 月度收益（%）",
                                 color_continuous_scale="RdBu_r", aspect="auto",
                                 labels={"x": "月份", "y": "年份", "color": "%"})
                st.plotly_chart(fig2, use_container_width=True)

                # ③ 滚动60日夏普 与 波动率
                roll_vol = (port_returns.rolling(60).std() * (252 ** 0.5)).replace(0, float("nan"))
                roll_sharpe = ((port_returns.rolling(60).mean() * 252 - config.RISK_FREE_RATE)
                               / roll_vol).replace([float("inf"), float("-inf")], float("nan"))
                fig3 = px.line(roll_sharpe, title="③ 滚动60日夏普比率（左轴）与波动率（右轴）")
                fig3.add_scatter(x=roll_vol.index, y=roll_vol, name="波动率", yaxis="y2")
                fig3.update_layout(yaxis2={"overlaying": "y", "side": "right", "title": "波动率"},
                                   legend=dict(orientation="h", yanchor="bottom", y=1.02))
                st.plotly_chart(fig3, use_container_width=True)

                # ④ 日收益分布
                fig4 = px.histogram(port_returns * 100, nbins=40, title="④ 日收益分布（%）",
                                    labels={"value": "日收益 %", "count": "天数"})
                fig4.add_vline(x=0, line_dash="dash", line_color="red")
                st.plotly_chart(fig4, use_container_width=True)

                # ⑤ 年度收益
                yearly = (1 + port_returns).resample("YE").prod() - 1
                fig5 = px.bar(yearly * 100, title="⑤ 年度收益（%）",
                              labels={"value": "年收益 %", "index": "年份"})
                fig5.add_hline(y=0, line_dash="dash", line_color="red")
                st.plotly_chart(fig5, use_container_width=True)
            else:
                st.info("回测样本不足，暂不展示")

    # ---------- 已建仓组合：调仓检测 ----------
    if has_result and cur_pf.get("holdings"):
        st.subheader("⑤ 调仓触发检测")
        if st.button("🔄 用最新价运行调仓检测", key=f"rebalance_{pid}"):
            end = dt.date.today().strftime("%Y%m%d")
            start = (dt.date.today() - dt.timedelta(days=45)).strftime("%Y%m%d")
            fresh = {}
            for c in set(cur_pf.get("shares") or {}) | set(cur_pf.get("holdings") or {}):
                df = data_loader.get_etf_daily(c, start, end)
                if df.empty:
                    df = data_loader.get_stock_daily(c, start, end)
                if not df.empty:
                    fresh[c] = float(df["close"].iloc[-1])
            if fresh:
                cur_pf["latest_prices"] = fresh
                pm.save_portfolio(cur_pf)
            latest_prices = cur_pf.get("latest_prices") or {}
            current_weights, current_shares = rebalancer.get_current_weights_from_state(
                latest_prices, capital, state_file=pm.portfolio_path(pid))
            result = rebalancer.run_rebalance_check(
                current_weights=current_weights,
                target_weights=cur_pf.get("realized_weights") or {},
                current_shares=current_shares,
                current_prices=latest_prices,
                target_shares=cur_pf.get("shares") or {},
                total_capital=capital,
                state_file=pm.portfolio_path(pid),
            )
            if result["triggered"]:
                pm.record_rebalance(pid, cur_pf.get("realized_weights") or {})
                st.success(f"✅ {result['reason']}")
                for ins in result["instructions"]:
                    st.write(f"- {ins.action} {_disp(ins.code)}  {ins.shares}股  约¥{ins.amount:.0f}  ({ins.reason})")
            else:
                st.info(f"⏸️ 暂不触发调仓：{result['reason']}")
            st.caption("提醒：定时自动检查请用 scheduler_check.py（cron / 任务计划程序）。")

    # ---------- 已建仓组合：预测 vs 实际净值对比 ----------
    cur_pf = pm.load_portfolio(pid)  # 重新读取，确保拿到最新 weight_history
    if has_result and cur_pf.get("holdings"):
        st.subheader("⑥ 预测 vs 实际净值对比")
        if st.button("📈 生成/刷新 预测vs实际对比图", key=f"compare_{pid}"):
            try:
                baseline = None
                if cur_pf.get("baseline"):
                    baseline = backtest_compare.PredictionBaseline(**cur_pf["baseline"])
                else:
                    st.info("此组合是在'预测vs实际'功能上线前创建的，缺少预测基准，只能画实际净值线。\n"
                            "想看到完整预测对比：在上方重新执行'拉取数据→求解→保存'一次即可生成。")

                # 权重历史：新组合直接读存档；旧组合用当前持仓权重合成一段
                wh = [(d, w) for d, w in (cur_pf.get("weight_history") or [])]
                if not wh:
                    w_now = cur_pf.get("realized_weights") or {}
                    eff_date = cur_pf.get("last_rebalance_date") or cur_pf.get("created_at")
                    if w_now and eff_date:
                        wh = [(eff_date, w_now)]

                if not wh:
                    st.warning("组合缺少权重历史，无法生成对比图")
                else:
                    end = dt.date.today().strftime("%Y%m%d")
                    start = dt.date.fromisoformat(wh[0][0]).strftime("%Y%m%d")
                    codes = set()
                    for _, w in wh:
                        codes.update(w.keys())
                    price_data = {}
                    for c in codes:
                        df = data_loader.get_etf_daily(c, start, end)
                        if df.empty:
                            df = data_loader.get_stock_daily(c, start, end)
                        if not df.empty:
                            price_data[c] = df

                    if baseline is not None:
                        comp = backtest_compare.compare_predicted_vs_actual(baseline, wh, price_data)
                        if comp.empty:
                            st.warning("对比数据不足（缺少行情覆盖）")
                        else:
                            chart_df = comp.rename(columns={
                                "predicted_nav": "预测净值", "actual_nav": "实际净值"})
                            fig = px.line(chart_df, title="预测 vs 实际净值",
                                          labels={"value": "净值", "index": "日期"})
                            st.plotly_chart(fig, use_container_width=True)
                            s = backtest_compare.summarize_deviation(comp)
                            st.write(f"**偏差摘要**：预测净值 {s['predicted_nav']} | "
                                     f"实际净值 {s['actual_nav']} | "
                                     f"偏差 {s['diff_pct']:+.2%} → **{s['status']}**")
                    else:
                        actual = backtest_compare.build_actual_nav_curve(wh, price_data)
                        if actual.empty:
                            st.warning("对比数据不足（缺少行情覆盖）")
                        else:
                            fig = px.line(actual.rename("实际净值"),
                                          title="实际净值（旧组合无预测基准）",
                                          labels={"value": "净值", "index": "日期"})
                            st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.error(f"对比图生成失败: {e}")

st.divider()
st.caption(config.DISCLAIMER)
