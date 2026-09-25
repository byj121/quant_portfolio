# quant_portfolio

自适应量化资产配置与交易辅助系统。**仅支持场内交易品种（A股个股 / 场内ETF），不涉及场外基金申赎。**

> ⚠️ 本系统仅为个人学习与投资决策辅助工具，所有输出不构成任何投资建议，实盘交易风险自负。

支持**多组合并行**：每个组合有自己的资金量、投资模式、持仓与调仓状态，互不污染。

---

## ⚠️ 重要结论（2026-09 回测得出，先看这个）

**当前的"质量 / 估值 / 安全"三因子选股没有正向价值。** 三次独立印证：

- 因子影响系数 `ALPHA_K` 扫描（4 档 × 3 资金）：因子反向 / 接近无效
- 持有期间表现：平均超额 **-0.93%**，跑输市场
- 混合 vs 纯ETF（同区间同配置）：加入个股后年化 **↓0.6~1.5pp**、夏普 **↓0.09~0.21**，
  回撤只改善 2~7pp —— 多承担的风险没换来相应收益

→ **系统价值几乎全部来自 ETF（资产配置），不来自选股。**
→ 若继续投入，方向应是：换因子体系（动量/低波/行业轮动）重新验证，或拆解三个因子单独测。

---

## 一、核心能力

| 模块 | 文件 | 说明 |
|---|---|---|
| 全局配置 | `config.py` | 参数中心：资金、标的池、防雷阈值、因子权重、优化约束、调仓规则。其他模块只读，不允许硬编码 |
| 实时行情与防雷 | `data_loader.py` | akshare 抓行情；实盘防雷（ST / 高负债 / 低流动性 / 停牌）；财报 point-in-time 滞后处理 |
| CSMAR 离线数据 | `csmar_loader.py` | 读取退市名单 / 股利分配 / ST 变更历史三类原始 xlsx，转 parquet 缓存，全部支持 PIT 查询 |
| 全市场防雷筛选 | `csmar_screen.py` | 读 CSMAR 数据一次筛全市场（9 条红线），供**扩大股票池**与**回测**使用 |
| 个股基本面 | `fundamentals.py` | tushare 取财务/估值/申万行业，产出打分用的基本面快照 |
| 因子引擎 | `factor_engine.py` | 个股三因子（质量40 / 估值40 / 安全20）+ OLS 行业市值中性化；ETF 三因子（趋势 / 量能 / 相关性去重）|
| 组合优化 | `optimizer.py` | 周线 EWMA + Ledoit-Wolf 协方差 → Max Sharpe（默认）；触上限资产 ≥2 只时熔断切 HRP；单手门槛校验；因子打分以 `ALPHA_K` 叠加进预期收益 |
| 调仓引擎 | `rebalancer.py` | 双重触发（偏离 >5% 且距上次 ≥30天 / >10% 紧急）、微量拦截（<1000元忽略）、增量资金优先 |
| 多组合管理 | `portfolio_manager.py` | 组合的增删查改，每个组合一份独立 json 存档 |
| 回测引擎 | `backtest_*.py` | 纯ETF / 纯股票 / 混合 三套 + 滚动窗口诊断 + 因子 k 值扫描 |
| 通知 | `notifier.py` | 邮件 / webhook |
| 定时检查 | `scheduler_check.py` | 不依赖 Streamlit，供 cron / Windows 任务计划程序调用 |
| 交互控制台 | `app.py` | Streamlit 前端，见「五、使用流程」 |

## 二、投资模式

建组合时由用户**直接选**模式，**不与资金量挂钩、不给默认建议**（`config.INVESTMENT_MODE_OPTIONS`）：

| 模式 | 含义 | 标的池 |
|---|---|---|
| `etf_only` | 纯 ETF | 8 只宽基 / 行业 / 防守 ETF，**全部保留**，不做数量截断 |
| `mixed` | ETF + 个股 | 8 只 ETF + 个股 Top15（两者各自打分后拼接，不放在一起比大小）|
| `stock_only` | 纯股票 | 全市场防雷筛选后的个股 Top15 |

单资产分配金额不足该标的 1 手（100 股）所需金额时自动剔除，权重等比例重新分摊给剩余标的，
整手向下取整，零头留作现金。

## 三、关于 look-ahead bias 的处理说明（重要）

财报的**报告期**与**实际公布日**之间存在天然滞后（年报最晚可拖到次年 4/30 披露）。
如果因子计算时直接使用报告期数据而不做处理，相当于让模型"提前看到"了未公布的信息，
回测夏普比率会显著虚高，这是学术界公认的常见回测陷阱。

本系统采用**保守滞后规则**（非逐条公告日期库匹配，兼顾工程量与可信度）：

| 报告类型 | 报告期结束 | 最早可用日期 |
|---|---|---|
| 年报 | 12月31日 | 次年 5月1日 |
| 中报 | 6月30日 | 当年 8月31日 |
| 一季报 | 3月31日 | 当年 4月20日 |
| 三季报 | 9月30日 | 当年 10月20日 |

规则定义见 `config.REPORT_LAG_RULES`，实现见 `data_loader.filter_financials_pit()`。
**该处理仅影响财务类因子，对纯价格类因子（RSRS / 趋势 / 量能）无影响。**

## 四、回测

三套引擎，共用「**触发即重筛**」语义：触发调仓时用【当时 ≤D 的数据】重跑
防雷 → 打分 → Top N → 优化 → 整手，得到全新目标权重，**而不是**调回旧目标。

| 脚本 | 区间 | 说明 |
|---|---|---|
| `backtest_engine.py` | `--years` 可配（默认 3 年）| 纯 ETF 段 |
| `backtest_stock.py` | 预热 2001-2002，正式 2003- 至今 | 纯股票段，CSMAR 数据 |
| `backtest_mixed.py` | 预热 2015-2016，正式 2017- 至今 | 混合模式（ETF 上市时间受限，2017 已是最早可行起点）|
| `backtest_diagnostics.py` | — | ETF 滚动窗口回测 / 极端下跌段测试 / 成本敏感性 |
| `backtest_k_sweep.py` | — | 因子影响系数 `ALPHA_K` 的 4 档 × 3 资金扫描 |
| `backtest_compare.py` | — | 建仓后「预测 vs 实际」净值曲线对比 |

**真实成本口径**：ETF 佣金万 0.5；股票佣金万 0.9（0.5 元起）、过户费万 0.1（双向）、
印花税万 5（仅卖出）、滑点 0.05%。

**防幸存者偏差**：CSMAR 数据含已退市股票；选股与交易只在「该标的当日已有价格数据」时进行，
天然排除尚未上市的标的。

## 五、环境安装

> ⚠️ **本项目所有脚本必须用项目自带的虚拟环境运行**，系统 Python 未安装 `pypfopt` 会直接报错。

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

依赖见 `requirements.txt`（akshare / pandas / numpy / statsmodels / PyPortfolioOpt /
streamlit / quantstats / plotly / tushare）。

`.env` 中配置密钥（该文件已 gitignore）：
```
TUSHARE_TOKEN=你的tushare token
EMAIL_AUTH_CODE=邮箱SMTP授权码
```

## 六、运行

```bash
start_site.bat        # 启动网站（内部用 .venv 的 python 跑 streamlit，端口 8501）
stop_site.bat         # 停止网站
```

或手动：`.venv\Scripts\python.exe -m streamlit run app.py`
浏览器打开 `http://localhost:8501`。

## 七、使用流程

1. 侧边栏**新建组合**：填名称、资金量，**自选投资模式**（纯ETF / 混合 / 纯股票）
2. 点击生成候选池 → **①** 候选池表格：勾选剔除不想要的标的，或手动输入代码强制加入
3. **②** 求解最优组合权重（自动 Max Sharpe / HRP 熔断切换）+ 单手门槛校验
4. **③** 查看一句话下单指南，按它在券商 App 手动下单
   （本系统不接实盘交易接口，只做决策辅助）
5. **④** 组合历史回测诊断（QuantStats Tear Sheet）
6. **⑤** 调仓触发检测　**⑥** 预测 vs 实际净值对比

定时自动调仓检查：把 `scheduler_check.py` 挂到 cron / Windows 任务计划程序，
每个交易日 15:30 收盘后跑一次，触发时按 `config.NOTIFY_METHOD` 发通知。

## 八、已知限制 / TODO

- **CSMAR 数据不会自动更新**，需定期从 CSMAR/CNRDS 重新下载才会新鲜
- 停牌判定为简化逻辑（当日成交量 = 0），非严格停牌公告匹配
- ETF 与股票的分红口径不一致：ETF 是除息日现金入账，股票用含红利再投资的收益率 —— 影响轻微但不严谨
- 实盘调仓未传因子影响系数（回测会传）；鉴于因子已证伪，实盘不传反而是对的
- 候选池「看年报」直达链接、按行业区别对待筛选 —— 用户已提出，尚未实施

## 九、目录结构

```
quant_portfolio/
├── config.py              # 全局配置参数中心
├── data_loader.py         # 实盘行情 + 防雷网 + PIT 处理
├── csmar_loader.py        # CSMAR 原始数据读取与 PIT 查询
├── csmar_screen.py        # 全市场 9 条防雷筛选（扩池 / 回测用）
├── fundamentals.py        # tushare 个股基本面快照
├── factor_engine.py       # 三因子 + 行业市值中性化打分
├── optimizer.py           # 组合优化求解器（Max Sharpe / HRP）
├── rebalancer.py          # 调仓决策引擎
├── portfolio_manager.py   # 多组合管理
├── notifier.py            # 通知渠道（邮件 / webhook）
├── scheduler_check.py     # 定时自动调仓检查
├── backtest_engine.py     # 纯 ETF 段回测
├── backtest_stock.py      # 纯股票段回测
├── backtest_mixed.py      # 混合模式回测
├── backtest_diagnostics.py# 回测诊断（滚动窗口 / 极端段 / 成本）
├── backtest_k_sweep.py    # 因子 k 值扫描
├── backtest_compare.py    # 预测 vs 实际净值对比
├── app.py                 # Streamlit 前端
├── start_site.bat / stop_site.bat
├── CLAUDE.md              # 给 AI 协作的工作手册
├── .env                   # 密钥（gitignored）
├── .cache/                # 运行状态与缓存（gitignored）
├── .gitignore
└── README.md
```
