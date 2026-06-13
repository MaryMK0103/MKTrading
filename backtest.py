#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py
-----------
Prehra historiu a vyhodnoti, ako casto po jednotlivych typoch signalov cena
naozaj pokracovala v ocakavanom smere. Pomoze ti zistit, KTORYM signalom verit.

Spustenie:  python backtest.py
Vystup:     tabulka uspesnosti do konzoly (a na Telegram, ak su nastavene secrets).

POZOR: Historicka uspesnost NIE JE zaruka buducich vysledkov. Sluzi len na to,
aby si videla, ktore signaly maju na danom trhu zmysel a ktore su sum.
"""

import os
import market_scanner as ms   # znovu pouzijeme detekciu zo skenera

# kolko sviecok dopredu hodnotime vysledok
HORIZON_BARS = 12          # 12 x 5m = 1 hodina
# historia na test
BT_PERIOD   = "30d"
BT_INTERVAL = "5m"


def evaluate(name, df):
    """Prejde historiu sviecu po sviecke a zmeria vysledok po HORIZON_BARS."""
    import pandas as pd
    closes = list(df["Close"].dropna().values)
    highs  = list(df["High"].dropna().values)
    lows   = list(df["Low"].dropna().values)
    n = len(closes)
    results = {}   # typ -> [pocet, uspechy, sucet_pohybu_%]

    start = max(ms.EMA_SLOW + 2, ms.BREAKOUT_LOOKBACK + 2, ms.ATR_PERIOD + 2)
    for i in range(start, n - HORIZON_BARS):
        sub = df.iloc[:i + 1]
        atr_val = ms.atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], ms.ATR_PERIOD)
        sigs = ms.detect_signals(name, sub, atr_val)
        if not sigs:
            continue
        entry = closes[i]
        future = closes[i + HORIZON_BARS]
        for s in sigs:
            typ = s["typ"]; d = s["dir"]
            move_pct = (future - entry) / entry * 100.0
            good = (move_pct > 0) if d == "up" else (move_pct < 0)
            r = results.setdefault(typ, [0, 0, 0.0])
            r[0] += 1
            r[1] += 1 if good else 0
            r[2] += move_pct if d == "up" else -move_pct  # pohyb v smere signalu
    return results


def main():
    print("=" * 64)
    print(f" BACKTEST - historia {BT_PERIOD}, horizont {HORIZON_BARS} sviecok "
          f"({HORIZON_BARS*int(BT_INTERVAL.rstrip('m'))} min)")
    print("=" * 64)

    total = {}
    lines = []
    for name, ticker in ms.INSTRUMENTS.items():
        try:
            df = ms.yf.Ticker(ticker).history(period=BT_PERIOD, interval=BT_INTERVAL)
        except Exception as e:
            print(f"  {name}: chyba {e}"); continue
        if df is None or df.empty or len(df) < 60:
            print(f"  {name}: malo dat"); continue
        res = evaluate(name, df)
        print(f"\n{name}:")
        if not res:
            print("   ziadne signaly v historii")
        for typ, (cnt, win, summ) in sorted(res.items()):
            wr = win / cnt * 100 if cnt else 0
            avg = summ / cnt if cnt else 0
            print(f"   {typ:16s}  n={cnt:4d}  uspesnost={wr:5.1f}%  priemer={avg:+.2f}%")
            t = total.setdefault(typ, [0, 0, 0.0])
            t[0] += cnt; t[1] += win; t[2] += summ

    print("\n" + "=" * 64)
    print(" SPOLU (vsetky instrumenty):")
    for typ, (cnt, win, summ) in sorted(total.items()):
        wr = win / cnt * 100 if cnt else 0
        avg = summ / cnt if cnt else 0
        line = f"   {typ:16s}  n={cnt:4d}  uspesnost={wr:5.1f}%  priemer={avg:+.2f}%"
        print(line); lines.append(f"{typ}: {wr:.0f}% (n={cnt}, prie {avg:+.2f}%)")
    print("=" * 64)
    print("Pozn.: 'uspesnost' = ako casto cena po signali isla spravnym smerom.")
    print("       50 % = nahoda. Historia nie je zaruka buducnosti.")

    # volitelne posli zhrnutie na Telegram
    if os.environ.get("BT_TELEGRAM") and lines:
        ms.send_telegram("\U0001F4CA <b>Backtest – úspešnosť signálov</b>\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
