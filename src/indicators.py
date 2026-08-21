"""
神华分析系统 - 技术指标
======================
所有技术指标统一使用 akquant 的内置 Indicator 类计算。
akquant 0.3.x 的指标是**类**，通过 `update` 逐根推送或 `update_many`
批量喂入，`.value` 取最新值。

- 单输入 (close): SMA(n), EMA(n), RSI(n), MACD(f,s,sig),
  BollingerBands(n, mult), TSF, TEMA, TRIX, ROC, MOM, WILLR...
- 多输入 (high, low, close, [volume]): ATR, ADX, STOCH, MFI, OBV...
- 流式: 没有 update_many 的就用 update 逐根推

KDJ 在 akquant 中没有直接提供，使用 STOCH 算 K/D，再合成 J = 3K - 2D。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

# 延迟 import: akquant 0.3.x 加载较慢 (~500ms 包含 backtest 子模块)
# 在 compute_all() 中按需导入并设置为模块全局
import numpy as np
import pandas as pd

# 占位, compute_all() 第一次调用时会替换
akquant = None  # type: ignore


def _ensure_akquant():
    """第一次使用时导入 akquant, 并注入到模块全局"""
    global akquant
    if akquant is not None:
        return
    import akquant as _ak
    akquant = _ak

logger = logging.getLogger(__name__)


def _arr(x) -> np.ndarray:
    """统一转 float64 numpy 数组"""
    if isinstance(x, pd.Series):
        return x.to_numpy(dtype=np.float64)
    return np.asarray(x, dtype=np.float64)


def _scalar(v) -> Optional[float]:
    """从 .value 拿到标量"""
    if v is None:
        return None
    if isinstance(v, tuple):
        # 多元返回: 取最后一个
        return _scalar(v[-1])
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return None
        return float(v)
    return None


def _last_n(arr, n: int):
    """取最后 n 个值; None -> 跳过"""
    if arr is None:
        return None
    try:
        if hasattr(arr, "__len__"):
            if len(arr) == 0:
                return None
            return float(arr[-1]) if isinstance(arr[-1], (int, float)) else arr[-1]
    except TypeError:
        pass
    return None


# ===== 单输入: 趋势 =====
def calc_ma(close: pd.Series, periods: List[int] = None) -> Dict[str, Optional[float]]:
    """简单移动均线 (SMA via akquant)"""
    periods = periods or [5, 10, 20, 30, 60, 120, 250]
    out: Dict[str, Optional[float]] = {}
    c = _arr(close)
    for p in periods:
        if len(c) < p:
            out[f"MA{p}"] = None
            continue
        try:
            ind = akquant.SMA(period=p)
            ind.update_many(c)
            out[f"MA{p}"] = _scalar(ind.value)
        except Exception as e:  # noqa: BLE001
            logger.warning("SMA(%d) failed: %s", p, e)
            out[f"MA{p}"] = None
    return out


def calc_ema(close: pd.Series, periods: List[int] = None) -> Dict[str, Optional[float]]:
    """指数均线"""
    periods = periods or [12, 26, 50]
    out: Dict[str, Optional[float]] = {}
    c = _arr(close)
    for p in periods:
        if len(c) < p + 1:
            out[f"EMA{p}"] = None
            continue
        try:
            ind = akquant.EMA(period=p)
            ind.update_many(c)
            out[f"EMA{p}"] = _scalar(ind.value)
        except Exception as e:  # noqa: BLE001
            out[f"EMA{p}"] = None
    return out


def calc_macd(close: pd.Series) -> Dict[str, Optional[float]]:
    """MACD (12, 26, 9)"""
    if len(close) < 35:
        return {"DIF": None, "DEA": None, "MACD": None}
    c = _arr(close)
    try:
        ind = akquant.MACD(fast_period=12, slow_period=26, signal_period=9)
        ind.update_many(c)
        v = ind.value
        if isinstance(v, tuple) and len(v) >= 3:
            return {"DIF": _scalar(v[0]), "DEA": _scalar(v[1]), "MACD": _scalar(v[2])}
    except Exception as e:  # noqa: BLE001
        logger.warning("MACD failed: %s", e)
    return {"DIF": None, "DEA": None, "MACD": None}


# ===== 震荡 =====
def calc_rsi(close: pd.Series, periods: List[int] = None) -> Dict[str, Optional[float]]:
    """RSI 多周期"""
    periods = periods or [6, 12, 24]
    out: Dict[str, Optional[float]] = {}
    c = _arr(close)
    for p in periods:
        if len(c) < p + 1:
            out[f"RSI{p}"] = None
            continue
        try:
            ind = akquant.RSI(period=p)
            ind.update_many(c)
            out[f"RSI{p}"] = _scalar(ind.value)
        except Exception as e:  # noqa: BLE001
            out[f"RSI{p}"] = None
    return out


def calc_kdj(high: pd.Series, low: pd.Series, close: pd.Series,
             n: int = 9, m1: int = 3, m2: int = 3) -> Dict[str, Optional[float]]:
    """
    KDJ 指标。akquant 无 KDJ，用 STOCH 算 K/D，再合成 J = 3K - 2D。
    K = STOCH_K (fast_k_period=n, slow_k_period=m1)
    D = STOCH_D (slow_d_period=m2)
    J = 3K - 2D
    """
    out: Dict[str, Optional[float]] = {"K": None, "D": None, "J": None}
    if len(close) < n + m1 + m2:
        return out
    h, l, c = _arr(high), _arr(low), _arr(close)
    try:
        # STOCH(fastk_period, slowk_period, slowd_period)
        ind = akquant.STOCH(fastk_period=n, slowk_period=m1, slowd_period=m2)
        for hh, ll, cc in zip(h, l, c):
            ind.update(float(hh), float(ll), float(cc))
        v = ind.value
        if isinstance(v, tuple) and len(v) >= 2:
            k_val = _scalar(v[0])
            d_val = _scalar(v[1])
            out["K"] = k_val
            out["D"] = d_val
            if k_val is not None and d_val is not None:
                out["J"] = 3 * k_val - 2 * d_val
    except Exception as e:  # noqa: BLE001
        logger.warning("KDJ failed: %s", e)
    return out


def calc_boll(close: pd.Series, period: int = 20, multiplier: float = 2.0
              ) -> Dict[str, Optional[float]]:
    """布林带"""
    out: Dict[str, Optional[float]] = {"BOLL_UP": None, "BOLL_MID": None, "BOLL_DN": None}
    if len(close) < period:
        return out
    c = _arr(close)
    try:
        ind = akquant.BollingerBands(period=period, multiplier=multiplier)
        ind.update_many(c)
        v = ind.value
        if isinstance(v, tuple) and len(v) >= 3:
            out["BOLL_UP"] = _scalar(v[0])
            out["BOLL_MID"] = _scalar(v[1])
            out["BOLL_DN"] = _scalar(v[2])
    except Exception as e:  # noqa: BLE001
        logger.warning("BollingerBands failed: %s", e)
    return out


def calc_willr(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = 14) -> Dict[str, Optional[float]]:
    """威廉指标 WR"""
    if len(close) < period:
        return {"WR14": None}
    h, l, c = _arr(high), _arr(low), _arr(close)
    try:
        ind = akquant.WILLR(period=period)
        for hh, ll, cc in zip(h, l, c):
            ind.update(float(hh), float(ll), float(cc))
        return {"WR14": _scalar(ind.value)}
    except Exception:  # noqa: BLE001
        return {"WR14": None}


def calc_cci(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> Dict[str, Optional[float]]:
    """CCI 顺势指标 (akquant.CCI 构造需要额外 c 参数, 用 pandas 自行计算)"""
    if len(close) < period:
        return {"CCI14": None}
    h, l, c = _arr(high), _arr(low), _arr(close)
    try:
        tp = (h + l + c) / 3.0  # typical price
        sma = pd.Series(tp).rolling(period).mean().to_numpy()
        md = pd.Series(tp).rolling(period).apply(
            lambda x: np.mean(np.abs(x - x.mean())), raw=True
        ).to_numpy()
        cci = (tp - sma) / (0.015 * md)
        cci = np.where(np.isinf(cci) | np.isnan(cci), np.nan, cci)
        val = float(cci[-1]) if not np.isnan(cci[-1]) else None
        return {"CCI14": val}
    except Exception:  # noqa: BLE001
        return {"CCI14": None}


# ===== 波动 / 趋势强度 =====
def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> Dict[str, Optional[float]]:
    """ATR (真实波幅)"""
    out: Dict[str, Optional[float]] = {"ATR14": None, "NATR14": None}
    if len(close) < period + 1:
        return out
    h, l, c = _arr(high), _arr(low), _arr(close)
    try:
        ind = akquant.ATR(period=period)
        # 优先用 update_many_hlc, 否则逐根
        if hasattr(ind, "update_many_hlc"):
            ind.update_many_hlc(h, l, c)
        else:
            for hh, ll, cc in zip(h, l, c):
                ind.update(float(hh), float(ll), float(cc))
        out["ATR14"] = _scalar(ind.value)
    except Exception as e:  # noqa: BLE001
        logger.warning("ATR failed: %s", e)
    # NATR = ATR / close * 100
    if out["ATR14"] is not None and c[-1] > 0:
        out["NATR14"] = round(out["ATR14"] / float(c[-1]) * 100, 3)
    return out


def calc_adx(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> Dict[str, Optional[float]]:
    """ADX 趋势强度 + +/-DI"""
    out: Dict[str, Optional[float]] = {"ADX14": None, "PLUS_DI": None, "MINUS_DI": None}
    if len(close) < period * 2:
        return out
    h, l, c = _arr(high), _arr(low), _arr(close)
    try:
        ind = akquant.ADX(period=period)
        for hh, ll, cc in zip(h, l, c):
            ind.update(float(hh), float(ll), float(cc))
        out["ADX14"] = _scalar(ind.value)
    except Exception as e:  # noqa: BLE001
        logger.warning("ADX failed: %s", e)
    try:
        ind = akquant.PLUS_DI(period=period)
        for hh, ll, cc in zip(h, l, c):
            ind.update(float(hh), float(ll), float(cc))
        out["PLUS_DI"] = _scalar(ind.value)
    except Exception:  # noqa: BLE001
        pass
    try:
        ind = akquant.MINUS_DI(period=period)
        for hh, ll, cc in zip(h, l, c):
            ind.update(float(hh), float(ll), float(cc))
        out["MINUS_DI"] = _scalar(ind.value)
    except Exception:  # noqa: BLE001
        pass
    return out


# ===== 量能 =====
def calc_obv(close: pd.Series, volume: pd.Series) -> Dict[str, Optional[float]]:
    """OBV 能量潮"""
    if len(close) < 2:
        return {"OBV": None, "OBV_MA10": None}
    c, v = _arr(close), _arr(volume)
    obv_series: Optional[np.ndarray] = None
    try:
        ind = akquant.OBV()
        for cc, vv in zip(c, v):
            ind.update(float(cc), float(vv))
        out = {"OBV": _scalar(ind.value), "OBV_MA10": None}
        return out
    except Exception:  # noqa: BLE001
        return {"OBV": None, "OBV_MA10": None}


def calc_mfi(high: pd.Series, low: pd.Series, close: pd.Series,
             volume: pd.Series, period: int = 14) -> Dict[str, Optional[float]]:
    """MFI 资金流量指标"""
    if len(close) < period + 1:
        return {"MFI14": None}
    h, l, c, v = _arr(high), _arr(low), _arr(close), _arr(volume)
    try:
        ind = akquant.MFI(period=period)
        for hh, ll, cc, vv in zip(h, l, c, v):
            ind.update(float(hh), float(ll), float(cc), float(vv))
        return {"MFI14": _scalar(ind.value)}
    except Exception:  # noqa: BLE001
        return {"MFI14": None}


# ===== 动量 =====
def calc_roc(close: pd.Series, period: int = 12) -> Dict[str, Optional[float]]:
    """ROC 变动率"""
    if len(close) < period + 1:
        return {"ROC12": None}
    c = _arr(close)
    try:
        ind = akquant.ROC(period=period)
        ind.update_many(c)
        return {"ROC12": _scalar(ind.value)}
    except Exception:  # noqa: BLE001
        return {"ROC12": None}


def calc_mom(close: pd.Series, period: int = 10) -> Dict[str, Optional[float]]:
    """MOM 动量"""
    if len(close) < period + 1:
        return {"MOM10": None}
    c = _arr(close)
    try:
        ind = akquant.MOM(period=period)
        ind.update_many(c)
        return {"MOM10": _scalar(ind.value)}
    except Exception:  # noqa: BLE001
        return {"MOM10": None}


def calc_trix(close: pd.Series, period: int = 14) -> Dict[str, Optional[float]]:
    """TRIX 三重指数平滑"""
    if len(close) < period * 4:
        return {"TRIX14": None}
    c = _arr(close)
    try:
        ind = akquant.TRIX(period=period)
        ind.update_many(c)
        return {"TRIX14": _scalar(ind.value)}
    except Exception:  # noqa: BLE001
        return {"TRIX14": None}


# ===== 主入口 =====
def compute_all(kline: pd.DataFrame) -> Dict[str, Dict[str, Optional[float]]]:
    """
    一次性计算所有指标，返回嵌套 dict。
    失败/数据不足的指标值为 None，不抛异常。
    """
    _ensure_akquant()  # 首次调用时延迟 import akquant (~500ms)
    if kline is None or kline.empty:
        return {}

    close = kline["close"].reset_index(drop=True)
    high = kline["high"].reset_index(drop=True)
    low = kline["low"].reset_index(drop=True)
    volume = kline["volume"].reset_index(drop=True)

    result: Dict[str, Dict[str, Optional[float]]] = {
        "trend": {
            **calc_ma(close),
            **calc_ema(close),
            **calc_macd(close),
        },
        "oscillator": {
            **calc_rsi(close),
            **calc_kdj(high, low, close),
            **calc_boll(close),
            **calc_willr(high, low, close),
            **calc_cci(high, low, close),
        },
        "volatility": {
            **calc_atr(high, low, close),
            **calc_adx(high, low, close),
        },
        "volume": {
            **calc_obv(close, volume),
            **calc_mfi(high, low, close, volume),
        },
        "momentum": {
            **calc_roc(close),
            **calc_mom(close),
            **calc_trix(close),
        },
    }

    # 计算价格相对均线的偏离度 (bias)
    bias: Dict[str, Optional[float]] = {}
    last = float(close.iloc[-1])
    for k, v in result["trend"].items():
        if k.startswith("MA") and k[2:].isdigit() and isinstance(v, (int, float)):
            n = int(k[2:])
            bias[f"BIAS{n}"] = round((last - v) / v * 100, 2) if v else None
    if bias:
        result["oscillator"].update(bias)

    return result
