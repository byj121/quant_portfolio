# quant_portfolio

自适应量化资产配置与交易辅助系统。**仅支持场内交易品种（A股个股 / 场内ETF），不涉及场外基金申赎。**

> ⚠️ 本系统仅为个人学习与投资决策辅助工具，所有输出不构成任何投资建议，实盘交易风险自负。

---

## 一、核心能力

| 模块 | 文件 | 说明 |
|---|---|---|
| 全局配置 | `config.py` | 资金量、标的池、防雷阈值、因子权重、优化约束、调仓规则统一管理 |
| 数据与防雷 | `data_loader.py` | akshare 抓取行情/财务数据；ST/高负债/低流动性三道红线；停牌过滤；财报 point-in-time 滞后处理 |
| 因子引擎 | `factor_engine.py` | 个股：质量(ROIC/FCF)+动量(截面Sharpe/RSRS)+低波动；ETF：趋势+量能+相关性去重；MAD去极值+Z-Score+OLS行业市值中性化 |
| 组合优化 | `optimizer.py` | 周线EWMA/Ledoit-Wolf协方差 + Max Sharpe（默认），触发集中度熔断（≥2只吃满25%上限）自动切换 HRP；单手门槛校验与整手取整 |
| 调仓引擎 | `rebalancer.py` | 双重触发（5%+30天 / 10%紧急）、微量拦截(<1000元忽略)、增量资金优先填缺口 |
| 交互控制台 | `app.py` | Streamlit 前端：候选池黑白名单勾选、权重饼图、相关性热力图、一句话下单指南、QuantStats Tear Sheet |

## 二、资金量自适应逻辑

- `TOTAL_CAPITAL < 50000`：**小资金模式**，锁定纯 ETF 池，持仓 4-5 只
- `TOTAL_CAPITAL >= 50000`：**混合模式**，解锁 ETF + A股蓝筹个股，Top15 候选池

单资产分配金额 < 该标的1手(100股)所需金额时自动剔除，权重等比例重新分摊给剩余标的，整手向下取整，零头留作现金。

## 三、关于 look-ahead bias 的处理说明（重要）

财报的**报告期**与**实际公布日**之间存在天然滞后（年报最晚可拖到次年4/30披露）。如果因子计算时直接使用报告期数据而不做处理，相当于让模型"提前看到"了未公布的信息，回测夏普比率会显著虚高，这是学术界公认的常见回测陷阱。

本系统采用**保守滞后规则**（非逐条公告日期库匹配，兼顾工程量与可信度）：

| 报告类型 | 报告期结束 | 最早可用日期 |
|---|---|---|
| 年报 | 12月31日 | 次年5月1日 |
| 中报 | 6月30日 | 当年8月31日 |
| 一季报 | 3月31日 | 当年4月20日 |
| 三季报 | 9月30日 | 当年10月20日 |

规则定义见 `config.REPORT_LAG_RULES`，实现见 `data_loader.filter_financials_pit()`。**该处理仅影响财务类因子（quality），对纯价格类因子（momentum/RSRS/low_vol/trend）无影响。**

## 四、环境安装

```bash
# 建议使用虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

`requirements.txt`（如未生成，可手动创建）：

```
akshare>=1.14.0
pandas>=2.0
numpy>=1.24
statsmodels>=0.14
PyPortfolioOpt>=1.5.5
streamlit>=1.35
quantstats>=0.0.62
plotly>=5.20
tushare>=1.4.0
```

## 五、运行

```bash
streamlit run app.py
```

浏览器自动打开 `http://localhost:8501`。

## 六、使用流程

1. 侧边栏设置总资金 → 点击"拉取数据 & 生成候选池"（需要外网访问行情接口）
2. 在候选池表格中勾选剔除不想要的标的，或在自选框手动输入代码强制加入
3. 点击"求解最优组合权重"，系统自动做 Max Sharpe / HRP 熔断切换
4. 查看权重饼图、相关性热力图、单手校验结果
5. 按"一句话下单指南"手动在券商 App 下单（本系统不接入实盘交易接口，只做决策辅助）
6. 生成 QuantStats Tear Sheet 查看历史回测诊断

## 七、已知限制 / TODO

- `financial_data` 财务因子数据源需自行接入财务接口（如 akshare `stock_financial_abstract` / Tushare），并整理为含 `period_end`/`report_type` 的标准格式后传入 `filter_financials_pit`
- 停牌判定为简化逻辑（当日成交量=0），非严格停牌公告匹配
- 持仓状态由 `rebalancer.load_state()` 维护（存于 `.cache/portfolio_state.json`），首次运行视为从0建仓，实盘需按实际成交回写状态
- 申万行业分类数据依赖数据接口稳定性，接口变更需同步维护
- 定时自动检查见 `scheduler_check.py`（可挂 cron / Windows 任务计划程序），通知渠道见 `config.NOTIFY_METHOD`（email / webhook / none）

## 八、目录结构

```
quant_portfolio/
├── config.py          # 全局配置
├── data_loader.py      # 数据获取 + 防雷网 + PIT处理
├── factor_engine.py    # 多因子中性化打分
├── optimizer.py         # 组合优化求解器
├── rebalancer.py        # 调仓决策引擎
├── notifier.py          # 通知渠道（邮件/webhook）
├── scheduler_check.py   # 定时自动调仓检查
├── app.py               # Streamlit 前端
├── .env                 # Tushare token（gitignored）
├── .gitignore
└── README.md
```
