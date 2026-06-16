#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py  (v2 - profi simulacia obchodov)
--------------------------------------------
Pre kazdy historicky signal NASIMULUJE obchod: vstup na signale, ATR stop,
ciel podla R:R. Prejde sviecky dopredu a zisti, ci sa skor zasiahol STOP alebo CIEL.
Vysledok meria v R-nasobkoch (vyhra = +R:R, prehra = -1). Zapocita aj naklady (spread).

Metriky: pocet obchodov, uspesnost, priemer R (expectancy), profit factor,
celkove R, max. prepad (drawdown), najlepsi/najhorsi obchod - pre kazdy typ aj spolu.

Spustenie:  python backtest.py
Vystup:     tabulka do konzoly + zhrnutie na Telegram (ak su nastavene secrets a BT_TELEGRAM=1).

POZOR: historicka uspesnost NIE JE zaruka buducnosti. Data su z yfinance (mierne
oneskorene), naklady su odhad, a vnutri sviecky predpokladame zasah STOPU skor (konzervativne).
"""

import os
import market_scanner as ms

# --- parametre backtestu ---
BT_PERIOD    = "30d"     # historia
BT_INTERVAL  = "5m"
MAX_HOLD     = 48        # max drzanie obchodu (48 x 5m = 4 h)
COST_PCT     = 0.03      # odhad nakladov na obchod (spread+poplatok), v %
APPLY_HTF    = True      # filter trendu (proxy: dlha EMA na 5m)
HTF_EMA_BARS = 200       # dlha EMA ako proxy vyssieho trendu

E_F, E_S = ms.EMA_FAST, ms.EMA_SLOW
RB, RAM, RPM = ms.RAPID_BARS, ms.RAPID_ATR_MULT, ms.RAPID_PCT_MIN
LB = ms.BREAKOUT_LOOKBACK
ROB, ROS = ms.RSI_OVERBOUGHT, ms.RSI_OVERSOLD
RP, AP = ms.RSI_PERIOD, ms.ATR_PERIOD
SAM, TRR = ms.STOP_ATR_MULT, ms.TARGET_RR


def atr_series(highs, lows, closes, period):
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, n)]
    a = sum(trs[:period]) / period
    out[period] = a
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out[i + 1] = a
    return out


def signals_at(i, closes, highs, lows, ef, es, rsi_a, atr_a):
    sigs = []
    av = atr_a[i]
    if av and i >= RB + 1:
        def rap(j):
            b = closes[j - RB]
            if not b:
                return 0
            m = closes[j] - b
            if abs(m) >= RAM * av and abs(m / b * 100) >= RPM:
                return 1 if m > 0 else -1
            return 0
        cur = rap(i)
        if cur and cur != rap(i - 1):
            sigs.append(("rapid", "up" if cur > 0 else "down"))
    if i >= 1 and ef[i - 1] is not None:
        if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
            sigs.append(("trend_up", "up"))
        elif ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
            sigs.append(("trend_down", "down"))
    if i >= LB + 2:
        wh = max(highs[i - 1 - LB:i - 1])
        wl = min(lows[i - 1 - LB:i - 1])
        if closes[i - 1] <= wh and closes[i] > wh:
            sigs.append(("breakout_up", "up"))
        elif closes[i - 1] >= wl and closes[i] < wl:
            sigs.append(("breakout_down", "down"))
    if rsi_a[i] is not None and rsi_a[i - 1] is not None:
        if rsi_a[i - 1] < ROB and rsi_a[i] >= ROB:
            sigs.append(("rsi_overbought", "down"))
        elif rsi_a[i - 1] > ROS and rsi_a[i] <= ROS:
            sigs.append(("rsi_oversold", "up"))
    return sigs


def simulate(i, direction, closes, highs, lows, atr_a):
    av = atr_a[i]
    if not av:
        return None
    risk = SAM * av
    entry = closes[i]
    if direction == "up":
        stop, target = entry - risk, entry + TRR * risk
    else:
        stop, target = entry + risk, entry - TRR * risk
    n = len(closes)
    end = min(i + MAX_HOLD, n - 1)
    r = None
    for j in range(i + 1, end + 1):
        if direction == "up":
            if lows[j] <= stop:
                r = -1.0; break
            if highs[j] >= target:
                r = TRR; break
        else:
            if highs[j] >= stop:
                r = -1.0; break
            if lows[j] <= target:
                r = TRR; break
    if r is None:                      # ani stop ani ciel -> zatvor na trhu
        last = closes[end]
        r = (last - entry) / risk if direction == "up" else (entry - last) / risk
    cost_r = (COST_PCT / 100.0 * entry) / risk
    return r - cost_r


def backtest_instrument(name, df):
    closes = list(df["Close"].dropna().values)
    highs = list(df["High"].dropna().values)
    lows = list(df["Low"].dropna().values)
    if len(closes) < max(E_S, LB, AP, HTF_EMA_BARS) + 5:
        return {}
    ef = ms.ema(closes, E_F)
    es = ms.ema(closes, E_S)
    rsi_a = ms.rsi(closes, RP)
    atr_a = atr_series(highs, lows, closes, AP)
    htf = ms.ema(closes, HTF_EMA_BARS)
    start = max(E_S + 2, LB + 2, AP + 2, HTF_EMA_BARS)
    results = {}   # typ -> list of R
    for i in range(start, len(closes) - MAX_HOLD):
        for typ, d in signals_at(i, closes, highs, lows, ef, es, rsi_a, atr_a):
            if APPLY_HTF:
                trend = "up" if closes[i] >= htf[i] else "down"
                if d != trend:
                    continue
            r = simulate(i, d, closes, highs, lows, atr_a)
            if r is not None:
                results.setdefault(typ, []).append(r)
    return results


def stats(rs):
    n = len(rs)
    if not n:
        return None
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gp = sum(wins)
    gl = -sum(losses)
    pf = (gp / gl) if gl > 0 else float('inf')
    # equity + max drawdown (v R)
    eq = 0.0; peak = 0.0; dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": n, "win": len(wins), "wr": len(wins) / n * 100,
            "exp": sum(rs) / n, "pf": pf, "totR": sum(rs),
            "dd": dd, "best": max(rs), "worst": min(rs)}


def main():
    print("=" * 70)
    print(f" BACKTEST v2 - simulacia obchodov | historia {BT_PERIOD} | "
          f"stop {SAM}xATR | ciel {TRR}R | max drzanie {MAX_HOLD} sviecok")
    print(f" HTF filter: {APPLY_HTF} | naklady/obchod: {COST_PCT} %")
    print("=" * 70)
    total = {}
    for name, ticker in ms.INSTRUMENTS.items():
        try:
            df = ms.yf.Ticker(ticker).history(period=BT_PERIOD, interval=BT_INTERVAL)
        except Exception as e:
            print(f"  {name}: chyba {e}"); continue
        if df is None or df.empty or len(df) < 200:
            print(f"  {name}: malo dat"); continue
        res = backtest_instrument(name, df)
        print(f"\n{name}:")
        if not res:
            print("   ziadne obchody")
        for typ in sorted(res):
            st = stats(res[typ])
            print(f"   {typ:16s} n={st['n']:4d}  uspesnost={st['wr']:5.1f}%  "
                  f"exp={st['exp']:+.2f}R  PF={st['pf']:.2f}  spolu={st['totR']:+.1f}R")
            total.setdefault(typ, []).extend(res[typ])

    print("\n" + "=" * 70)
    print(" SPOLU (vsetky instrumenty):")
    all_rs = []
    tg_lines = []
    for typ in sorted(total):
        st = stats(total[typ])
        all_rs.extend(total[typ])
        print(f"   {typ:16s} n={st['n']:4d}  uspesnost={st['wr']:5.1f}%  exp={st['exp']:+.2f}R  "
              f"PF={st['pf']:.2f}  spolu={st['totR']:+.1f}R  maxDD={st['dd']:.1f}R")
        tg_lines.append(f"{ms.TYPE_LABEL.get(typ, typ)}: {st['wr']:.0f}% · exp {st['exp']:+.2f}R · "
                        f"PF {st['pf']:.2f} · {st['totR']:+.0f}R (n={st['n']})")
    ov = stats(all_rs)
    if ov:
        print("\n" + "-" * 70)
        print(f" CELKOVO: {ov['n']} obchodov | uspesnost {ov['wr']:.1f}% | "
              f"expectancy {ov['exp']:+.3f}R | PF {ov['pf']:.2f} | "
              f"spolu {ov['totR']:+.1f}R | max prepad {ov['dd']:.1f}R")
    print("=" * 70)
    print("Pozn.: R = nasobok rizika. exp(ectancy) = priemerny zisk/strata na obchod v R.")
    print("       PF = profit factor (>1 = ziskove). Historia nie je zaruka buducnosti.")

    if os.environ.get("BT_TELEGRAM") and ov:
        msg = ["📊 <b>Backtest – simulácia obchodov</b>",
               f"História {BT_PERIOD} · stop {SAM}×ATR · cieľ {TRR}R · náklady {COST_PCT}%",
               f"<b>Celkovo: {ov['n']} obchodov · úspešnosť {ov['wr']:.0f}% · "
               f"expectancy {ov['exp']:+.2f}R · PF {ov['pf']:.2f} · {ov['totR']:+.0f}R</b>",
               "", "Podľa typu:"] + ["• " + l for l in tg_lines]
        msg.append("\nℹ R = násobok rizika; PF>1 = ziskové. Orientačné, nie reálny obchod.")
        ms.send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
