#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_scanner.py  (v2)
-----------------------
Skener trhu s upozorneniami na Telegram. NEOBCHODUJE - len pozoruje a posiela
signaly, rozhodnutie je na tebe.

Novinky vo v2:
  * Trvala pamat (state.json) - cooldown funguje aj napric behmi v cloude
  * ATR-based prahy - prah sa prisposobi volatilite kazdeho trhu
  * Confluence - zvyrazni, ked sa zhodne viac signalov naraz
  * Filter vyssieho timeframe (1h trend) - tlmenie protitrendovych signalov
  * Riziko v sprave - orientacny stop, R:R ciel a velkost pozicie
  * Tiche hodiny - v zadanom okne neposiela
  * Volitelny AI kontext - kratky komentar ku signalu (ak je nastaveny API kluc)

Data: yfinance (zadarmo, mierne oneskorene).
Spustenie lokalne:    python market_scanner.py
V cloude (GitHub):    nastav RUN_ONCE=1 (urobi 1 sken a skonci)
"""

import os
import json
import time
import math
import datetime as dt

import requests

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("Chyba: chyba kniznica yfinance. Spusti: pip install yfinance requests pandas")


# ============================================================
#  KONFIGURACIA
# ============================================================

# --- Telegram (z premennych prostredia / GitHub Secrets) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Instrumenty: nazov -> yfinance ticker ---
INSTRUMENTS = {
    "Zlato (XAU/USD)":   "GC=F",
    "Striebro (XAG/USD)":"SI=F",
    "US100 (Nasdaq)":    "NQ=F",
    "US30 (Dow)":        "YM=F",
    "US500 (S&P 500)":   "ES=F",
    "DE40 (DAX)":        "^GDAXI",
    "Ropa WTI":          "CL=F",
    "NatGas":            "NG=F",
}

# Hodnota 1 bodu pohybu na 1 jednotku/lot (uprav podla svojho brokera/CFD!).
# Sluzi LEN na orientacny vypocet velkosti pozicie. 1.0 = neutralne.
POINT_VALUE = {}   # napr. {"Zlato (XAU/USD)": 1.0, "US100 (Nasdaq)": 1.0}

POLL_SECONDS = 60
INTERVAL = "5m"
PERIOD   = "5d"

# --- Rychly pohyb ---
RAPID_BARS     = 3           # za kolko sviecok meriame pohyb
RAPID_ATR_MULT = 1.0         # pohyb > nasobok ATR -> signal (volatilite na mieru)
RAPID_PCT_MIN  = 0.15        # poistka: aspon tolko % (aby nepipalo pri mrtvom trhu)

# --- Trend (EMA kriz) ---
EMA_FAST = 9
EMA_SLOW = 21

# --- Breakout ---
BREAKOUT_LOOKBACK = 24

# --- RSI ---
RSI_PERIOD     = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD   = 30

# --- ATR ---
ATR_PERIOD = 14

# --- Ktore signaly ---
ENABLE_RAPID    = True
ENABLE_TREND    = True
ENABLE_BREAKOUT = True
ENABLE_RSI      = True

# --- Filter vyssieho timeframe (1h trend cez EMA) ---
ENABLE_HTF_FILTER = True
HTF_INTERVAL = "1h"
HTF_PERIOD   = "1mo"
HTF_EMA      = 50            # smer trendu na 1h podla EMA(50)

# --- Confluence ---
# True = posli LEN ked sa zhodnu 2+ signaly rovnakeho smeru.
# False = posli vsetko, ale zhodu zvyrazni.
CONFLUENCE_ONLY = False

# --- Riziko (orientacne!) ---
SHOW_RISK       = True
ACCOUNT_BALANCE = 1000.0     # velkost uctu (v mene uctu)
RISK_PCT        = 2.0        # kolko % uctu riskujes na obchod
STOP_ATR_MULT   = 1.5        # stop = nasobok ATR od vstupu
TARGET_RR       = 1.5        # ciel = nasobok rizika (R:R)

# --- Graf ---
SEND_CHART = True
CHART_BARS = 60

# --- Cooldown (minuty) - drzi napric behmi vdaka state.json ---
COOLDOWN_MINUTES = 30
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# --- Tiche hodiny (lokalny cas TZ). Rovnake = vypnute. ---
QUIET_TZ    = "Europe/Bratislava"
QUIET_START = 23             # hodina (0-23)
QUIET_END   = 7             # hodina (0-23); priklad: 23 a 7 = ticho 23:00-07:00

# --- AI kontext (volitelny). Ak je nastaveny ANTHROPIC_API_KEY, prida komentar. ---
ENABLE_AI_CONTEXT = True
AI_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL   = "claude-haiku-4-5-20251001"


# ============================================================
#  TELEGRAM
# ============================================================

def resolve_chat_id():
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates", timeout=15)
        for upd in reversed(r.json().get("result", [])):
            msg = upd.get("message") or upd.get("channel_post")
            if msg and "chat" in msg:
                TELEGRAM_CHAT_ID = str(msg["chat"]["id"])
                return TELEGRAM_CHAT_ID
    except Exception as e:
        print(f"!! Nepodarilo sa zistit chat_id: {e}")
    return ""


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN:
        print("!! Telegram token chyba - vypis do konzoly:\n   " + text.replace("\n", "\n   "))
        return False
    chat_id = resolve_chat_id()
    if not chat_id:
        print("!! Nemam chat_id.\n   " + text.replace("\n", "\n   "))
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            print(f"!! Telegram chyba {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"!! Telegram vynimka: {e}")
        return False


def send_telegram_photo(path, caption):
    if not TELEGRAM_BOT_TOKEN:
        return False
    chat_id = resolve_chat_id()
    if not chat_id:
        return False
    try:
        with open(path, "rb") as f:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f}, timeout=30)
        return r.status_code == 200
    except Exception as e:
        print(f"!! Telegram foto chyba: {e}")
        return False


# ============================================================
#  INDIKATORY
# ============================================================

def ema(values, span):
    k = 2.0 / (span + 1.0)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else (v * k + prev * (1.0 - k))
        out.append(prev)
    return out


def rsi(values, period=14):
    if len(values) <= period:
        return [None] * len(values)
    g, l = [], []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        g.append(max(ch, 0.0)); l.append(max(-ch, 0.0))
    out = [None] * len(values)
    ag = sum(g[:period]) / period; al = sum(l[:period]) / period
    calc = lambda ag, al: 100.0 if al == 0 else 100.0 - (100.0 / (1.0 + ag / al))
    out[period] = calc(ag, al)
    for i in range(period + 1, len(values)):
        ag = (ag * (period - 1) + g[i - 1]) / period
        al = (al * (period - 1) + l[i - 1]) / period
        out[i] = calc(ag, al)
    return out


def atr(highs, lows, closes, period=14):
    """Average True Range (Wilder). Vrati poslednu hodnotu alebo None."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    a = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
    return a


# ============================================================
#  STAV (proti duplicitam, napric behmi)
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"!! Nepodarilo sa ulozit state: {e}")


def recently_alerted(state, key, now_ts):
    last = state.get(key)
    if last is None:
        return False
    return (now_ts - last) < COOLDOWN_MINUTES * 60


def mark_alerted(state, key, now_ts):
    state[key] = now_ts


# ============================================================
#  RIZIKO
# ============================================================

def compute_risk(name, direction, last, atr_val):
    """Vrati text s orientacnym stopom, cielom a velkostou pozicie."""
    if not SHOW_RISK or not atr_val or atr_val <= 0:
        return ""
    risk_dist = STOP_ATR_MULT * atr_val
    if direction == "up":
        stop = last - risk_dist
        target = last + TARGET_RR * risk_dist
    else:
        stop = last + risk_dist
        target = last - TARGET_RR * risk_dist
    pv = POINT_VALUE.get(name, 1.0)
    risk_money = ACCOUNT_BALANCE * RISK_PCT / 100.0
    size = risk_money / (risk_dist * pv) if (risk_dist * pv) > 0 else 0
    return (f"\n\U0001F6E1 vstup ~{last:.2f} | stop ~{stop:.2f} | ciel ~{target:.2f} (R:R {TARGET_RR:g})"
            f"\n\U0001F4CF riziko {RISK_PCT:g}% = {risk_money:.0f}, ~{size:.2f} jednotiek "
            f"(over si hodnotu bodu pre {name.split()[0]}!)")


# ============================================================
#  AI KONTEXT (volitelny)
# ============================================================

def ai_context(summary):
    if not (ENABLE_AI_CONTEXT and AI_API_KEY):
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": AI_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 120,
                  "messages": [{"role": "user", "content":
                      "Si strucny trading asistent. K tomuto signalu napis JEDNU vetu po slovensky "
                      "s kontextom (co to znamena, na co si dat pozor). Ziadne odporucanie na obchod.\n\n"
                      + summary}]},
            timeout=20)
        data = r.json()
        txt = "".join(b.get("text", "") for b in data.get("content", []))
        return ("\n\U0001F4AC " + txt.strip()) if txt.strip() else ""
    except Exception as e:
        print(f"!! AI kontext chyba: {e}")
        return ""


# ============================================================
#  HTF FILTER (1h trend)
# ============================================================

_htf_cache = {}

def htf_trend(ticker):
    """Vrati 'up'/'down'/None podla polohy ceny voci EMA na 1h."""
    if not ENABLE_HTF_FILTER:
        return None
    if ticker in _htf_cache:
        return _htf_cache[ticker]
    try:
        df = yf.Ticker(ticker).history(period=HTF_PERIOD, interval=HTF_INTERVAL)
        closes = list(df["Close"].dropna().values)
        if len(closes) < HTF_EMA + 2:
            _htf_cache[ticker] = None
            return None
        e = ema(closes, HTF_EMA)
        trend = "up" if closes[-1] >= e[-1] else "down"
        _htf_cache[ticker] = trend
        return trend
    except Exception:
        _htf_cache[ticker] = None
        return None


# ============================================================
#  DETEKCIA SIGNALOV
# ============================================================

def detect_signals(name, df, atr_val):
    signals = []
    closes = list(df["Close"].dropna().values)
    highs  = list(df["High"].dropna().values)
    lows   = list(df["Low"].dropna().values)

    if len(closes) < max(EMA_SLOW + 2, RAPID_BARS + 1, BREAKOUT_LOOKBACK + 2, ATR_PERIOD + 2):
        return signals
    last = closes[-1]

    # 1) RYCHLY POHYB (ATR-based)
    if ENABLE_RAPID and atr_val:
        ref = closes[-1 - RAPID_BARS]
        move = last - ref
        pct = (move / ref * 100.0) if ref else 0.0
        if abs(move) >= RAPID_ATR_MULT * atr_val and abs(pct) >= RAPID_PCT_MIN:
            d = "up" if move > 0 else "down"
            sip = "\U0001F4C8" if d == "up" else "\U0001F4C9"
            smer = "NARAST" if d == "up" else "POKLES"
            signals.append({"typ": "rapid", "dir": d,
                "sprava": f"{sip} <b>RYCHLY {smer}</b> {name}: {pct:+.2f} % "
                          f"za ~{RAPID_BARS*int(INTERVAL.rstrip('m'))} min (cena {last:.2f})"})

    # 2) TREND (EMA kriz)
    if ENABLE_TREND:
        ef = ema(closes, EMA_FAST); es = ema(closes, EMA_SLOW)
        if ef[-2] <= es[-2] and ef[-1] > es[-1]:
            signals.append({"typ": "trend_up", "dir": "up",
                "sprava": f"\U0001F7E2 <b>TREND HORE</b> {name}: EMA{EMA_FAST} prerazila "
                          f"EMA{EMA_SLOW} zdola (cena {last:.2f})"})
        elif ef[-2] >= es[-2] and ef[-1] < es[-1]:
            signals.append({"typ": "trend_down", "dir": "down",
                "sprava": f"\U0001F534 <b>TREND DOLE</b> {name}: EMA{EMA_FAST} prerazila "
                          f"EMA{EMA_SLOW} zhora (cena {last:.2f})"})

    # 3) BREAKOUT (edge-triggered)
    if ENABLE_BREAKOUT:
        prev = closes[-2]
        wh = max(highs[-2 - BREAKOUT_LOOKBACK:-2])
        wl = min(lows[-2 - BREAKOUT_LOOKBACK:-2])
        mins = BREAKOUT_LOOKBACK * int(INTERVAL.rstrip("m"))
        if prev <= wh and last > wh:
            signals.append({"typ": "breakout_up", "dir": "up",
                "sprava": f"\U0001F680 <b>BREAKOUT HORE</b> {name}: cena {last:.2f} "
                          f"prerazila {mins}-min maximum {wh:.2f}"})
        elif prev >= wl and last < wl:
            signals.append({"typ": "breakout_down", "dir": "down",
                "sprava": f"⚠️ <b>BREAKOUT DOLE</b> {name}: cena {last:.2f} "
                          f"prerazila {mins}-min minimum {wl:.2f}"})

    # 4) RSI (edge-triggered)
    if ENABLE_RSI:
        r = rsi(closes, RSI_PERIOD)
        if r[-1] is not None and r[-2] is not None:
            val, pv = r[-1], r[-2]
            if pv < RSI_OVERBOUGHT and val >= RSI_OVERBOUGHT:
                signals.append({"typ": "rsi_overbought", "dir": "down",
                    "sprava": f"\U0001F525 <b>RSI PREKUPENE</b> {name}: RSI {val:.0f} "
                              f"(prekrocilo {RSI_OVERBOUGHT}) - mozny obrat dole (cena {last:.2f})"})
            elif pv > RSI_OVERSOLD and val <= RSI_OVERSOLD:
                signals.append({"typ": "rsi_oversold", "dir": "up",
                    "sprava": f"\U0001F9CA <b>RSI PREPREDANE</b> {name}: RSI {val:.0f} "
                              f"(kleslo pod {RSI_OVERSOLD}) - mozny obrat hore (cena {last:.2f})"})

    return signals


def apply_htf_filter(ticker, signals):
    """Odstrani signaly proti 1h trendu."""
    trend = htf_trend(ticker)
    if trend is None:
        return signals, None
    kept = [s for s in signals if s["dir"] == trend]
    return kept, trend


def confluence_tag(signals):
    """Vrati ('up'/'down', pocet) ak sa zhoduju 2+ signaly v smere, inak (None,0)."""
    ups = [s for s in signals if s["dir"] == "up"]
    downs = [s for s in signals if s["dir"] == "down"]
    if len(ups) >= 2:
        return "up", len(ups)
    if len(downs) >= 2:
        return "down", len(downs)
    return None, 0


# ============================================================
#  GRAF
# ============================================================

def make_chart(name, df):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    try:
        sub = df.tail(CHART_BARS)
        closes = list(sub["Close"].values)
        if len(closes) < 5:
            return None
        ef = ema(closes, EMA_FAST); es = ema(closes, EMA_SLOW)
        x = range(len(closes))
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(x, closes, color="#222", linewidth=1.6, label="Cena")
        ax.plot(x, ef, color="#1f9d55", linewidth=1.0, label=f"EMA{EMA_FAST}")
        ax.plot(x, es, color="#c0392b", linewidth=1.0, label=f"EMA{EMA_SLOW}")
        ax.set_title(name); ax.legend(loc="upper left", fontsize=8); ax.grid(alpha=0.25)
        fig.tight_layout()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"_chart_{name.split()[0].replace('/', '')}.png")
        fig.savefig(path, dpi=90); plt.close(fig)
        return path
    except Exception as e:
        print(f"!! Chyba pri grafe: {e}")
        return None


# ============================================================
#  SKEN
# ============================================================

def fetch(ticker):
    try:
        df = yf.Ticker(ticker).history(period=PERIOD, interval=INTERVAL)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"!! Chyba pri nacitani {ticker}: {e}")
        return None


def scan_once(state):
    found = 0
    now_ts = time.time()
    for name, ticker in INSTRUMENTS.items():
        df = fetch(ticker)
        if df is None:
            continue
        closes = list(df["Close"].dropna().values)
        highs  = list(df["High"].dropna().values)
        lows   = list(df["Low"].dropna().values)
        atr_val = atr(highs, lows, closes, ATR_PERIOD)

        sigs = detect_signals(name, df, atr_val)
        if not sigs:
            continue

        # filter vyssieho timeframe
        sigs, trend = apply_htf_filter(ticker, sigs)
        if not sigs:
            continue

        # confluence
        conf_dir, conf_n = confluence_tag(sigs)
        if CONFLUENCE_ONLY and conf_dir is None:
            continue

        # dedup napric behmi
        fresh = []
        for s in sigs:
            key = f"{name}|{s['typ']}"
            if recently_alerted(state, key, now_ts):
                continue
            mark_alerted(state, key, now_ts)
            fresh.append(s)
        if not fresh:
            continue

        ts = dt.datetime.now().strftime("%H:%M:%S")
        head = ""
        if conf_dir:
            smer = "HORE" if conf_dir == "up" else "DOLE"
            head = f"⭐ <b>CONFLUENCE {smer}</b> ({conf_n} signalov sa zhoduje)\n"
        body = "\n".join(s["sprava"] for s in fresh)

        # riziko (podla prveho signalu / jeho smeru)
        direction = fresh[0]["dir"]
        risk = compute_risk(name, direction, closes[-1], atr_val)

        # AI kontext
        ai = ai_context(head + body)

        text = f"{head}{body}{risk}{ai}\n⏱ {ts}"
        for s in fresh:
            print(f"[SIGNAL] {s['sprava']}")

        chart = make_chart(name, df) if SEND_CHART else None
        if not (chart and send_telegram_photo(chart, text)):
            send_telegram(text)
        found += len(fresh)
    return found


# ============================================================
#  TICHE HODINY
# ============================================================

def in_quiet_hours():
    if QUIET_START == QUIET_END:
        return False
    try:
        from zoneinfo import ZoneInfo
        h = dt.datetime.now(ZoneInfo(QUIET_TZ)).hour
    except Exception:
        h = dt.datetime.now().hour
    if QUIET_START < QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END   # cez polnoc


# ============================================================
#  MAIN
# ============================================================

def main():
    print("=" * 60)
    print(" SKENER TRHU v2 - spusteny")
    print(f" Instrumenty: {', '.join(INSTRUMENTS.keys())}")
    print(f" Sviecka: {INTERVAL} | HTF filter: {ENABLE_HTF_FILTER} | "
          f"AI: {bool(AI_API_KEY)} | confluence_only: {CONFLUENCE_ONLY}")
    print("=" * 60)

    if in_quiet_hours():
        print("[QUIET] tiche hodiny - sken sa neposiela.")
        return

    state = load_state()

    if os.environ.get("RUN_ONCE", "").strip() in ("1", "true", "True", "yes"):
        n = scan_once(state)
        save_state(state)
        print(f"[RUN_ONCE] sken hotovy, signalov: {n}")
        return

    send_telegram("✅ Skener trhu v2 spusteny a sleduje trh.")
    while True:
        try:
            if not in_quiet_hours():
                n = scan_once(state)
                save_state(state)
                print(f"[{dt.datetime.now():%H:%M:%S}] sken hotovy, signalov: {n}")
        except KeyboardInterrupt:
            print("\nUkoncene pouzivatelom."); break
        except Exception as e:
            print(f"!! Chyba v slucke: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
