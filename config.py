# -*- coding: utf-8 -*-
"""
config.py
全局配置参数中心。所有模块只读此文件，不允许在其他模块里硬编码参数。
"""
import os
from pathlib import Path

# ============ 1. 资金与模式 ============
TOTAL_CAPITAL = 30000  # 账户总资金（元）。仅支持场内交易品种（A股/场内ETF），不涉及场外基金申赎。

INVESTMENT_MODE_OPTIONS = ["etf_only", "mixed", "stock_only"]  # 建组合时用户直接选，不跟资金量挂钩、不给默认建议
MODE_LABELS = {"etf_only": "纯ETF", "mixed": "ETF+个股混合", "stock_only": "纯股票"}

LOT_SIZE = 100  # A股/ETF 最小交易单位：1手 = 100股

# ============ 2. 标的池 ============
# 小资金模式：纯宽基/行业ETF池（示例，可自行增减，代码为场内ETF代码）
ETF_POOL = [
    "510300",  # 沪深300ETF
    "510500",  # 中证500ETF
    "588000",  # 科创50ETF
    "512100",  # 中证1000ETF
    "159915",  # 创业板ETF
    "510880",  # 红利ETF
    "511010",  # 国债ETF（现金替代/防守仓）
    "518880",  # 黄金ETF（对冲）
]

# ETF 中文简称（页面展示用，避免只看代码太累）
ETF_NAMES = {
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "588000": "科创50ETF",
    "512100": "中证1000ETF",
    "159915": "创业板ETF",
    "510880": "红利ETF",
    "511010": "国债ETF",
    "518880": "黄金ETF",
}

# 混合模式个股自选池（会经过防雷网 + 财务排雷 + 基本面打分）
STOCK_POOL = [    # --- 原有 10 只 ---
    "600519", "000858", "601318", "600036", "000333",
    "300750", "601888", "600900", "000651", "600030",
    # --- 新增 15 只（跨行业，便于行业中性化）---
    "000001",  # 平安银行(银行)
    "601166",  # 兴业银行(银行)
    "600276",  # 恒瑞医药(医药)
    "300760",  # 迈瑞医疗(医药)
    "603288",  # 海天味业(食品饮料)
    "600887",  # 伊利股份(食品饮料)
    "600809",  # 山西汾酒(白酒)
    "000568",  # 泸州老窖(白酒)
    "002415",  # 海康威视(电子)
    "600309",  # 万华化学(化工)
    "600585",  # 海螺水泥(建材)
    "002594",  # 比亚迪(汽车)
    "601012",  # 隆基绿能(电力设备)
    "002714",  # 牧原股份(农林牧渔)
    "600690",  # 海尔智家(家电)
]

# ============ 3. 防雷红线 ============
MAX_DEBT_RATIO = 0.80          # 资产负债率上限（非金融类适用）
MIN_AVG_TURNOVER_20D = 20_000_000  # 近20日日均成交额下限（元）
EXCLUDE_ST = True
EXCLUDE_SUSPENDED = True       # 剔除停牌标的
FINANCIAL_SW_L1 = ["银行", "非银金融"]  # 申万一级行业，命中则跳过负债率检查

# 次新股：上市不满该天数剔除
EXCLUDE_NEW_STOCK_DAYS = 365
# 财务恶化排雷
PE_TTM_MIN = 0.0          # PE-TTM <= 0（亏损/异常）剔除
PE_TTM_MAX = 100.0        # PE-TTM > 100（估值离谱）剔除
ROE_AVG3Y_MIN = 0.0       # 近3年平均ROE < 0 剔除
GROSS_MARGIN_MIN = 0.05   # 最新销售毛利率 < 5% 剔除
OCF_NI_MIN_RATIO = 0.7    # 近3年年报(经营现金流净额/净利润)均值 < 0.7 视为现金流背离剔除

# ============ 4. 财报 point-in-time 滞后处理（规避 look-ahead bias）============
# 采用保守滞后规则：不做逐条公告日期库匹配，而是按监管披露截止日强制滞后。
REPORT_LAG_RULES = {
    "annual": {"period_end_month": 12, "usable_after": "next_year-05-01"},   # 年报最晚4/30披露，5/1起可用
    "semi":   {"period_end_month": 6,  "usable_after": "same_year-08-31"},   # 中报最晚8/31披露
    "q1":     {"period_end_month": 3,  "usable_after": "same_year-04-20"},   # 一季报最晚4/20左右
    "q3":     {"period_end_month": 9,  "usable_after": "same_year-10-20"},   # 三季报最晚10/20左右
}

# ============ 5. 因子引擎 ============
# 个股：纯基本面三因子（原技术面质量/动量/低波不再用于个股打分）
FACTOR_WEIGHTS_STOCK = {
    "quality": 0.40,    # 质量：近3年平均ROE + 最新销售毛利率
    "valuation": 0.40,  # 估值：PE-TTM历史分位 + 股息率-10年期国债利差
    "safety": 0.20,     # 安全：资产负债率（越低越好）
}
FACTOR_WEIGHTS_ETF = {
    "trend": 0.45,     # 趋势强度（RSRS，ETF继续使用）
    "volume": 0.25,    # 量能活跃度
    "dedup": 0.30,     # 与已选标的的相关性去重惩罚（负向因子）
}
# MAD 去极值倍数。
# 2026-09-19 由 3.0 放宽到 5.0：实测 3×MAD 对当前候选池分布偏严，会裁平 343 只（13.7%），
# 且被裁的恰是"高ROE+高股息+低估值"的优质股（ROE中位11.6% vs 未裁组7.9%），
# 导致估值因子在这批股票上失去区分度（Top15 出现 11 只并列）。5×MAD 预计只裁 ~5%。
MAD_THRESHOLD = 5.0

# 候选池分资产：个股单独打分取 Top（10-15）。ETF 候选池保留全部池内标的，不做数量截断。
STOCK_TOP_N = 15

# 估值因子辅助参数
YIELD_10Y_CN = 0.022    # 10年期国债收益率（约值，可手动更新）
PE_PERCENTILE_LOOKBACK_DAYS = 730  # PE-TTM 历史分位回看窗口（约2年）

# ============ 6. 组合优化 ============
WEEKLY_LOOKBACK_WEEKS = 104  # 用于协方差估计的周线回看窗口（约2年）
EWMA_HALFLIFE_WEEKS = 26     # EWMA 协方差半衰期（约半年）
MAX_WEIGHT_PER_ASSET = 0.25
MIN_WEIGHT_PER_ASSET = 0.0
RISK_FREE_RATE = 0.02

# 集中度熔断：若 Max Sharpe 解中触及权重上限的资产数 >= 该值，自动切换 HRP
CONCENTRATION_BREAKER_COUNT = 2

# 因子打分对预期收益的缩放系数（2026-09-19 新增）：
# 优化器原只用历史价格估预期收益，完全不看因子打分 → 选股逻辑形同虚设。
# 现在把因子打分标准化后叠加进预期收益：mu' = mu_历史 + ALPHA_K * z(alpha_score)
# k=0.05 表示"打分每高1个标准差，预期年化收益上调5个百分点"（股票量级可比）。
ALPHA_K = 0.05

# ============ 7. 调仓规则 ============
REBALANCE_DRIFT_THRESHOLD = 0.05      # 常规触发阈值：偏离度 > 5%
REBALANCE_URGENT_THRESHOLD = 0.10     # 紧急触发阈值：偏离度 > 10%，忽略天数限制
REBALANCE_MIN_INTERVAL_DAYS = 30
MIN_TRADE_AMOUNT = 1000  # 低于此金额的调仓指令自动忽略（防佣金损耗）

# ============ 8. 路径 ============
PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_ROOT / ".cache"
STATE_FILE = PROJECT_ROOT / ".cache" / "portfolio_state.json"  # 记录上次调仓日期、当前持仓
CACHE_DIR.mkdir(exist_ok=True)

# ============ 8.1 CSMAR 数据 ============
# 用户从 CSMAR/CNRDS 下载的原始 xlsx 存放目录（含 A股退市日期/A股股利分配/A股特殊处理股票 三个子目录）。
# 可通过 CSMAR_DATA_DIR 环境变量覆盖；首次读取会解析 xlsx 并转成 parquet 缓存到 CACHE_DIR/csmar/。
CSMAR_DATA_DIR = Path(os.environ.get("CSMAR_DATA_DIR", PROJECT_ROOT / "data" / "csmar_data"))
CSMAR_CACHE_DIR = CACHE_DIR / "csmar"

# ============ 9. 通知提醒 ============
# 定时检查脚本(scheduler_check.py)触发调仓时会走这里配置的渠道发通知。
NOTIFY_METHOD = "email"  # 可选: "email" / "webhook" / "none"


def _env(key: str, default: str = "") -> str:
    """从环境变量或项目根 .env 读取配置项（不依赖 python-dotenv，与 data_loader 同一约定）。
    敏感信息（邮箱授权码等）只放 .env，不写死进代码。"""
    val = os.environ.get(key)
    if val is None:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return (val or default).strip()


# 以下敏感信息一律从 .env 读取（见项目根 .env）；
# 缺失时 notifier 会以 error 级别报出，而不是静默跳过。
EMAIL_CONFIG = {
    "smtp_server": _env("EMAIL_SMTP_SERVER", "smtp.qq.com"),  # 按邮箱服务商改，QQ邮箱示例
    "smtp_port": int(_env("EMAIL_SMTP_PORT", "465") or "465"),
    "sender": _env("EMAIL_SENDER"),              # 发件邮箱
    "sender_auth_code": _env("EMAIL_AUTH_CODE"),  # SMTP授权码（不是登录密码）
    "receiver": _env("EMAIL_RECEIVER"),          # 接收提醒的邮箱（可同发件邮箱）
}

WEBHOOK_CONFIG = {
    "url": _env("WEBHOOK_URL"),  # 例如 Server酱 / PushPlus / 企业微信机器人 的 webhook 地址
}

# ============ 10. 免责声明 ============
DISCLAIMER = "本系统仅为个人学习与投资决策辅助工具，所有输出不构成任何投资建议，实盘交易风险自负。"
