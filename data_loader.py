# -*- coding: utf-8 -*-
"""
data_loader.py
职责：
1. 用 akshare 抓取 A股 / 场内ETF 的行情与财务数据
2. 执行三道防雷红线过滤（ST / 高负债 / 低流动性）+ 停牌过滤
3. 对财务数据做 point-in-time 滞后处理，规避 look-ahead bias

注意：akshare 依赖网络，若当前环境无外网访问，函数会抛出异常，
请在有网络的本地/服务器环境中运行。
"""
from __future__ import annotations
import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Optional

import pandas as pd

try:
    import akshare as ak
except ImportError:
    ak = None

try:
    import tushare as ts
except ImportError:
    ts = None

import config

logger = logging.getLogger("data_loader")
logging.basicConfig(level=logging.INFO)


def _require_akshare():
    if ak is None:
        raise ImportError("未安装 akshare，请先执行: pip install akshare --upgrade")


# ---------------------------------------------------------------------------
# 1. 基础行情抓取
# ---------------------------------------------------------------------------

def _to_prefix_code(code: str) -> str:
    """给 6 位代码加市场前缀，适配新浪/腾讯接口的 symbol 格式"""
    if code.startswith("920"):          # 北交所新代码段（920xxx）
        return "bj" + code
    if code.startswith("9"):            # 900xxx = 沪B
        return "sh" + code
    if code.startswith(("6", "5")):
        return "sh" + code
    if code.startswith(("0", "1", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return code


def _finalize_daily(df: pd.DataFrame) -> pd.DataFrame:
    """统一输出契约：DatetimeIndex(date)，升序，列 [date,open,high,low,close,volume,amount]"""
    cols = [c for c in ["date", "open", "high", "low", "close", "volume", "amount"]
            if c in df.columns]
    df = df[cols].copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def _fetch_stock_daily_tx(code: str, start: str, end: str) -> pd.DataFrame:
    """腾讯个股日线兜底（东财失败时自动切换）"""
    df = ak.stock_zh_a_hist_tx(symbol=_to_prefix_code(code), start_date=start, end_date=end)
    if df is None or df.empty:
        return pd.DataFrame()
    return _finalize_daily(df)


def _fetch_etf_daily_sina(code: str, start: str, end: str) -> pd.DataFrame:
    """新浪场内ETF日线兜底（东财失败时自动切换）。新浪返回全量历史，按起止日期截取。"""
    df = ak.fund_etf_hist_sina(symbol=_to_prefix_code(code))
    if df is None or df.empty:
        return pd.DataFrame()
    df = _finalize_daily(df)
    start_dt, end_dt = pd.to_datetime(start), pd.to_datetime(end)
    return df.loc[start_dt:end_dt]


# ---------------------------------------------------------------------------
# Tushare Pro 数据源（主源）：token 从环境变量或 .env 读取，无需网络爬虫
# ---------------------------------------------------------------------------
_TUSHARE_TOKEN: Optional[str] = None


def _load_tushare_token() -> Optional[str]:
    """读取 Tushare token（环境变量 TUSHARE_TOKEN 优先，其次项目根 .env）"""
    global _TUSHARE_TOKEN
    if _TUSHARE_TOKEN is not None:
        return _TUSHARE_TOKEN or None
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        env_path = config.PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TUSHARE_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    _TUSHARE_TOKEN = token
    return _TUSHARE_TOKEN or None


def _tushare_ready() -> bool:
    return ts is not None and _load_tushare_token() is not None


def _to_ts_code(code: str) -> str:
    """6 位代码 -> Tushare ts_code（600519.SH / 000858.SZ / 430047.BJ / 920208.BJ）"""
    if code.startswith("920"):          # 北交所新代码段（920xxx）
        return code + ".BJ"
    if code.startswith("9"):            # 900xxx = 沪B
        return code + ".SH"
    if code.startswith(("6", "5")):
        return code + ".SH"
    if code.startswith(("0", "3", "1", "2")):
        return code + ".SZ"
    if code.startswith(("4", "8")):
        return code + ".BJ"
    return code + ".SH"


def _normalize_tushare(df: pd.DataFrame) -> pd.DataFrame:
    """tushare 统一单位：vol 单位=手 -> 股(×100)，amount 单位=千元 -> 元(×1000)"""
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    df["volume"] = df["volume"] * 100
    df["amount"] = df["amount"] * 1000
    return _finalize_daily(df)


def _fetch_stock_daily_ts(code: str, start: str, end: str) -> pd.DataFrame:
    """Tushare 个股日线（前复权）"""
    ts.set_token(_load_tushare_token())
    df = ts.pro_bar(ts_code=_to_ts_code(code), adj="qfq", start_date=start, end_date=end)
    if df is None or df.empty:
        return pd.DataFrame()
    return _normalize_tushare(df)


def _fetch_etf_daily_ts(code: str, start: str, end: str) -> pd.DataFrame:
    """Tushare 场内ETF日线（fund_daily，非复权）"""
    ts.set_token(_load_tushare_token())
    df = ts.pro_api().fund_daily(ts_code=_to_ts_code(code), start_date=start, end_date=end)
    if df is None or df.empty:
        return pd.DataFrame()
    return _normalize_tushare(df)


def get_stock_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """获取个股日线（前复权）。数据源优先级：Tushare -> 东财 -> 腾讯。"""
    if _tushare_ready():
        try:
            df = _fetch_stock_daily_ts(code, start, end)
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"[{code}] Tushare 个股日线失败: {e}，兜底东财")
    _require_akshare()
    try:
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start,
                                 end_date=end, adjust="qfq")
        if df is not None and not df.empty:
            return _finalize_daily(df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
            }))
    except Exception as e:
        logger.warning(f"[{code}] 东财个股日线失败: {e}，兜底腾讯 stock_zh_a_hist_tx")
    try:
        return _fetch_stock_daily_tx(code, start, end)
    except Exception as e:
        # akshare 对北交所等代码可能直接抛异常；这里吞掉并返回空，
        # 避免一只标的拉不到数据就把整条调用链（如全市场重筛）拖崩。
        logger.warning(f"[{code}] 腾讯个股日线失败: {e}，返回空")
        return pd.DataFrame()


def get_etf_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """获取场内ETF日线（前复权）。数据源优先级：Tushare -> 东财 -> 新浪。"""
    if _tushare_ready():
        try:
            df = _fetch_etf_daily_ts(code, start, end)
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"[{code}] Tushare ETF日线失败: {e}，兜底东财")
    _require_akshare()
    try:
        df = ak.fund_etf_hist_em(symbol=code, period="daily", start_date=start,
                                  end_date=end, adjust="qfq")
        if df is not None and not df.empty:
            return _finalize_daily(df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
            }))
    except Exception as e:
        logger.warning(f"[{code}] 东财ETF日线失败: {e}，兜底新浪 fund_etf_hist_sina")
    try:
        return _fetch_etf_daily_sina(code, start, end)
    except Exception as e:
        logger.warning(f"[{code}] 新浪ETF日线失败: {e}，返回空")
        return pd.DataFrame()


def get_etf_daily_strict(code: str, start: str, end: str) -> pd.DataFrame:
    """仅用 tushare fund_daily 拉 ETF 日线，不回退 akshare，失败抛异常。

    供历史回测使用：保证整条历史来自同一数据源（避免静默换源导致序列不一致、
    回测结果不可复现）。"""
    if not _tushare_ready():
        raise RuntimeError("tushare 不可用，无法进行严格数据拉取")
    df = _fetch_etf_daily_ts(code, start, end)
    if df is None or df.empty:
        raise RuntimeError(f"[{code}] tushare fund_daily 在 {start}~{end} 无数据")
    return df


def get_etf_dividends_ts(code: str, start: str, end: str) -> pd.DataFrame:
    """从 tushare fund_adj 复权因子跳变还原 ETF 现金分红（除息日 + 每股现金）。
    原理：交易所基金复权因子在除息日跳升，跳升比例 A_after/A_before = pre_close/(pre_close-每股分红)，
    故 每股分红 = pre_close × (1 - A_before/A_after)。
    返回 DataFrame(index=除息日 DatetimeIndex, columns=['cash_per_share'])；无分红返回空。
    仅 tushare，失败抛异常（回测确定性需要，不静默混源）。"""
    if not _tushare_ready():
        raise RuntimeError("tushare 不可用，无法获取基金分红")
    ts.set_token(_load_tushare_token())
    pro = ts.pro_api()
    af = pro.fund_adj(ts_code=_to_ts_code(code), start_date=start, end_date=end)
    if af is None or af.empty:
        return pd.DataFrame(columns=["cash_per_share"])
    af = af.sort_values("trade_date").reset_index(drop=True)
    af["A"] = af["adj_factor"].astype(float)
    af["ratio"] = af["A"] / af["A"].shift(1)
    jumps = af[af["ratio"] > 1.0005]  # 正常无分红日 ratio≈1.0；除息跳变通常3%~5%
    if jumps.empty:
        return pd.DataFrame(columns=["cash_per_share"])
    fd = pro.fund_daily(ts_code=_to_ts_code(code), start_date=start, end_date=end)
    if fd is None or fd.empty:
        raise RuntimeError(f"[{code}] fund_daily 无数据，无法还原分红")
    pre_map = dict(zip(fd["trade_date"].astype(str), fd["pre_close"].astype(float)))
    rows = []
    for _, r in jumps.iterrows():
        td = str(r["trade_date"])
        pre = pre_map.get(td)
        if not pre:
            continue
        rows.append({"date": td,
                     "cash_per_share": round(pre * (1 - 1 / r["ratio"]), 6)})
    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=["cash_per_share"])
    out["date"] = pd.to_datetime(out["date"])
    return out.set_index("date").sort_index()


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """日线转周线（周五收盘），用于协方差矩阵计算，降低换手噪音"""
    if df.empty:
        return df
    weekly = df.resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum", "amount": "sum",
    }).dropna()
    return weekly


# ---------------------------------------------------------------------------
# 2. 三道防雷红线
# ---------------------------------------------------------------------------

_TUSHARE_LATEST_TRADE_DATE: Optional[str] = None


def _latest_trade_date(pro) -> Optional[str]:
    """最近一个交易日（SSE 交易日历，最多往前回看14天）"""
    global _TUSHARE_LATEST_TRADE_DATE
    if _TUSHARE_LATEST_TRADE_DATE:
        return _TUSHARE_LATEST_TRADE_DATE
    end = dt.date.today()
    start = end - dt.timedelta(days=14)
    cal = pro.trade_cal(exchange="SSE", start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"), is_open="1")
    if cal is None or cal.empty:
        return None
    _TUSHARE_LATEST_TRADE_DATE = str(cal["cal_date"].sort_values().iloc[-1])
    return _TUSHARE_LATEST_TRADE_DATE


def _get_stock_basic_info_ts() -> pd.DataFrame:
    """Tushare 个股基础信息：名称(含ST标记) + 最近交易日成交量"""
    ts.set_token(_load_tushare_token())
    pro = ts.pro_api()
    basic = pro.stock_basic(exchange="", list_status="L",
                            fields="ts_code,symbol,name,industry")
    latest = _latest_trade_date(pro)
    if latest is None or basic is None or basic.empty:
        return pd.DataFrame()
    daily = pro.daily(trade_date=latest, fields="ts_code,vol")
    df = basic.merge(daily, on="ts_code", how="left")
    df = df.rename(columns={"symbol": "code", "vol": "volume"})
    return df[["code", "name", "volume"]]


def get_stock_basic_info() -> pd.DataFrame:
    """拉取全市场个股基础信息（名称含ST标记、最近交易日成交量）。Tushare优先。"""
    if _tushare_ready():
        try:
            df = _get_stock_basic_info_ts()
            if not df.empty:
                return df
        except Exception as e:
            logger.warning(f"Tushare 基础信息失败: {e}，兜底 akshare")
    _require_akshare()
    spot = ak.stock_zh_a_spot_em()  # 含名称、最新价、是否停牌可推断（成交量为0近似）
    spot = spot.rename(columns={"代码": "code", "名称": "name", "成交量": "volume"})
    return spot[["code", "name", "volume"]]


def is_st_or_delisting(name: str) -> bool:
    if not config.EXCLUDE_ST:
        return False
    flags = ["ST", "*ST", "退", "PT"]
    return any(f in name for f in flags)


def is_suspended(volume_today: float) -> bool:
    """成交量为0近似判定当日停牌（简化处理，非严格停牌公告匹配）"""
    if not config.EXCLUDE_SUSPENDED:
        return False
    return volume_today == 0 or pd.isna(volume_today)


_FINANCIAL_KEYWORDS = ("银行", "保险", "证券", "金融")


def _is_financial_industry(industry: Optional[str]) -> bool:
    """金融行业命中则跳过负债率检查。
    兼容两套行业命名：config.FINANCIAL_SW_L1(申万: 银行/非银金融) 与
    tushare stock_basic.industry(证监会: 银行/保险/证券/多元金融)。"""
    if not industry:
        return False
    if industry in config.FINANCIAL_SW_L1:
        return True
    return any(k in industry for k in _FINANCIAL_KEYWORDS)


def _get_debt_industry_ts(code: str) -> tuple[Optional[float], Optional[str]]:
    """Tushare：最新资产负债率(fina_indicator) + 行业分类(stock_basic)"""
    ts.set_token(_load_tushare_token())
    pro = ts.pro_api()
    debt_ratio = None
    try:
        ind = pro.fina_indicator(ts_code=_to_ts_code(code),
                                 fields="ts_code,end_date,debt_to_assets")
        if ind is not None and not ind.empty:
            latest = ind.sort_values("end_date").iloc[-1]
            if pd.notna(latest["debt_to_assets"]):
                debt_ratio = float(latest["debt_to_assets"]) / 100
    except Exception as e:
        logger.warning(f"[{code}] 资产负债率(Tushare)获取失败: {e}")

    industry = None
    try:
        sb = pro.stock_basic(ts_code=_to_ts_code(code), fields="ts_code,industry")
        if sb is not None and not sb.empty:
            industry = sb.iloc[0]["industry"] or None
    except Exception as e:
        logger.warning(f"[{code}] 行业(Tushare)获取失败: {e}")

    return debt_ratio, industry


def get_debt_ratio_and_industry(code: str) -> tuple[Optional[float], Optional[str]]:
    """获取最新资产负债率与行业分类。Tushare优先，akshare兜底。"""
    if _tushare_ready():
        try:
            debt_ratio, industry = _get_debt_industry_ts(code)
            if debt_ratio is not None or industry is not None:
                return debt_ratio, industry
        except Exception as e:
            logger.warning(f"[{code}] Tushare 财务/行业失败: {e}，兜底 akshare")
    _require_akshare()
    try:
        indicator = ak.stock_financial_abstract(symbol=code)
        debt_ratio = None
        if indicator is not None and not indicator.empty:
            row = indicator[indicator["指标"].str.contains("资产负债率", na=False)]
            if not row.empty:
                debt_ratio = float(str(row.iloc[0, -1]).replace("%", "")) / 100
    except Exception as e:
        logger.warning(f"[{code}] 资产负债率获取失败: {e}")
        debt_ratio = None

    try:
        sw = ak.stock_individual_info_em(symbol=code)
        industry = None
        if sw is not None and not sw.empty:
            row = sw[sw["item"] == "行业"]
            if not row.empty:
                industry = row.iloc[0]["value"]
    except Exception as e:
        logger.warning(f"[{code}] 行业信息获取失败: {e}")
        industry = None

    return debt_ratio, industry


def get_avg_turnover_20d(code: str, is_etf: bool = False) -> float:
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=45)).strftime("%Y%m%d")
    df = get_etf_daily(code, start, end) if is_etf else get_stock_daily(code, start, end)
    if df.empty or "amount" not in df.columns:
        return 0.0
    return float(df["amount"].tail(20).mean())


@dataclass
class RiskCheckResult:
    code: str
    passed: bool
    reason: str = ""


def risk_screen_stock(code: str, name: str, volume_today: float,
                      profile: dict = None) -> RiskCheckResult:
    """对单只个股执行防雷网：ST/退市 + 停牌 + 次新 + 财务恶化排雷 + 负债率 + 流动性。

    profile: fundamentals.build_stock_profiles() 返回的单只基本面快照。
             给出后直接用它做次新/财务排雷/负债率判定，避免逐个重复抓取。
    """
    if is_st_or_delisting(name):
        return RiskCheckResult(code, False, "ST/退市风险")
    if is_suspended(volume_today):
        return RiskCheckResult(code, False, "疑似停牌")

    if profile is not None:
        days_listed = profile.get("days_listed")
        if days_listed is not None and days_listed < config.EXCLUDE_NEW_STOCK_DAYS:
            return RiskCheckResult(code, False, f"次新股(上市仅{days_listed}天)")
        pe = profile.get("pe_ttm")
        if pe is not None and (pe <= config.PE_TTM_MIN or pe > config.PE_TTM_MAX):
            return RiskCheckResult(code, False, f"PE-TTM={pe:.1f} 不在(0,100]区间，剔除")
        roe3 = profile.get("roe_avg3")
        if roe3 is not None and roe3 < config.ROE_AVG3Y_MIN:
            return RiskCheckResult(code, False, f"近3年平均ROE={roe3:.2%}<0，剔除")
        gm = profile.get("gross_margin")
        if gm is not None and gm < config.GROSS_MARGIN_MIN:
            return RiskCheckResult(code, False, f"最新毛利率{gm:.1%}<5%，剔除")
        ocf = profile.get("ocf_ni_ratio")
        if ocf is not None and ocf < config.OCF_NI_MIN_RATIO:
            return RiskCheckResult(code, False,
                                   f"现金流背离(近3年经营现金流/净利均值{ocf:.2f}<0.7)，剔除")
        industry = profile.get("industry")
        debt_ratio = profile.get("debt_ratio")
    else:
        # 兼容旧路径：无快照时逐个抓
        debt_ratio, industry = get_debt_ratio_and_industry(code)

    is_financial = _is_financial_industry(industry)
    if not is_financial and debt_ratio is not None and debt_ratio > config.MAX_DEBT_RATIO:
        return RiskCheckResult(code, False, f"资产负债率过高({debt_ratio:.1%})")

    avg_turnover = get_avg_turnover_20d(code, is_etf=False)
    if avg_turnover < config.MIN_AVG_TURNOVER_20D:
        return RiskCheckResult(code, False, f"流动性不足(日均成交额{avg_turnover/1e4:.0f}万)")
    return RiskCheckResult(code, True, "通过")


def risk_screen_etf(code: str) -> RiskCheckResult:
    """ETF 只需做流动性与停牌检查，不涉及资产负债率/ST"""
    avg_turnover = get_avg_turnover_20d(code, is_etf=True)
    if avg_turnover < config.MIN_AVG_TURNOVER_20D:
        return RiskCheckResult(code, False, f"流动性不足(日均成交额{avg_turnover/1e4:.0f}万)")
    return RiskCheckResult(code, True, "通过")


def build_investable_universe(mode: str = "etf_only",
                              stock_profiles: list = None) -> dict:
    """
    构建可投资标的池：mode 由用户建组合时指定（"etf_only" / "mixed" / "stock_only"），
    不按资金量自动判断。
    stock_profiles: fundamentals.build_stock_profiles() 的结果列表，混合/纯股票不传时自动拉取。
    返回 {"mode", "etf", "stock", "rejected", "profiles"(幸存个股的基本面快照)}
    """
    result = {"mode": mode, "etf": [], "stock": [], "rejected": [], "profiles": {}}

    # 纯股票模式：不筛 ETF
    if mode != "stock_only":
        for code in config.ETF_POOL:
            r = risk_screen_etf(code)
            if r.passed:
                result["etf"].append(code)
            else:
                result["rejected"].append((code, r.reason))

    if mode in ("mixed", "stock_only"):
        from fundamentals import build_stock_profiles, get_latest_volume_map
        if stock_profiles is None:
            stock_profiles = build_stock_profiles(config.STOCK_POOL)
        prof_by_code = {p.get("code"): p for p in stock_profiles if p.get("code")}
        vol_map = get_latest_volume_map(config.STOCK_POOL)
        for code in config.STOCK_POOL:
            profile = prof_by_code.get(code)
            if profile is None:
                result["rejected"].append((code, "未获取到基本面数据"))
                continue
            r = risk_screen_stock(code, profile.get("name") or "", vol_map.get(code, 0.0),
                                  profile=profile)
            if r.passed:
                result["stock"].append(code)
                result["profiles"][code] = profile
            else:
                result["rejected"].append((code, r.reason))

    logger.info(f"投资域构建完成: mode={mode}, ETF={len(result['etf'])}只, "
                f"个股={len(result['stock'])}只, 剔除={len(result['rejected'])}只")
    return result


# ---------------------------------------------------------------------------
# 3. Point-in-time 财报滞后处理
# ---------------------------------------------------------------------------

def financial_data_usable_date(period_end: dt.date, report_type: str) -> dt.date:
    """
    根据 config.REPORT_LAG_RULES 计算某一期财报数据的"最早可用日期"。
    在因子计算时，任何截面日期 T，只应使用 usable_date <= T 的财报数据，
    以规避 look-ahead bias（详见 README 中的说明）。
    """
    rule = config.REPORT_LAG_RULES[report_type]
    usable_after = rule["usable_after"]
    prefix, date_part = usable_after.split("-", 1)
    month, day = map(int, date_part.split("-"))
    year = period_end.year + 1 if prefix == "next_year" else period_end.year
    return dt.date(year, month, day)


def filter_financials_pit(financial_records: pd.DataFrame, as_of_date: dt.date) -> pd.DataFrame:
    """
    对一批财务记录做 point-in-time 过滤：
    financial_records 需包含列 ['period_end', 'report_type', ...其他因子字段]
    仅保留 usable_date <= as_of_date 的记录，且每个 code 只取最新一期。
    """
    if financial_records.empty:
        return financial_records
    df = financial_records.copy()
    df["usable_date"] = df.apply(
        lambda row: financial_data_usable_date(row["period_end"], row["report_type"]), axis=1
    )
    df = df[df["usable_date"] <= as_of_date]
    df = df.sort_values("period_end").groupby("code", as_index=False).tail(1)
    return df


if __name__ == "__main__":
    universe = build_investable_universe()
    print(universe)
