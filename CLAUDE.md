# quant_portfolio — AI 工作手册

> 给 Claude Code 看的。给人类看的介绍在 [README.md](README.md)。
> 本文件每次对话**自动加载** —— 改了项目语义就回来改这里，否则我会拿旧认知给你干活。

## 这是什么

小资金（默认 3 万）的 A 股 / 场内 ETF 量化资产配置系统。**只做决策辅助，不接实盘交易接口**
—— 输出"一句话下单指南"，用户自己在券商 App 下单。支持**多组合并行**：每个组合有独立的资金、
投资模式（`etf_only` / `mixed` / `stock_only`）、持仓与调仓状态，互不污染。

## 铁律（动手前先看）

1. **跑任何脚本必须用项目自带 venv**：`.venv\Scripts\python.exe xxx.py`
   → 系统 Python 3.13 没装 pypfopt，直接敲 `python` 会 ImportError。
2. **大改动前先 `git commit` 存一档**（2026-09-25 起本仓库受 Git 管理，本地仓库、无远程）。
3. **改了被 import 的模块，Streamlit 必须重启**才生效（页面不热加载）：
   `stop_site.bat` → `start_site.bat`。只改 `app.py` 自身则刷新浏览器即可。
4. **`config.py` 是唯一参数中心**，不允许在其他模块硬编码参数。

## ⚠️ 已证伪的结论 —— 别再往这个方向建议

**"质量 / 估值 / 安全"三因子选股没有正向价值。** 三次独立印证（2026-09-25）：

- k 值扫描（4 档 × 3 资金）：因子反向 / 接近无效
- 持有期间表现：平均超额 **-0.93%**，跑输市场
- 混合 vs 纯ETF（同区间同配置）：加股票 → 年化 ↓0.6~1.5pp、**夏普 ↓0.09~0.21**

→ **系统价值几乎 100% 来自 ETF（资产配置），不来自选股。**
→ 用户面前三条路：① 定在纯 ETF ② 换因子体系（动量/低波/行业轮动）重新验证 ③ 拆三个因子单独测。
→ **用户尚未拍板。别替他决定，也别默认推荐"继续改进三因子"。**

## 关键口径（代码里看不出来，弄错会算错数）

- **CSMAR 分红口径**：`FI_T11` 里「12月31日」那条 = **该会计年度累计（已含中期）**。
  取年度条；没有年度条才取中期条；**绝不可两条相加**。（三例联网核查，详见 `csmar_loader.py` 顶部注释）
- **PIT 财报滞后**：年报→次年 5/1、中报→当年 8/31、一季报→4/20、三季报→10/20。
  只影响财务类因子，价格类因子不受影响。见 `config.REPORT_LAG_RULES`，
  实现在 `data_loader.filter_financials_pit()`。
- **调仓语义 = 重筛**：触发时用【当时 ≤D 的数据】重跑全流程（防雷 → 打分 → Top N → 优化），
  得到**全新目标权重**，**不是**调回旧目标。重筛失败时兜底沿用旧目标（不会因报错而不调仓）。
- **ETF 候选池保留全部 8 只**，不做数量截断 —— 用户明确要求，**不准改成前五**。
- 个股取 Top `config.STOCK_TOP_N`（15）。**ETF 与个股各自打分后拼接**，不放在一起比大小
  （避免一类系统性挤出另一类）。

## 数据来源

| 用途 | 来源 |
|---|---|
| 股票 25 年日行情 + PIT 财报/ST/退市/申万 | CSMAR 原始 xlsx → `csmar_loader` 缓存为 parquet |
| CSMAR 原始 xlsx 所在目录 | `C:\Users\鲍彦君\Desktop\csmar_data`（`config.CSMAR_DATA_DIR`）|
| ETF 行情与分红 | tushare `fund_daily`（磁盘缓存）|
| 实盘个股基本面 | tushare（`fundamentals.py`）|
| 实盘行情 | akshare（`data_loader.py`）|

环境变量（`.env`，已 gitignore）：`TUSHARE_TOKEN`、`EMAIL_AUTH_CODE`。
**CSMAR 数据不会自动更新 —— 需用户定期重新下载才会新鲜。**

## 常用命令

全部在项目目录下执行：

```bash
start_site.bat          # 起网站 http://localhost:8501
stop_site.bat           # 停网站

.venv\Scripts\python.exe backtest_mixed.py --etf-only      # 纯ETF基准
.venv\Scripts\python.exe backtest_mixed.py                 # 混合 ETF+个股
.venv\Scripts\python.exe backtest_stock.py                 # 纯股票段
.venv\Scripts\python.exe backtest_k_sweep.py               # 因子 k 值扫描
.venv\Scripts\python.exe backtest_diagnostics.py --quick   # ETF 滚动窗口诊断
.venv\Scripts\python.exe scheduler_check.py                # 定时调仓检查（交易日 15:30）
```

通用回测参数：`--warm-start --start --end --refresh --caps`；
混合回测另有 `--alpha-k`（因子配权系数）、`--etf-only`（纯ETF基准）。

## 模块地图

| 文件 | 职责 |
|---|---|
| `config.py` | 参数中心（资金、标的池、防雷阈值、因子权重、调仓规则、路径）|
| `data_loader.py` | akshare 实盘行情 + 实盘防雷 + `filter_financials_pit` |
| `csmar_loader.py` | CSMAR xlsx → parquet 缓存，全 PIT 查询 |
| `csmar_screen.py` | 全市场 9 条防雷筛选（`screen_market` / `build_profiles`），供扩池与回测 |
| `fundamentals.py` | tushare 基本面快照（`build_stock_profiles`）|
| `factor_engine.py` | 三因子 + OLS 行业市值中性化 → `alpha_score` |
| `optimizer.py` | 协方差估计 → Max Sharpe / HRP 熔断 → 单手门槛；`ALPHA_K` 把打分叠进预期收益 |
| `rebalancer.py` | 调仓触发判定与交易指令生成 |
| `portfolio_manager.py` | 多组合容器（`.cache/portfolios/<id>.json`）|
| `notifier.py` | 邮件 / webhook 通知 |
| `scheduler_check.py` | 定时检查（不依赖 Streamlit，供 cron / 任务计划调用）|
| `backtest_engine.py` | 纯 ETF 段回测 |
| `backtest_stock.py` | 纯股票段回测（CSMAR 数据）|
| `backtest_mixed.py` | 混合回测（ETF 受上市时间限制，正式期最早 2017）|
| `backtest_diagnostics.py` | ETF 滚动窗口 / 极端下跌段 / 成本敏感性 |
| `backtest_k_sweep.py` | `ALPHA_K` 4 档 × 3 资金扫描 |
| `backtest_compare.py` | 建仓后"预测 vs 实际"净值曲线对比 |
| `app.py` | Streamlit 前端（6 个区块）|

## 目录与状态

- `.cache/` —— 运行状态与缓存：组合 json、CSMAR parquet、候选池 pkl、日志、回测报告。**不入库。**
  ⚠️ 删 `.cache/portfolio_state.json` 或 `.cache/portfolios/` 会**丢组合状态**。
- `.env` —— 密钥，已 gitignore。
- `.gitignore` 已挡掉 `.env` / `.venv` / `*.parquet` / `*.pkl` / `__pycache__`。
