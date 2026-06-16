#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py  (v4 - optimalizator + nove indikatory + profi metriky)
-------------------------------------------------------------------
Okrem povodnych signalov (rapid, trend, breakout, RSI) testuje aj nove strategie:
  * Supertrend (flip smeru) - trend-following
  * Bollinger reversia (navrat z pasma) - mean reversion
  * Bollinger breakout (preraz pasma) - momentum
  * ADX-potvrdeny trend (EMA kriz + silny ADX)
Vsetky indikatory su naprogramovane priamo (ziadna krehka zavislost).

Pre kazdy typ hlada najlepsie parametre (stop/ciel/drzanie/smer) POCTIVO:
optimalizuje na prvych 70 % historie a overuje na zvysnych 30 % (out-of-sample).
Metriky: uspesnost, expectancy (R), profit factor, Sharpe, Sortino, max drawdown.

Spustenie: python backtest.py   |   Vystup: konzola + Telegram zhrnutie.
POZOR: historia != buducnost. Yahoo data, naklady odhad, vnutri sviecky stop skor.
"""

import os
import math
import statistics
import itertools
from collections import defaultdict
import market_scanner as ms

OPT_PERIOD = "60d"
BT_INTERVAL = "5m"
COST_PCT = 0.03
SPLIT = 0.70
MIN_N_IN = 40
STOPS = [1.0, 1.5, 2.0, 2.5]
TARGETS = [1.0, 1.5, 2.0, 3.0]
HOLDS = [48, 96]
MODES = ["with", "counter"]

E_F, E_S = ms.EMA_FAST, ms.EMA_SLOW
RB, RAM, RPM = ms.RAPID_BARS, ms.RAPID_ATR_MULT, ms.RAPID_PCT_MIN
LB = ms.BREAKOUT_LOOKBACK
ROB, ROS = ms.RSI_OVERBOUGHT, ms.RSI_OVERSOLD
RP, AP = ms.RSI_PERIOD, ms.ATR_PERIOD
HTF_EMA_BARS = 200
BB_LEN, BB_MULT = 20, 2.0
ADX_LEN, ADX_MIN = 14, 25
ST_LEN, ST_MULT = 10, 3.0


# ---------- indikatory (cisty python) ----------
def sma(v, n):
    out = [None] * len(v)
    for i in range(n - 1, len(v)):
        out[i] = sum(v[i - n + 1:i + 1]) / n
    return out


def bollinger(closes, length, mult):
    mid = sma(closes, length)
    up = [None] * len(closes); lo = [None] * len(closes)
    for i in range(length - 1, len(closes)):
        m = mid[i]
        var = sum((closes[j] - m) ** 2 for j in range(i - length + 1, i + 1)) / length
        sd = math.sqrt(var)
        up[i] = m + mult * sd; lo[i] = m - mult * sd
    return up, lo


def _trs(h, l, c):
    return [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(c))]


def adx(h, l, c, length):
    n = len(c); out = [None] * n
    if n < 2 * length + 2:
        return out
    tr = _trs(h, l, c)
    pdm = []; mdm = []
    for i in range(1, n):
        up = h[i] - h[i - 1]; dn = l[i - 1] - l[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    atr = sum(tr[:length]); p = sum(pdm[:length]); m = sum(mdm[:length])
    dx = []
    for k in range(length, len(tr)):
        atr = atr - atr / length + tr[k]
        p = p - p / length + pdm[k]
        m = m - m / length + mdm[k]
        pdi = 100 * p / atr if atr else 0
        mdi = 100 * m / atr if atr else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
    if len(dx) >= length:
        a = sum(dx[:length]) / length
        out[2 * length] = a
        for k in range(length, len(dx)):
            a = (a * (length - 1) + dx[k]) / length
            out[k + 1 + length] = a
    return out


def supertrend(h, l, c, length, mult):
    n = len(c); tr = _trs(h, l, c)
    atr = [None] * n
    if len(tr) >= length:
        a = sum(tr[:length]) / length; atr[length] = a
        for i in range(length, len(tr)):
            a = (a * (length - 1) + tr[i]) / length; atr[i + 1] = a
    direction = [None] * n
    fub = flb = None; prev = 1
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (h[i] + l[i]) / 2
        bub = hl2 + mult * atr[i]; blb = hl2 - mult * atr[i]
        if fub is None:
            fub, flb = bub, blb; direction[i] = 1; prev = 1; continue
        fub = bub if (bub < fub or c[i - 1] > fub) else fub
        flb = blb if (blb > flb or c[i - 1] < flb) else flb
        d = 1 if c[i] > fub else (-1 if c[i] < flb else prev)
        direction[i] = d; prev = d
    return direction


def atr_series(highs, lows, closes, period):
    n = len(closes); out = [None] * n
    if n < period + 1:
        return out
    tr = _trs(highs, lows, closes)
    a = sum(tr[:period]) / period; out[period] = a
    for i in range(period, len(tr)):
        a = (a * (period - 1) + tr[i]) / period; out[i + 1] = a
    return out


# ---------- detekcia signalov (povodne + nove) ----------
def signals_at(i, c, h, l, ef, es, rsi_a, atr_a, bbu, bbl, adx_a, st):
    sigs = []
    av = atr_a[i]
    if av and i >= RB + 1:
        def rap(j):
            b = c[j - RB]
            if not b:
                return 0
            mm = c[j] - b
            if abs(mm) >= RAM * av and abs(mm / b * 100) >= RPM:
                return 1 if mm > 0 else -1
            return 0
        cur = rap(i)
        if cur and cur != rap(i - 1):
            sigs.append(("rapid", "up" if cur > 0 else "down"))
    if ef[i - 1] is not None:
        cross_up = ef[i - 1] <= es[i - 1] and ef[i] > es[i]
        cross_dn = ef[i - 1] >= es[i - 1] and ef[i] < es[i]
        if cross_up:
            sigs.append(("trend_up", "up"))
        elif cross_dn:
            sigs.append(("trend_down", "down"))
        # ADX-potvrdeny trend
        if adx_a[i] is not None and adx_a[i] >= ADX_MIN:
            if cross_up:
                sigs.append(("adx_trend_up", "up"))
            elif cross_dn:
                sigs.append(("adx_trend_down", "down"))
    if i >= LB + 2:
        wh = max(h[i - 1 - LB:i - 1]); wl = min(l[i - 1 - LB:i - 1])
        if c[i - 1] <= wh and c[i] > wh:
            sigs.append(("breakout_up", "up"))
        elif c[i - 1] >= wl and c[i] < wl:
            sigs.append(("breakout_down", "down"))
    if rsi_a[i] is not None and rsi_a[i - 1] is not None:
        if rsi_a[i - 1] < ROB and rsi_a[i] >= ROB:
            sigs.append(("rsi_overbought", "down"))
        elif rsi_a[i - 1] > ROS and rsi_a[i] <= ROS:
            sigs.append(("rsi_oversold", "up"))
    # Bollinger
    if bbu[i] is not None and bbu[i - 1] is not None:
        if c[i - 1] <= bbl[i - 1] and c[i] > bbl[i]:
            sigs.append(("bb_revert_up", "up"))
        elif c[i - 1] >= bbu[i - 1] and c[i] < bbu[i]:
            sigs.append(("bb_revert_down", "down"))
        if c[i - 1] <= bbu[i - 1] and c[i] > bbu[i]:
            sigs.append(("bb_breakout_up", "up"))
        elif c[i - 1] >= bbl[i - 1] and c[i] < bbl[i]:
            sigs.append(("bb_breakout_down", "down"))
    # Supertrend flip
    if st[i] is not None and st[i - 1] is not None and st[i] != st[i - 1]:
        sigs.append(("supertrend_up" if st[i] == 1 else "supertrend_down",
                     "up" if st[i] == 1 else "down"))
    return sigs


def sim(i, direction, c, h, l, av, stop_mult, target_rr, max_hold):
    if not av:
        return None
    risk = stop_mult * av; entry = c[i]
    if direction == "up":
        stop, target = entry - risk, entry + target_rr * risk
    else:
        stop, target = entry + risk, entry - target_rr * risk
    n = len(c); end = min(i + max_hold, n - 1); r = None
    for j in range(i + 1, end + 1):
        if direction == "up":
            if l[j] <= stop:
                r = -1.0; break
            if h[j] >= target:
                r = target_rr; break
        else:
            if h[j] >= stop:
                r = -1.0; break
            if l[j] <= target:
                r = target_rr; break
    if r is None:
        last = c[end]
        r = (last - entry) / risk if direction == "up" else (entry - last) / risk
    return r - (COST_PCT / 100.0 * entry) / risk


def stats(rs):
    n = len(rs)
    if not n:
        return None
    wins = [r for r in rs if r > 0]
    gp = sum(wins); gl = -sum(r for r in rs if r <= 0)
    pf = (gp / gl) if gl > 0 else float('inf')
    mean = sum(rs) / n
    sd = statistics.pstdev(rs) if n > 1 else 0
    dsd = statistics.pstdev([min(r, 0) for r in rs]) if n > 1 else 0
    eq = peak = dd = 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = min(dd, eq - peak)
    return {"n": n, "wr": len(wins) / n * 100, "exp": mean, "pf": pf, "totR": sum(rs),
            "sharpe": (mean / sd if sd > 0 else 0), "sortino": (mean / dsd if dsd > 0 else 0),
            "dd": dd}


def prep(name, df):
    c = list(df["Close"].dropna().values)
    h = list(df["High"].dropna().values)
    l = list(df["Low"].dropna().values)
    if len(c) < HTF_EMA_BARS + 60:
        return None
    ef = ms.ema(c, E_F); es = ms.ema(c, E_S)
    rsi_a = ms.rsi(c, RP); atr_a = atr_series(h, l, c, AP)
    htf = ms.ema(c, HTF_EMA_BARS)
    bbu, bbl = bollinger(c, BB_LEN, BB_MULT)
    adx_a = adx(h, l, c, ADX_LEN)
    st = supertrend(h, l, c, ST_LEN, ST_MULT)
    start = max(E_S + 2, LB + 2, AP + 2, HTF_EMA_BARS, BB_LEN + 2, 2 * ADX_LEN + 2)
    n = len(c); split_i = int(n * SPLIT)
    sgs = []
    for i in range(start, n - max(HOLDS)):
        trend = "up" if c[i] >= htf[i] else "down"
        for typ, d in signals_at(i, c, h, l, ef, es, rsi_a, atr_a, bbu, bbl, adx_a, st):
            sgs.append((i, typ, d, trend, i < split_i))
    return {"c": c, "h": h, "l": l, "atr": atr_a, "sgs": sgs}


def main():
    print("=" * 74)
    print(f" OPTIMALIZACIA v4 | historia {OPT_PERIOD} | in {int(SPLIT*100)}% / out {100-int(SPLIT*100)}%")
    print(f" Strategie: rapid, trend, breakout, RSI, +Supertrend, +Bollinger, +ADX-trend")
    print(f" Sweep: stop={STOPS} ciel={TARGETS}R drz={HOLDS} smer={MODES}")
    print("=" * 74)
    data = []
    for name, ticker in ms.INSTRUMENTS.items():
        try:
            df = ms.yf.Ticker(ticker).history(period=OPT_PERIOD, interval=BT_INTERVAL)
        except Exception as e:
            print(f"  {name}: chyba {e}"); continue
        if df is None or df.empty or len(df) < 300:
            continue
        p = prep(name, df)
        if p:
            data.append(p)
    print(f" Nacitanych {len(data)} instrumentov.\n")

    combos = list(itertools.product(STOPS, TARGETS, HOLDS, MODES))
    results = {c: defaultdict(lambda: {"in": [], "out": []}) for c in combos}
    for p in data:
        c_, h_, l_, a_, sgs = p["c"], p["h"], p["l"], p["atr"], p["sgs"]
        for combo in combos:
            sm, tr, mh, mode = combo
            b = results[combo]
            for (i, typ, d, trend, is_in) in sgs:
                if mode == "with" and d != trend:
                    continue
                if mode == "counter" and d == trend:
                    continue
                r = sim(i, d, c_, h_, l_, a_[i], sm, tr, mh)
                if r is not None:
                    b[typ]["in" if is_in else "out"].append(r)

    types = sorted({t for c in combos for t in results[c].keys()})
    tg = []
    print(" NAJLEPSIA KOMBINACIA na typ (in-sample) + overenie out-of-sample:\n")
    for typ in types:
        best = None
        for c in combos:
            din = results[c][typ]["in"]
            if len(din) < MIN_N_IN:
                continue
            si = stats(din)
            if best is None or si["exp"] > best[1]["exp"]:
                best = (c, si)
        label = ms.TYPE_LABEL.get(typ, typ)
        if not best:
            print(f"  {label:16s} (malo dat)"); continue
        c, si = best
        so = stats(results[c][typ]["out"])
        sm, tr, mh, mode = c
        robust = bool(so and so["exp"] > 0 and si["exp"] > 0)
        mark = "✅ DRŽÍ" if robust else "⚠️ len IN"
        out_txt = (f"OUT exp={so['exp']:+.2f}R PF={so['pf']:.2f} Sharpe={so['sharpe']:+.2f} "
                   f"({so['wr']:.0f}%, n={so['n']})" if so else "OUT: malo dat")
        print(f"  {label:16s} stop {sm}×ATR · ciel {tr}R · drz {mh} · "
              f"{'trend' if mode=='with' else 'fade'}")
        print(f"      IN exp={si['exp']:+.2f}R PF={si['pf']:.2f} Sharpe={si['sharpe']:+.2f} "
              f"({si['wr']:.0f}%, n={si['n']})  |  {out_txt}  → {mark}")
        if robust:
            tg.append(f"✅ {label}: stop {sm}×ATR, ciel {tr}R, "
                      f"{'trend' if mode=='with' else 'fade'} → out {so['exp']:+.2f}R "
                      f"PF {so['pf']:.2f} Sharpe {so['sharpe']:+.2f} ({so['wr']:.0f}%)")

    print("\n" + "=" * 74)
    if tg:
        print(" ROBUSTNE (ziskove aj out-of-sample):")
        for t in tg:
            print("   " + t)
    else:
        print(" Ziadna kombinacia nedrzala ziskovo aj out-of-sample.")
    print("=" * 74)
    print("Pozn.: 'DRZI' = kladna expectancy IN aj OUT. Sharpe>0 = lepsi pomer zisk/kolisanie.")
    print("       Aj tak ber s rezervou - minulost != buducnost.")

    if os.environ.get("BT_TELEGRAM"):
        if tg:
            msg = ["🔬 <b>Optimalizácia v4 – robustné stratégie</b>",
                   f"(ziskové aj na overovacej vzorke, {OPT_PERIOD})", ""] + tg
            msg.append("\nℹ overené out-of-sample. Orientačné, nie reálny obchod.")
        else:
            msg = ["🔬 <b>Optimalizácia v4</b>",
                   "Ani s novými indikátormi (Supertrend, Bollinger, ADX) nebola žiadna",
                   "kombinácia ziskovo robustná aj na overovacej vzorke.",
                   "Záver: tieto signály nemajú spoľahlivý mechanický edge — lepšie ako alerty + úsudok."]
        ms.send_telegram("\n".join(msg))


if __name__ == "__main__":
    main()
