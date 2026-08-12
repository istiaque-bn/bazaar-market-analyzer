"""Technical indicator calculations on OHLC DataFrames."""
from __future__ import annotations

import numpy as np
import pandas as pd


def prices_to_df(price_qs, *, exclude_quality_flags: set[str] | None = None) -> pd.DataFrame:
    """exclude_quality_flags drops bars carrying any of the given
    market.services.data_quality flag codes before they reach a caller —
    used by the ML training panels (see data_quality.TRAINING_EXCLUDE_FLAGS)
    so a bad tick can't teach a model. Left unset (the default) for display
    paths, which show all data including flagged rows, just labeled."""
    cols = ["date", "open", "high", "low", "close", "volume", "value"]
    if exclude_quality_flags:
        cols.append("quality_flags")
    rows = list(price_qs.order_by("date").values(*cols))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if exclude_quality_flags:
        bad = df["quality_flags"].apply(lambda flags: bool(set(flags or []) & exclude_quality_flags))
        df = df[~bad].drop(columns=["quality_flags"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "volume", "value"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["close"]).reset_index(drop=True)


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(1, window // 2)).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    return series.ewm(span=window, adjust=False).mean()


def rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    # avg_loss == 0: no losses in the window, so RS is undefined rather than
    # "no signal" — RSI is 100 if there were gains, or 50 if the price was flat.
    result = result.where(avg_loss != 0, np.where(avg_gain > 0, 100, 50))
    return result


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def bollinger(series: pd.Series, window: int = 20, num_std: float = 2.0):
    mid = sma(series, window)
    std = series.rolling(window, min_periods=max(1, window // 2)).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=1).mean()


def support_resistance(df: pd.DataFrame, lookback: int = 40) -> tuple[float | None, float | None]:
    if df.empty:
        return None, None
    window = df.tail(lookback)
    return float(window["low"].min()), float(window["high"].max())


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 5:
        return df
    out = df.copy()
    out["sma_20"] = sma(out["close"], 20)
    out["sma_50"] = sma(out["close"], 50)
    out["sma_200"] = sma(out["close"], 200)
    out["ema_12"] = ema(out["close"], 12)
    out["ema_26"] = ema(out["close"], 26)
    out["rsi_14"] = rsi(out["close"], 14)
    macd_line, macd_sig, macd_hist = macd(out["close"])
    out["macd"] = macd_line
    out["macd_signal"] = macd_sig
    out["macd_hist"] = macd_hist
    bb_u, bb_m, bb_l = bollinger(out["close"])
    out["bb_upper"] = bb_u
    out["bb_middle"] = bb_m
    out["bb_lower"] = bb_l
    out["atr_14"] = atr(out)
    out["volume_sma_20"] = sma(out["volume"].astype(float), 20)
    out["return_1d"] = out["close"].pct_change()
    out["return_5d"] = out["close"].pct_change(5)
    out["return_20d"] = out["close"].pct_change(20)
    out["volatility_20"] = out["return_1d"].rolling(20).std() * np.sqrt(252)
    return out


def volatility_regime(vol: pd.Series) -> pd.Series:
    """Where a stock's own current volatility_20 ranks against its own
    trailing ~1y distribution: 0=low, 1=mid, 2=high vol regime. Backward-
    looking rolling quantiles only, so a row never sees its own future
    volatility. NaN (not enough history yet to rank) is preserved rather
    than guessed at, for the caller to default or drop as appropriate."""
    p33 = vol.rolling(252, min_periods=60).quantile(0.33)
    p67 = vol.rolling(252, min_periods=60).quantile(0.67)
    regime = pd.Series(np.select([vol <= p33, vol <= p67], [0, 1], default=2), index=vol.index, dtype=float)
    regime[p33.isna()] = np.nan
    return regime


def latest_indicator_row(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    enriched = compute_indicators(df)
    row = enriched.iloc[-1]
    support, resistance = support_resistance(enriched)
    result = {}
    for key in (
        "sma_20",
        "sma_50",
        "sma_200",
        "ema_12",
        "ema_26",
        "rsi_14",
        "macd",
        "macd_signal",
        "macd_hist",
        "bb_upper",
        "bb_middle",
        "bb_lower",
        "atr_14",
        "volume_sma_20",
        "return_5d",
        "return_20d",
        "volatility_20",
        "close",
        "volume",
    ):
        val = row.get(key)
        result[key] = None if pd.isna(val) else float(val)
    result["support"] = support
    result["resistance"] = resistance
    result["date"] = row["date"].date() if hasattr(row["date"], "date") else row["date"]
    return result
