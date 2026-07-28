"""Rule-based pattern detection for Bangladesh equities."""
from __future__ import annotations

import pandas as pd

from market.services.indicators import compute_indicators


def detect_patterns(df: pd.DataFrame) -> list[dict]:
    if df is None or len(df) < 30:
        return []
    data = compute_indicators(df)
    i = len(data) - 1
    row = data.iloc[i]
    prev = data.iloc[i - 1]
    patterns: list[dict] = []

    def add(name: str, direction: str, strength: float, description: str):
        patterns.append(
            {
                "name": name,
                "direction": direction,
                "strength": round(max(0.0, min(1.0, strength)), 3),
                "description": description,
            }
        )

    # Golden / death cross (SMA50 vs SMA200)
    if pd.notna(row.get("sma_50")) and pd.notna(row.get("sma_200")) and pd.notna(prev.get("sma_50")):
        if prev["sma_50"] <= prev["sma_200"] and row["sma_50"] > row["sma_200"]:
            add("Golden Cross", "bullish", 0.85, "SMA50 crossed above SMA200 — longer-term bullish shift.")
        elif prev["sma_50"] >= prev["sma_200"] and row["sma_50"] < row["sma_200"]:
            add("Death Cross", "bearish", 0.85, "SMA50 crossed below SMA200 — longer-term bearish shift.")

    # RSI extremes
    rsi = row.get("rsi_14")
    if pd.notna(rsi):
        if rsi < 30:
            add("RSI Oversold", "bullish", min(1.0, (30 - rsi) / 30 + 0.5), f"RSI at {rsi:.1f} — historically oversold zone.")
        elif rsi > 70:
            add("RSI Overbought", "bearish", min(1.0, (rsi - 70) / 30 + 0.5), f"RSI at {rsi:.1f} — historically overbought zone.")

    # MACD cross
    if pd.notna(row.get("macd")) and pd.notna(row.get("macd_signal")):
        if prev["macd"] <= prev["macd_signal"] and row["macd"] > row["macd_signal"]:
            add("MACD Bullish Cross", "bullish", 0.7, "MACD crossed above signal line.")
        elif prev["macd"] >= prev["macd_signal"] and row["macd"] < row["macd_signal"]:
            add("MACD Bearish Cross", "bearish", 0.7, "MACD crossed below signal line.")

    # Bollinger bounce / break
    if pd.notna(row.get("bb_lower")) and pd.notna(row.get("bb_upper")):
        close = row["close"]
        if close <= row["bb_lower"]:
            add("BB Lower Touch", "bullish", 0.65, "Price at/below lower Bollinger Band — mean-reversion candidate.")
        elif close >= row["bb_upper"]:
            add("BB Upper Touch", "bearish", 0.65, "Price at/above upper Bollinger Band — stretched move.")

    # Volume spike
    if pd.notna(row.get("volume_sma_20")) and row["volume_sma_20"] > 0:
        ratio = float(row["volume"]) / float(row["volume_sma_20"])
        if ratio >= 2.0:
            direction = "bullish" if row["close"] >= prev["close"] else "bearish"
            add("Volume Spike", direction, min(1.0, ratio / 4), f"Volume {ratio:.1f}x 20-day average.")

    # Short-term MA trend
    if pd.notna(row.get("sma_20")) and pd.notna(row.get("sma_50")):
        if row["close"] > row["sma_20"] > row["sma_50"]:
            add("Uptrend Stack", "bullish", 0.55, "Price > SMA20 > SMA50 — short-term uptrend structure.")
        elif row["close"] < row["sma_20"] < row["sma_50"]:
            add("Downtrend Stack", "bearish", 0.55, "Price < SMA20 < SMA50 — short-term downtrend structure.")

    # Near support / resistance
    support = data.tail(40)["low"].min()
    resistance = data.tail(40)["high"].max()
    close = row["close"]
    near_support = bool(support) and close <= support * 1.02
    near_resistance = bool(resistance) and close >= resistance * 0.98
    if near_support and near_resistance:
        # Range is tight enough that both thresholds overlap — tag whichever boundary is closer.
        if (close - support) <= (resistance - close):
            add("Near Support", "bullish", 0.5, f"Trading near recent support (~{support:.2f}).")
        else:
            add("Near Resistance", "bearish", 0.5, f"Trading near recent resistance (~{resistance:.2f}).")
    elif near_support:
        add("Near Support", "bullish", 0.5, f"Trading near recent support (~{support:.2f}).")
    elif near_resistance:
        add("Near Resistance", "bearish", 0.5, f"Trading near recent resistance (~{resistance:.2f}).")

    return patterns
