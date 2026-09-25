# -*- coding: utf-8 -*-
"""
csmar_loader.py
读取并查询用户从 CSMAR/CNRDS 下载的三份原始数据：

  1) A股退市日期/TRDNEW_Co.xlsx        —— 全A股（含已退市）名单 + 上市/退市日期
  2) A股股利分配/FI_T11.xlsx           —— 每股现金股利（实施口径）
  3) A股特殊处理股票/特殊处理内容.xlsx  —— ST/*ST 变更历史（带公告日/实施日）

设计要点：
  - 原始 xlsx 解析后转存 parquet 缓存到 config.CSMAR_CACHE_DIR，之后直接读缓存（xlsx 很大，解析慢）。
  - 所有查询都支持 point-in-time（按"当时能看到什么"取数），规避 look-ahead bias。
  - 数据源目录由 config.CSMAR_DATA_DIR 指定。

【分红口径查证结论 —— 务必保留，勿凭感觉改】
FI_T11 里同一股票-会计年度可能出现两条记录（Accper=6月30日 与 12月31日）：
  经三只股票独立联网核查确认：**12月31日那条 = 该会计年度分红总额（累计口径，已含中期）**，
  不是"额外的年度分红"。故取用规则为「取年度条，没有年度条就取中期条」，**绝不可把两条相加**。

  核查证据：
   · 贵州茅台600519 2024：数据 中期23.882 / 年度51.555；
     真实公告为 中期每10股派238.82元 + 年度每10股派276.73元 → (238.82+276.73)/10 = 51.555 ✓
   · 东鹏饮料605499 2024：数据 中期2.50 / 年度5.00；
     真实公告为 中期每10股派25元 + 年度每10股派25元 → (25+25)/10 = 5.00 ✓
   · 锦和商管603682 2023：数据 中期0.220 / 年度0.220；
     真实公告为「2023年度不进行利润分配」（年度不分红），全年仅中期0.220 ✓
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger("csmar_loader")
logging.basicConfig(level=logging.INFO)

# 原始文件名
_F_DELIST = ("A股退市日期", "TRDNEW_Co.xlsx")
_F_DIV = ("A股股利分配", "FI_T11.xlsx")
_F_ST = ("A股特殊处理股票", "特殊处理内容.xlsx")

# ---------------------------------------------------------------------------
# 内部：读取 + parquet 缓存
# ---------------------------------------------------------------------------

def _cache_path(name: str) -> Path:
    config.CSMAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.CSMAR_CACHE_DIR / f"{name}.parquet"


def _load_with_cache(name: str, xlsx_rel: tuple[str, str], parser, force: bool = False) -> pd.DataFrame:
    """通用「读 xlsx -> 解析 -> 存 parquet -> 复用」流程。"""
    cp = _cache_path(name)
    if cp.exists() and not force:
        return pd.read_parquet(cp)
    fp = Path(config.CSMAR_DATA_DIR) / xlsx_rel[0] / xlsx_rel[1]
    if not fp.exists():
        raise FileNotFoundError(f"CSMAR 原始文件不存在: {fp}")
    logger.info(f"解析 CSMAR 原始文件: {fp}")
    df = parser(fp)
    try:
        df.to_parquet(cp)
    except Exception as e:  # 缓存写失败不影响本次使用
        logger.warning(f"CSMAR 缓存写入失败(不影响使用): {e}")
    return df


def _read_excel_skip(fp: Path, skip: int, cols: list[str]) -> pd.DataFrame:
    """读 xlsx 并跳过前面若干说明行，再显式指定列名。"""
    df = pd.read_excel(fp, skiprows=skip)
    df = df.iloc[:, : len(cols)]
    df.columns = cols
    return df


# ---------------------------------------------------------------------------
# 1) 退市日期
# ---------------------------------------------------------------------------

def _parse_delisting(fp: Path) -> pd.DataFrame:
    """TRDNEW_Co.xlsx：前2行是中文表头+空行。"""
    df = _read_excel_skip(fp, 2, ["code", "list_date", "name", "delist_date", "full_name"])
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}", na=False)]
    for c in ("list_date", "delist_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    return df.drop_duplicates("code").reset_index(drop=True)


def load_delisting(force: bool = False) -> pd.DataFrame:
    """全A股（含已退市）名单。
    列: code, list_date, name, delist_date, full_name
    delist_date 为空 = 仍在市。
    """
    return _load_with_cache("delisting", _F_DELIST, _parse_delisting, force)


def get_delist_info(code: str) -> dict | None:
    """查单只股票的上市/退市信息；不在名单返回 None。"""
    d = load_delisting()
    row = d[d["code"] == str(code).zfill(6)]
    if row.empty:
        return None
    r = row.iloc[0]
    return {"code": r["code"], "list_date": r["list_date"], "name": r["name"],
            "delist_date": r["delist_date"], "full_name": r["full_name"],
            "delisted": pd.notna(r["delist_date"])}


def is_delisted_by(code: str, as_of: dt.date) -> bool:
    """截至 as_of 是否已退市（供回测剔除用）。"""
    info = get_delist_info(code)
    return bool(info and info["delisted"] and info["delist_date"].date() <= as_of)


# ---------------------------------------------------------------------------
# 2) 股利分配（每股现金股利，实施口径）
# ---------------------------------------------------------------------------

def _parse_dividends(fp: Path) -> pd.DataFrame:
    """FI_T11.xlsx：前2行是中文表头 + '没有单位'行。"""
    df = _read_excel_skip(fp, 2, ["code", "name", "accper", "typrep", "source",
                                  "dps_pretax", "dps_afttax"])
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}", na=False)]
    df["accper"] = pd.to_datetime(df["accper"], errors="coerce")
    for c in ("dps_pretax", "dps_afttax"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["typrep"] = df["typrep"].astype(str).str.strip().str.upper()
    df = df.dropna(subset=["accper"])

    # 关键：Typrep A(合并报表)/B(母公司报表) 的每股股利数值完全相同（已实测50375条中0条不同），
    # 必须去重，否则同一笔分红会被算两次。统一保留 A，A 缺失时保留 B。
    df["_pref"] = (df["typrep"] == "B").astype(int)  # A 优先
    df = df.sort_values(["code", "accper", "_pref"])
    df = df.drop_duplicates(["code", "accper"], keep="first").drop(columns="_pref")

    df["year"] = df["accper"].dt.year
    df["is_annual"] = df["accper"].dt.month == 12   # 12月31日 = 年度（累计，含中期）
    return df.sort_values(["code", "accper"]).reset_index(drop=True)


def load_dividends(force: bool = False) -> pd.DataFrame:
    """每股现金股利（实施口径，单位：元/股）。
    列: code, name, accper, typrep, source, dps_pretax, dps_afttax, year, is_annual

    口径提醒（详见模块顶部说明）：
      12月31日那条 = 该会计年度分红总额（已含中期）；取数时"取年度条，没有则取中期条"，**不可相加**。
    """
    return _load_with_cache("dividends", _F_DIV, _parse_dividends, force)


def _annual_report_usable_date(year: int) -> dt.date:
    """某会计年度的分红数据最早可用日期（按 config.REPORT_LAG_RULES['annual'] 口径）。"""
    rule = config.REPORT_LAG_RULES["annual"]["usable_after"]   # 'next_year-05-01'
    prefix, md = rule.split("-", 1)
    m, d = map(int, md.split("-"))
    return dt.date(year + 1 if prefix == "next_year" else year, m, d)


def get_dps(code: str, as_of: dt.date) -> float | None:
    """取 as_of 时点**已知**的、最近一个完整会计年度的每股税前现金股利（元/股）。

    point-in-time：只使用"年报披露后才可用"的数据（年报最晚4/30披露，按 5/1 起可用），
    因此不会用到当时还没公布的未来分红。
    """
    d = load_dividends()
    sub = d[d["code"] == str(code).zfill(6)]
    if sub.empty:
        return None
    # 只保留当时已披露的（按会计年度 + 年报滞后规则）
    usable = sub[sub["year"].apply(lambda y: _annual_report_usable_date(int(y)) <= as_of)]
    if usable.empty:
        return None
    # 优先取"年度(12月31日)"那条 —— 它是全年累计口径；没有年度条时取该年中期条
    ann = usable[usable["is_annual"]]
    pick = ann.sort_values("year").iloc[-1] if not ann.empty else usable.sort_values("accper").iloc[-1]
    v = pick["dps_pretax"]
    return float(v) if pd.notna(v) else None


def get_dps_map(codes: list[str], as_of: dt.date) -> dict[str, float]:
    """批量版 get_dps，返回 {code: 每股税前现金股利}（取不到的不在结果里）。"""
    d = load_dividends()
    d = d[d["code"].isin([str(c).zfill(6) for c in codes])]
    if d.empty:
        return {}
    d = d.copy()
    d["usable"] = d["year"].apply(lambda y: _annual_report_usable_date(int(y)))
    d = d[d["usable"] <= as_of]
    if d.empty:
        return {}
    # 每年优先年度条
    d = d.sort_values(["code", "year", "is_annual", "accper"])
    latest = d.groupby("code").tail(1)
    return {r["code"]: float(r["dps_pretax"]) for _, r in latest.iterrows()
            if pd.notna(r["dps_pretax"])}


def get_div_yield(code: str, price: float, as_of: dt.date) -> float | None:
    """股息率 = 每股税前现金股利 ÷ 股价。price<=0 或取不到股利时返回 None。"""
    if price is None or price <= 0:
        return None
    dps = get_dps(code, as_of)
    if dps is None:
        return None
    return dps / float(price)


def get_div_spread(code: str, price: float, as_of: dt.date,
                   rf: float | None = None) -> float | None:
    """股息率 - 10年期国债收益率（估值因子用的利差）。"""
    y = get_div_yield(code, price, as_of)
    if y is None:
        return None
    return y - (config.YIELD_10Y_CN if rf is None else rf)


# ---------------------------------------------------------------------------
# 3) 特殊处理（ST/*ST）历史
# ---------------------------------------------------------------------------

# TCatCd -> 处理后的状态
_TCAT_STATE = {
    1: "*ST", 2: "正常", 3: "正常", 4: "ST", 5: "ST", 6: "*ST",
    7: "退市", 8: "退市整理期", 9: "PT", 10: "ST", 11: "正常",
}


def _parse_st(fp: Path) -> pd.DataFrame:
    """特殊处理内容.xlsx：第1行是中文表头。"""
    df = _read_excel_skip(fp, 1, ["code", "name", "annc_date", "imp_date",
                                  "name_after", "tcat", "tcat_cd", "reason"])
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}", na=False)]
    for c in ("annc_date", "imp_date"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    df["tcat_cd"] = pd.to_numeric(df["tcat_cd"], errors="coerce")
    df = df.dropna(subset=["imp_date", "tcat_cd"])
    df["tcat_cd"] = df["tcat_cd"].astype(int)
    # 按实施日排序，并给出实施后的状态
    df["state_after"] = df["tcat_cd"].map(_TCAT_STATE)
    return df.sort_values(["code", "imp_date"]).reset_index(drop=True)


def load_special_treatment(force: bool = False) -> pd.DataFrame:
    """ST/*ST 变更历史。
    列: code, name, annc_date(公告日), imp_date(实施日), name_after(处理后简称),
        tcat(处理类别), tcat_cd(类别编号), reason(原因), state_after(处理后状态)
    """
    return _load_with_cache("special_treatment", _F_ST, _parse_st, force)


def get_st_status(code: str, as_of: dt.date) -> str | None:
    """判断 as_of 当日该股票的 ST 状态（point-in-time）。

    返回: '正常' / 'ST' / '*ST' / 'PT' / '退市' / '退市整理期'
          返回 None 表示数据里没有任何特殊处理记录（即从未被特殊处理）。
    依据：按"处理实施日 ImpDate <= as_of"的事件逐条回放，取最后一条的状态。
    """
    d = load_special_treatment()
    ev = d[(d["code"] == str(code).zfill(6)) & (d["imp_date"].dt.date <= as_of)]
    if ev.empty:
        return None
    return ev.sort_values("imp_date").iloc[-1]["state_after"]


def is_st_on(code: str, as_of: dt.date, include_pt: bool = True) -> bool:
    """as_of 当日是否处于特殊处理状态（ST/*ST，可选含 PT）。无记录视为正常。"""
    s = get_st_status(code, as_of)
    if s in (None, "正常"):
        return False
    if s == "PT":
        return include_pt
    return True


def get_st_events(code: str) -> pd.DataFrame:
    """某只股票的完整特殊处理变更历史（按实施日升序）。"""
    d = load_special_treatment()
    return d[d["code"] == str(code).zfill(6)].sort_values("imp_date").reset_index(drop=True)


def get_st_map(codes: list[str], as_of: dt.date) -> dict[str, str]:
    """批量：返回 {code: 状态}，只包含"当时处于非正常状态"的股票。"""
    d = load_special_treatment()
    d = d[d["code"].isin([str(c).zfill(6) for c in codes]) & (d["imp_date"].dt.date <= as_of)]
    if d.empty:
        return {}
    last = d.sort_values("imp_date").groupby("code").tail(1)
    return {r["code"]: r["state_after"] for _, r in last.iterrows()
            if r["state_after"] != "正常"}


# ---------------------------------------------------------------------------
# 3.5) 财务指标表（偿债能力 / 盈利能力 / 披露财务指标 / 相对价值指标 / 预披露日期）
#     这几张表格式一致：Excel 前2行是说明行，第3行起是数据；有 Typrep(A合并/B母公司) 需去重。
# ---------------------------------------------------------------------------

_F_T1 = ("偿债能力", "FI_T1.xlsx")
_F_T5 = ("盈利能力", "FI_T5.xlsx")
_F_T2 = ("披露财务指标", "FI_T2.xlsx")
_F_T10 = ("相对价值指标", "FI_T10.xlsx")
_F_IAR = ("年、中、季报预披露日期表", "IAR_Forecdt.xlsx")


def _parse_fin_table(fp: Path, cols: list[str], dedup: bool = True) -> pd.DataFrame:
    """通用财务表解析：跳过前2行说明行 → 规范化代码/日期 → 按 code+accper 去重(A优先)。"""
    df = _read_excel_skip(fp, 2, cols)
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}", na=False)].copy()
    df["accper"] = pd.to_datetime(df["accper"], errors="coerce")
    df = df.dropna(subset=["accper"])
    if dedup and "typrep" in df.columns:
        df["typrep"] = df["typrep"].astype(str).str.strip().str.upper()
        df["_pref"] = (df["typrep"] == "B").astype(int)   # A 优先
        df = df.sort_values(["code", "accper", "_pref"])
        df = df.drop_duplicates(["code", "accper"], keep="first").drop(columns="_pref")
    return df.sort_values(["code", "accper"]).reset_index(drop=True)


def load_debt_ratio(force: bool = False) -> pd.DataFrame:
    """偿债能力：资产负债率 + 行业(证监会口径)。
    列: code, name, accper, typrep, source, ind_code, ind_name, debt_ratio
    """
    def _p(fp):
        df = _parse_fin_table(fp, ["code", "name", "accper", "typrep", "source",
                                   "ind_code", "ind_name", "debt_ratio"])
        df["debt_ratio"] = pd.to_numeric(df["debt_ratio"], errors="coerce")
        return df
    return _load_with_cache("debt_ratio", _F_T1, _p, force)


def load_profitability(force: bool = False) -> pd.DataFrame:
    """盈利能力：ROE(A/B/C/TTM) + 营业毛利率。
    列: code, name, accper, typrep, source, ind_code, ind_name,
        roe_a, roe_b, roe_c, roe_ttm, gross_margin, gross_margin_ttm
    注：ROE 三种口径 —— A=净利润/期末股东权益；B=(期末+期初)/2；C=(期末+上年期末)/2。
        统一用 **roe_b（平均净资产口径）** 作为打分口径（最贴近 tushare 的加权ROE）。
    """
    def _p(fp):
        df = _parse_fin_table(fp, ["code", "name", "accper", "typrep", "source",
                                   "ind_code", "ind_name", "roe_a", "roe_b", "roe_c",
                                   "roe_ttm", "gross_margin", "gross_margin_ttm"])
        for c in ["roe_a", "roe_b", "roe_c", "roe_ttm", "gross_margin", "gross_margin_ttm"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    return _load_with_cache("profitability", _F_T5, _p, force)


def load_per_share(force: bool = False) -> pd.DataFrame:
    """披露财务指标：每股经营活动现金流 + 基本每股收益（用于 现金流/净利 排雷）。
    列: code, name, accper, ind_code, ind_name, ocfps, eps
    ⚠️ 该表只有 2007 年之后的数据。
    """
    def _p(fp):
        df = _parse_fin_table(fp, ["code", "name", "accper", "ind_code", "ind_name",
                                   "ocfps", "eps"], dedup=False)
        for c in ["ocfps", "eps"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    return _load_with_cache("per_share", _F_T2, _p, force)


def load_valuation_pe(force: bool = False) -> pd.DataFrame:
    """相对价值指标：市盈率(PE)TTM + 行业。
    列: code, name, accper, source, ind_code, ind_name, pe_ttm
    """
    def _p(fp):
        df = _parse_fin_table(fp, ["code", "name", "accper", "source",
                                   "ind_code", "ind_name", "pe_ttm"], dedup=False)
        df["pe_ttm"] = pd.to_numeric(df["pe_ttm"], errors="coerce")
        return df
    return _load_with_cache("valuation_pe", _F_T10, _p, force)


def load_disclosure_dates(force: bool = False) -> pd.DataFrame:
    """年/中/季报实际披露日（point-in-time 关键）。
    列: code, name, accper(统计截止日期), actudt(实际披露日)
    """
    def _p(fp):
        df = _parse_fin_table(fp, ["code", "name", "accper", "actudt"], dedup=False)
        df["actudt"] = pd.to_datetime(df["actudt"], errors="coerce")
        return df
    return _load_with_cache("disclosure_dates", _F_IAR, _p, force)


# ---------------------------------------------------------------------------
# 3.6) 日个股行情（TRD_Dalyr，25年，19个Excel分布在5个子文件夹）
#     列：code, trade_date, open, high, low, close, volume(股), amount(元),
#         float_mv(千元), total_mv(千元), ret_wd(含红利再投资日收益), trade_status
#     trade_status: 1正常 / 2 ST / 3 *ST / 4 S / 5 SST / 6 S*ST / 7 G / 8 GST /
#                   9 G*ST / 10 U / 11 UST / 12 U*ST / 13 N / 14 NST / 15 N*ST / 16 PT
# ---------------------------------------------------------------------------

_F_MKT_DIR = "日个股回报率文件"
_MKT_COLS = ["code", "trade_date", "open", "high", "low", "close", "volume", "amount",
             "float_mv", "total_mv", "ret_wd", "trade_status", "after_vol"]


def _parse_market(fp: Path) -> pd.DataFrame:
    df = _read_excel_skip(fp, 2, _MKT_COLS)
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    df = df[df["code"].str.fullmatch(r"\d{6}", na=False)].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for c in ["open", "high", "low", "close", "volume", "amount",
              "float_mv", "total_mv", "ret_wd", "trade_status"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["trade_date"]).reset_index(drop=True)


def _market_files(folders: list[str] = None) -> list[Path]:
    root = Path(config.CSMAR_DATA_DIR) / _F_MKT_DIR
    if not root.exists():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if folders and d.name not in folders:
            continue
        out += sorted(d.glob("TRD_Dalyr*.xlsx"))
    return out


def load_market_daily(folders: list[str] = None, force: bool = False) -> pd.DataFrame:
    """日个股行情。按文件逐个转 parquet 缓存（每个文件约100万行，首次较慢）。
    folders: 只加载指定子文件夹（如 ["21-26"]）；None=全部。
    """
    files = _market_files(folders)
    if not files:
        raise FileNotFoundError(f"未找到行情文件: {config.CSMAR_DATA_DIR}\\{_F_MKT_DIR}")
    frames = []
    for fp in files:
        key = f"{fp.parent.name}_{fp.stem}"
        cp = _cache_path("mkt_" + key)
        if cp.exists() and not force:
            df = pd.read_parquet(cp)
        else:
            logger.info(f"解析行情文件: {fp.name}（{fp.parent.name}）")
            df = _parse_market(fp)
            try:
                df.to_parquet(cp)
            except Exception as e:
                logger.warning(f"行情缓存写入失败: {e}")
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def get_latest_snapshot(lookback_days: int = 30, folders: list[str] = None) -> pd.DataFrame:
    """取最近 lookback_days 个自然日的行情，算每只股票的：
    latest_close(最新收盘), avg_amount_20d(近20个交易日日均成交额, 元), last_date, trade_status
    供"扩池筛选"用（股价 + 流动性 + 停牌判断）。

    ⚠️ folders 不给时【只加载最后一个时间段文件夹】（如 21-26），因为最新行情只在那里，
       加载全部19个文件要几十分钟。需要全历史请显式传 folders。
    """
    if folders is None:
        allf = _market_files()
        if not allf:
            raise FileNotFoundError("未找到行情文件")
        folders = [allf[-1].parent.name]          # 按文件名排序的最后一个 = 最新时间段
    df = load_market_daily(folders=folders)
    last = df["trade_date"].max()
    start = last - pd.Timedelta(days=lookback_days + 30)   # 多留点缓冲取满20个交易日
    sub = df[df["trade_date"] >= start].sort_values(["code", "trade_date"])
    g = sub.groupby("code", sort=False)
    out = pd.DataFrame({
        "latest_close": g["close"].last(),
        "avg_amount_20d": g["amount"].apply(lambda s: s.tail(20).mean()),
        "last_date": g["trade_date"].last(),
        "trade_status": g["trade_status"].last(),
        "total_mv": g["total_mv"].last(),
    })
    return out


# ---------------------------------------------------------------------------
# 4) 代码 -> 中文简称（页面展示用）
# ---------------------------------------------------------------------------
_NAME_MAP_CACHE: dict[str, str] | None = None


def get_all_names() -> dict[str, str]:
    """全市场 代码->简称 映射（构建一次后常驻内存，离线、不消耗任何 API）。
    ETF 名来自 config.ETF_NAMES；个股名来自 CSMAR 退市表(覆盖全部A股含已退市)。
    """
    global _NAME_MAP_CACHE
    if _NAME_MAP_CACHE is None:
        m: dict[str, str] = dict(config.ETF_NAMES)
        try:
            dl = load_delisting()
            m.update({str(k).zfill(6): str(v) for k, v in zip(dl["code"], dl["name"]) if pd.notna(v)})
        except Exception as e:
            logger.warning(f"构建名称映射失败(退市表): {e}")
        try:
            dv = load_dividends()
            for c, n in zip(dv["code"], dv["name"]):
                c = str(c).zfill(6)
                if c not in m and pd.notna(n):
                    m[c] = str(n)
        except Exception as e:
            logger.warning(f"构建名称映射失败(分红表): {e}")
        _NAME_MAP_CACHE = m
    return _NAME_MAP_CACHE


def name_of(code: str) -> str:
    """单只标的中文简称；取不到返回空串。"""
    return get_all_names().get(str(code).zfill(6), "")


def labeled(code: str) -> str:
    """展示用文本：有名字返回 '600519 贵州茅台'，没有则只返回代码。"""
    c = str(code).zfill(6)
    n = name_of(c)
    return f"{c} {n}" if n else c


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    today = dt.date.today()
    print("=== 退市数据 ===")
    dl = load_delisting(); print(dl.shape, "已退市:", dl["delist_date"].notna().sum())
    print("\n=== 股利分配 ===")
    dv = load_dividends(); print(dv.shape, "年度条:", dv["is_annual"].sum())
    for c in ("600519", "605499", "603682"):
        print(f"  {c} 截至{today} 每股税前股利 = {get_dps(c, today)}")
    print("\n=== 特殊处理 ===")
    st = load_special_treatment(); print(st.shape)
    print("  600887 于 2009-06-01 的ST状态:", get_st_status("600887", dt.date(2009, 6, 1)))
    print("  600887 于 2010-12-01 的ST状态:", get_st_status("600887", dt.date(2010, 12, 1)))
