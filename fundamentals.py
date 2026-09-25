# -*- coding: utf-8 -*-
"""
fundamentals.py
职责：对接 Tushare 财务/估值/行业数据，为个股"防雷网财务排雷 + 纯基本面打分"提供数据。

核心输出 build_stock_profiles(codes)：返回每只个股的一个"基本面快照"，含：
  - 排雷用：上市天数、PE-TTM、近3年平均ROE、最新毛利率、经营现金流/净利润比值、负债率
  - 打分用：近3年平均ROE、最新毛利率、PE-TTM历史分位、股息率-10年国债利差、负债率、市值、申万一级行业
  - 中性化用：market_cap(元)、industry(申万一级真实行业)

调用控制（他人账号、积分未知）：
  - 申万行业归属、上市日期、全市场基础信息落盘缓存(.cache)，只第一次付调用费
  - 单只股票财务=1次 fina_indicator；估值历史=1次 daily_basic
  - 若接口返回频率限制/积分不足类错误，抛 RateLimitError 立即中止，不重试硬冲
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import tushare as ts
except ImportError:
    ts = None

import config
import csmar_loader

logger = logging.getLogger("fundamentals")

# 频率限制/权限不足错误关键字：命中立即中止，避免把他人账号打到被限制
_RATE_LIMIT_HINTS = ("每分钟最多", "访问次数", "频率", "积分不足", "429", "limit", "rate")


class RateLimitError(RuntimeError):
    """调用频率/权限受限，应停止后续请求。"""


def _to_ts_code(code: str) -> str:
    if code.startswith(("6", "5", "9")):
        return code + ".SH"
    if code.startswith(("0", "3", "1", "2")):
        return code + ".SZ"
    if code.startswith(("4", "8")):
        return code + ".BJ"
    return code + ".SH"


def _check_rate_error(e: Exception) -> None:
    """检测 tushare 抛出的频率/权限错误，命中则中止。"""
    msg = str(e)
    low = msg.lower()
    if any(k in low for k in _RATE_LIMIT_HINTS):
        raise RateLimitError(f"检测到接口限流/权限错误，已停止：{msg}") from e


def _pro():
    if ts is None:
        raise ImportError("未安装 tushare")
    tok = os.environ.get("TUSHARE_TOKEN", "")
    if not tok:
        env = config.PROJECT_ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("TUSHARE_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    ts.set_token(tok)
    return ts.pro_api()


# ---------------------------------------------------------------------------
# 缓存目录
# ---------------------------------------------------------------------------
CACHE_SUBDIR = config.CACHE_DIR

def _load_cache(name: str, max_age_days: int = 7) -> dict | None:
    p = CACHE_SUBDIR / name
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        age = (dt.date.today() - dt.date.fromisoformat(data.get("fetched_at", "2000-01-01"))).days
        if age <= max_age_days:
            return data.get("data")
    except Exception:
        pass
    return None


def _save_cache(name: str, data) -> None:
    CACHE_SUBDIR.mkdir(parents=True, exist_ok=True)
    (CACHE_SUBDIR / name).write_text(
        json.dumps({"fetched_at": dt.date.today().isoformat(), "data": data},
                   ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# 申万一级行业归属（磁盘缓存）
# ---------------------------------------------------------------------------
def get_sw_l1_map(codes: list[str]) -> dict[str, str]:
    """返回 {code: 申万一级行业名}，缺失的 code 从 tushare index_member_all 补齐并缓存。"""
    cached = _load_cache("sw_l1_map.json", max_age_days=14) or {}
    missing = [c for c in codes if c not in cached]
    if missing:
        pro = _pro()
        for code in missing:
            try:
                df = pro.index_member_all(ts_code=_to_ts_code(code))
                if df is None or df.empty:
                    cached[code] = ""
                    continue
                # 选当前仍有效(is_new=Y)或最新生效(in_date 最大)的一行
                df = df.sort_values("in_date", ascending=False)
                row = df.iloc[0]
                cached[code] = row["l1_name"] if pd.notna(row["l1_name"]) else ""
            except Exception as e:
                _check_rate_error(e)
                logger.warning(f"[{code}] 申万行业获取失败: {e}")
                cached[code] = ""
        _save_cache("sw_l1_map.json", cached)
    return {c: cached.get(c, "") for c in codes}


# ---------------------------------------------------------------------------
# 申万一级行业【历史成分】（point-in-time）
# ---------------------------------------------------------------------------

_SW_MEMBERS_FILE = config.CACHE_DIR / "sw_members.parquet"


def load_sw_members(force: bool = False) -> pd.DataFrame:
    """申万一级行业历史成分（含 in_date/out_date），31次调用后落盘缓存。

    为什么不用 index_member_all：该接口每只股票只返回 1 条"当前"记录且 out_date 全空；
    index_member(index_code=<一级行业指数>) 才返回【完整进出历史】。

    列: l1_name(申万一级行业名), code(6位), in_date, out_date
    """
    if _SW_MEMBERS_FILE.exists() and not force:
        return pd.read_parquet(_SW_MEMBERS_FILE)
    pro = _pro()
    cls = pro.index_classify(level="L1", src="SW2021")
    frames = []
    for _, r in cls.iterrows():
        try:
            m = pro.index_member(index_code=r["index_code"])
        except Exception as e:
            _check_rate_error(e)
            logger.warning(f"[申万] {r['industry_name']} 成分获取失败: {e}")
            continue
        if m is None or m.empty:
            continue
        m = m.copy()
        m["l1_name"] = r["industry_name"]
        frames.append(m)
        time.sleep(0.15)
    if not frames:
        raise RuntimeError("申万成分全部获取失败")
    df = pd.concat(frames, ignore_index=True).rename(columns={"con_code": "ts_code"})
    df["code"] = df["ts_code"].astype(str).str[:6]
    df["in_date"] = pd.to_datetime(df["in_date"], format="%Y%m%d", errors="coerce")
    df["out_date"] = pd.to_datetime(df["out_date"], format="%Y%m%d", errors="coerce")
    df = df[["l1_name", "code", "in_date", "out_date"]].dropna(subset=["in_date"])
    try:
        df.to_parquet(_SW_MEMBERS_FILE)
    except Exception as e:
        logger.warning(f"申万成分缓存写入失败: {e}")
    return df


def get_sw_l1_pit(codes: list[str], as_of: dt.date) -> dict[str, str]:
    """point-in-time：截至 as_of，每只股票所属的申万一级行业。

    ⚠️ 约 7% 股票存在"区间重叠"（申万2021改版把新行业回溯到历史，同时保留旧记录）。
    处理规则：同一时点命中多条时，取 in_date 最早的那条 = "当时的口径"，
    避免用新版分类回溯历史造成前视偏差。
    """
    m = load_sw_members()
    want = {str(c).zfill(6) for c in codes}
    m = m[m["code"].isin(want)]
    ts = pd.Timestamp(as_of)
    m = m[(m["in_date"] <= ts) & (m["out_date"].isna() | (m["out_date"] > ts))]
    if m.empty:
        return {}
    m = m.sort_values(["code", "in_date"])
    return m.groupby("code")["l1_name"].first().to_dict()


# ---------------------------------------------------------------------------
# 全市场基础信息（名称 / 上市日期，磁盘缓存）
# ---------------------------------------------------------------------------
def _latest_trade_date(pro) -> str | None:
    end = dt.date.today()
    start = end - dt.timedelta(days=14)
    cal = pro.trade_cal(exchange="SSE", start_date=start.strftime("%Y%m%d"),
                        end_date=end.strftime("%Y%m%d"), is_open="1")
    if cal is None or cal.empty:
        return None
    return str(cal["cal_date"].sort_values().iloc[-1])


def get_latest_volume_map(codes: list[str]) -> dict[str, float]:
    """最近交易日全市场成交量（1次调用），按交易日缓存。{code: vol}"""
    cached = _load_cache("latest_volume.json", max_age_days=0) or {}
    if cached:
        return {c: cached.get(c, 0.0) for c in codes}
    pro = _pro()
    ld = _latest_trade_date(pro)
    if ld is None:
        return {}
    try:
        df = pro.daily(trade_date=ld, fields="ts_code,vol")
        if df is None or df.empty:
            return {}
        m = {}
        for _, r in df.iterrows():
            sym = r["ts_code"].split(".")[0]
            m[sym] = float(r["vol"]) if pd.notna(r["vol"]) else 0.0
        _save_cache("latest_volume.json", m)
        return {c: m.get(c, 0.0) for c in codes}
    except Exception as e:
        _check_rate_error(e)
        logger.warning(f"全市场成交量获取失败: {e}")
        return {}


def get_stock_basic_info_map(codes: list[str]) -> dict[str, dict]:
    """返回 {code: {"name":..., "list_date": "YYYYMMDD" or None}}，缺失则拉取并缓存。"""
    cached = _load_cache("stock_basic_info.json", max_age_days=7) or {}
    missing = [c for c in codes if c not in cached]
    if missing:
        pro = _pro()
        try:
            df = pro.stock_basic(exchange="", list_status="L",
                                 fields="ts_code,symbol,name,list_date")
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    cached[r["symbol"]] = {"name": r["name"],
                                           "list_date": str(r["list_date"]) if pd.notna(r["list_date"]) else ""}
            _save_cache("stock_basic_info.json", cached)
        except Exception as e:
            _check_rate_error(e)
            logger.warning(f"stock_basic 全市场获取失败: {e}")
    return {c: cached.get(c, {"name": "", "list_date": ""}) for c in codes}


# ---------------------------------------------------------------------------
# 单只个股基本面快照
# ---------------------------------------------------------------------------

def _annual_rows(fina: pd.DataFrame) -> pd.DataFrame:
    """取年报（报告期 12-31）并按时间升序"""
    if fina is None or fina.empty:
        return pd.DataFrame()
    f = fina.copy()
    f["end_date"] = f["end_date"].astype(str)
    f = f[f["end_date"].str.endswith("1231")]
    return f.sort_values("end_date")


def _latest(fina: pd.DataFrame) -> pd.Series | None:
    if fina is None or fina.empty:
        return None
    return fina.sort_values("end_date").iloc[-1]


def _as_frac(v) -> float | None:
    """tushare 百分数值 -> 小数（17.95 -> 0.1795）；NaN 返回 None"""
    try:
        x = float(v)
        if np.isnan(x):
            return None
        return x / 100.0
    except (TypeError, ValueError):
        return None


def fetch_one_profile(code: str, sw_map: dict[str, str],
                      basic_map: dict[str, dict]) -> dict:
    """拉取单只个股的完整基本面快照。数据拉不全时关键字段为 None。"""
    pro = _pro()
    ts_code = _to_ts_code(code)
    today = dt.date.today()
    profile = {
        "code": code,
        "name": basic_map.get(code, {}).get("name", ""),
        "industry": sw_map.get(code, ""),
        "list_date": basic_map.get(code, {}).get("list_date", "") or "",
        "days_listed": None,
        "pe_ttm": None, "pe_pctile": None,
        "dv_ttm": None, "div_spread": None, "div_source": None,
        "roe_avg3": None, "gross_margin": None,
        "debt_ratio": None, "ocf_ni_ratio": None,
        "total_mv": None, "ok": True,
    }
    # 上市天数（次新股排雷）
    if profile["list_date"]:
        try:
            listed = dt.date(int(profile["list_date"][:4]), int(profile["list_date"][4:6]),
                             int(profile["list_date"][6:8]))
            profile["days_listed"] = (today - listed).days
        except ValueError:
            pass

    # 1) 财务指标 fina_indicator
    try:
        start = (today - dt.timedelta(days=6 * 365)).strftime("%Y%m%d")
        fina = pro.fina_indicator(ts_code=ts_code, start_date=start,
                                  fields="ts_code,end_date,ann_date,roe,eps,ocfps,"
                                         "grossprofit_margin,debt_to_assets")
        if fina is not None and not fina.empty:
            ann = _annual_rows(fina)
            latest = _latest(fina)
            if latest is not None:
                profile["gross_margin"] = _as_frac(latest.get("grossprofit_margin"))
                profile["debt_ratio"] = _as_frac(latest.get("debt_to_assets"))
            # 近3年平均ROE（年报）
            if not ann.empty:
                roes = pd.to_numeric(ann["roe"].tail(3), errors="coerce")
                if roes.notna().any():
                    profile["roe_avg3"] = float(roes.mean() / 100.0)
            # 经营现金流/净利润 近3年年报均值 = ocfps/eps
            if not ann.empty:
                ratios = []
                for _, r in ann.tail(3).iterrows():
                    try:
                        eps, ocfps = float(r["eps"]), float(r["ocfps"])
                        if not (np.isnan(eps) or np.isnan(ocfps)) and abs(eps) > 1e-9:
                            ratios.append(ocfps / eps)
                    except (TypeError, ValueError):
                        continue
                if ratios:
                    profile["ocf_ni_ratio"] = float(np.mean(ratios))
    except Exception as e:
        _check_rate_error(e)
        logger.warning(f"[{code}] fina_indicator 失败: {e}")
        profile["ok"] = False

    # 2) 估值/市值 daily_basic（近2年历史算 PE 分位）
    try:
        start = (today - dt.timedelta(days=config.PE_PERCENTILE_LOOKBACK_DAYS)).strftime("%Y%m%d")
        db = pro.daily_basic(ts_code=ts_code, start_date=start, end_date=today.strftime("%Y%m%d"),
                             fields="trade_date,close,pe_ttm,dv_ttm,total_mv")
        if db is not None and not db.empty:
            db = db.sort_values("trade_date")
            latest = db.iloc[-1]
            pe_now = float(latest["pe_ttm"]) if pd.notna(latest["pe_ttm"]) else None
            if pe_now is not None and pe_now > 0:
                profile["pe_ttm"] = pe_now
                hist = pd.to_numeric(db["pe_ttm"], errors="coerce")
                hist = hist[(hist > 0) & hist.notna()]
                if len(hist) > 20:
                    profile["pe_pctile"] = float((hist < pe_now).mean())
            # 股息率：优先用 CSMAR 真实分红数据（每股税前现金股利 ÷ 最新股价），tushare dv_ttm 兜底。
            # CSMAR 取的是"最近一个已完成会计年度的全年每股股利"，按年报披露滞后规则做 point-in-time，
            # 且已按"取年度条(全年累计)、不与中期相加"的口径处理（见 csmar_loader 模块顶部说明）。
            close = float(latest["close"]) if pd.notna(latest.get("close", None)) else None
            dv_csmar = None
            if close and close > 0:
                try:
                    dv_csmar = csmar_loader.get_div_yield(code, close, today)
                except Exception as e:
                    logger.warning(f"[{code}] CSMAR 股息率计算失败: {e}")
            if dv_csmar is not None:
                profile["dv_ttm"] = dv_csmar
                profile["div_spread"] = dv_csmar - config.YIELD_10Y_CN
                profile["div_source"] = "csmar"
            else:
                dv = float(latest["dv_ttm"]) if pd.notna(latest["dv_ttm"]) else None
                if dv is not None:
                    profile["dv_ttm"] = dv / 100.0  # % -> 小数
                    profile["div_spread"] = profile["dv_ttm"] - config.YIELD_10Y_CN
                    profile["div_source"] = "tushare"
            mv = float(latest["total_mv"]) if pd.notna(latest["total_mv"]) else None
            if mv is not None:
                profile["total_mv"] = mv * 1e4  # 万元 -> 元
    except Exception as e:
        _check_rate_error(e)
        logger.warning(f"[{code}] daily_basic 失败: {e}")
        profile["ok"] = False

    return profile


def build_stock_profiles(codes: list[str]) -> list[dict]:
    """批量拉取基本面快照。遇到频率限制抛 RateLimitError 立即中止。"""
    sw_map = get_sw_l1_map(codes)
    basic_map = get_stock_basic_info_map(codes)
    profiles = []
    for code in codes:
        profiles.append(fetch_one_profile(code, sw_map, basic_map))
    return profiles
