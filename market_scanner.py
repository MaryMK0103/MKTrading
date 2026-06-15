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

# --- Ekonomicky kalendar (vstavany, pevne sa opakujuce udalosti) ---
ENABLE_CALENDAR      = True
CALENDAR_WARN_HOURS  = 2      # ku signalu prida varovanie, ak je udalost do tolkoto hodin
ENABLE_MORNING_BRIEF = True   # ranna sumarka udalosti dna
MORNING_BRIEF_HOUR   = 8      # hodina rannej sumarky (lokalny cas QUIET_TZ)

# FOMC dni 2026 (rozhodnutie Fed o sadzbach, 14:00 ET) - overene
FOMC_DATES = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}

# --- Signal "velky denny pohyb" ---
ENABLE_DAILY_MOVE = True
DAILY_MOVE_PCT    = 1.0       # alert ak je trh +-tolko % za den (vs predosly close)

# --- Denik signalov + tyzdenny suhrn ---
ENABLE_SIGNAL_LOG    = True
SIGNAL_LOG_MAX       = 300     # kolko poslednych signalov drzat
ENABLE_WEEKLY_SUMMARY = True
WEEKLY_DAY  = 0               # 0=pondelok ... 6=nedela
WEEKLY_HOUR = 8              # hodina suhrnu (lokalny cas QUIET_TZ)

# --- Scan na poziadanie cez Telegram (napis "scan") ---
ENABLE_SCAN_COMMAND = True

# --- Priebezne vyhodnocovanie dnesnych signalov ---
ENABLE_HOURLY_EVAL = True     # kazdu hodinu suhrn (ako si dnesne signaly vedu)
ENABLE_DAILY_EVAL  = True     # vyhodnotenie na konci dna
DAILY_EVAL_HOUR    = 22       # hodina denneho vyhodnotenia (lokalny cas)


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

# ============================================================
#  EKONOMICKY KALENDAR (vstavany)
# ============================================================

def _et_zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")

def todays_events():
    """Zoznam dnesnych pevnych udalosti: dict(name, when[ET datetime], affects)."""
    if not ENABLE_CALENDAR:
        return []
    et = _et_zone()
    now = dt.datetime.now(et)
    d = now.date()
    wd = now.weekday()           # Po=0 ... Ne=6
    evs = []
    def mk(h, m, name, affects):
        evs.append({"name": name,
                    "when": dt.datetime(d.year, d.month, d.day, h, m, tzinfo=et),
                    "affects": affects})
    if wd == 1:   # utorok
        mk(16, 30, "Zasoby ropy (API, tyzdenne)", {"Ropa WTI"})
    if wd == 2:   # streda
        mk(10, 30, "Zasoby ropy (EIA, tyzdenne)", {"Ropa WTI"})
    if wd == 3:   # stvrtok
        mk(10, 30, "Zasoby plynu (EIA, tyzdenne)", {"NatGas"})
    if wd == 4 and d.day <= 7:   # prvy piatok v mesiaci
        mk(8, 30, "NFP - trh prace USA", "all")
    if d.isoformat() in FOMC_DATES:
        mk(14, 0, "FOMC - rozhodnutie Fed o sadzbach", "all")
    return evs

def event_warning_for(name):
    """Varovanie ku signalu, ak je do CALENDAR_WARN_HOURS velka udalost pre tento trh."""
    if not ENABLE_CALENDAR:
        return ""
    try:
        from zoneinfo import ZoneInfo
        local = ZoneInfo(QUIET_TZ)
        now = dt.datetime.now(_et_zone())
        out = []
        for e in todays_events():
            delta = (e["when"] - now).total_seconds()
            if 0 <= delta <= CALENDAR_WARN_HOURS * 3600:
                if e["affects"] == "all" or name in e["affects"]:
                    t = e["when"].astimezone(local).strftime("%H:%M")
                    out.append(f"⚠️ POZOR o {t}: {e['name']} – mozna zvysena volatilita")
        return ("\n" + "\n".join(out)) if out else ""
    except Exception:
        return ""

def morning_brief_text():
    try:
        from zoneinfo import ZoneInfo
        local = ZoneInfo(QUIET_TZ)
        evs = todays_events()
        if not evs:
            return ""
        lines = []
        for e in sorted(evs, key=lambda x: x["when"]):
            t = e["when"].astimezone(local).strftime("%H:%M")
            aff = "vsetky trhy" if e["affects"] == "all" else ", ".join(e["affects"])
            lines.append(f"• {t} – {e['name']} ({aff})")
        return "\U0001F4C5 <b>Dnes dolezite udalosti</b>\n" + "\n".join(lines)
    except Exception:
        return ""

def maybe_send_morning_brief(state):
    if not ENABLE_MORNING_BRIEF:
        return
    try:
        from zoneinfo import ZoneInfo
        now_local = dt.datetime.now(ZoneInfo(QUIET_TZ))
    except Exception:
        now_local = dt.datetime.now()
    if now_local.hour < MORNING_BRIEF_HOUR:
        return
    key = "_brief_" + now_local.date().isoformat()
    if state.get(key):
        return
    txt = morning_brief_text()
    if txt:
        send_telegram(txt)
        state[key] = time.time()


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


def _local_now():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(QUIET_TZ))
    except Exception:
        return dt.datetime.now()


def daily_change(df):
    """Zmena ceny za den (vs posledny close predosleho dna). Vrati (chg%, datum) alebo None."""
    closes = list(df["Close"].dropna().values)
    try:
        dates = [t.date() for t in df.index]
    except Exception:
        return None
    if not closes or not dates:
        return None
    last_date = dates[-1]
    prev = [closes[i] for i in range(len(closes)) if dates[i] != last_date]
    if not prev or not prev[-1]:
        return None
    return (closes[-1] - prev[-1]) / prev[-1] * 100.0, last_date.isoformat()


def append_log(state, entry):
    if not ENABLE_SIGNAL_LOG:
        return
    log = state.get("log", [])
    log.append(entry)
    if len(log) > SIGNAL_LOG_MAX:
        log = log[-SIGNAL_LOG_MAX:]
    state["log"] = log


def log_signal(state, name, typ, d, price, strength):
    now = _local_now()
    append_log(state, {"ts": time.time(), "date": now.strftime("%Y-%m-%d"),
                       "time": now.strftime("%H:%M"), "name": name, "typ": typ,
                       "dir": d, "price": round(float(price), 2), "str": strength})


def build_snapshot_text(items):
    """Kompaktny textovy prehlad vsetkych trhov (pre 'scan' prikaz)."""
    order = {"green": 0, "orange": 1, "gray": 2}
    its = sorted(items, key=lambda x: (order.get(x["status"], 3), -abs(x["chg_pct"])))
    dots = {"green": "🟢", "orange": "🟠", "gray": "⚪"}
    lines = ["📊 <b>Prehľad trhov</b>"]
    for it in its:
        d = dots.get(it["status"], "⚪")
        arr = "▲" if it["trend"] == "up" else "▼"
        lines.append(f"{d} <b>{it['name']}</b> {it['price']} {arr} "
                     f"{it['chg_pct']:+.2f}% · RSI {it['rsi']}")
    lines.append("⏱ " + _local_now().strftime("%H:%M"))
    return "\n".join(lines)


def telegram_get_updates(offset):
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 0}, timeout=15)
        return r.json().get("result", [])
    except Exception as e:
        print(f"!! getUpdates chyba: {e}")
        return []


def handle_scan_command(state, items):
    """Ak pouzivatel napisal 'scan', posle prehlad trhov."""
    if not ENABLE_SCAN_COMMAND:
        return
    updates = telegram_get_updates(state.get("tg_offset", 0))
    triggered = False
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        txt = (msg.get("text") or "").strip().lower()
        if txt in ("scan", "prehlad", "prehľad", "/scan"):
            triggered = True
    if triggered and items:
        send_telegram(build_snapshot_text(items))


def maybe_weekly_summary(state):
    if not ENABLE_WEEKLY_SUMMARY:
        return
    now = _local_now()
    if now.weekday() != WEEKLY_DAY or now.hour < WEEKLY_HOUR:
        return
    wk = now.strftime("%G-W%V")
    if state.get("weekly_sent") == wk:
        return
    from collections import Counter
    cutoff = time.time() - 7 * 86400
    week = [e for e in state.get("log", []) if e.get("ts", 0) >= cutoff]
    by_type = Counter(e["typ"] for e in week)
    by_name = Counter(e["name"] for e in week)
    lines = ["📒 <b>Týždenný súhrn signálov</b>",
             f"Spolu: {len(week)} signálov za 7 dní"]
    if by_type:
        lines.append("Podľa typu: " + ", ".join(f"{t} {c}×" for t, c in by_type.most_common()))
    if by_name:
        lines.append("Najaktívnejšie: " + ", ".join(f"{n} {c}×" for n, c in by_name.most_common(3)))
    send_telegram("\n".join(lines))
    state["weekly_sent"] = wk


def eval_today(state, items):
    """Pre kazdy dnesny signal: o kolko % sa cena pohla v PREDIKOVANOM smere odvtedy.
    Vrati zoznam (zaznam, signed_pct alebo None ak nemame aktualnu cenu)."""
    price_map = {it["name"]: it["price"] for it in items}
    today = _local_now().strftime("%Y-%m-%d")
    out = []
    for e in state.get("log", []):
        if e.get("date") != today:
            continue
        cur = price_map.get(e["name"])
        if cur is None or not e.get("price"):
            out.append((e, None))
            continue
        move = (cur - e["price"]) / e["price"] * 100.0
        signed = move if e["dir"] == "up" else -move
        out.append((e, round(signed, 2)))
    return out


def enrich_recent(state, items, limit=40):
    """Dnesne signaly s priebeznym vysledkom pre dashboard (najnovsie hore)."""
    res = []
    for e, signed in eval_today(state, items):
        ee = dict(e)
        if signed is not None:
            ee["res_pct"] = signed
            ee["ok"] = signed > 0
        res.append(ee)
    return res[-limit:][::-1]


def summary_text(res, title):
    evaluable = [(e, s) for e, s in res if s is not None]
    n = len(res)
    lines = [f"<b>{title}</b>", f"Signálov dnes: {n}"]
    if evaluable:
        ok = sum(1 for _, s in evaluable if s > 0)
        avg = sum(s for _, s in evaluable) / len(evaluable)
        lines.append(f"V správnom smere: {ok}/{len(evaluable)} "
                     f"({ok/len(evaluable)*100:.0f} %), priemer {avg:+.2f} %")
        best = max(evaluable, key=lambda x: x[1])
        worst = min(evaluable, key=lambda x: x[1])
        lines.append(f"Najlepší: {best[0]['name']} ({best[0]['typ']}) {best[1]:+.2f} %")
        lines.append(f"Najhorší: {worst[0]['name']} ({worst[0]['typ']}) {worst[1]:+.2f} %")
    elif n == 0:
        lines.append("dnes žiadne signály")
    lines.append("ℹ pohyb v smere signálu odvtedy (orientačné, nie reálny obchod)")
    return "\n".join(lines)


def maybe_hourly_eval(state, items):
    if not ENABLE_HOURLY_EVAL or in_quiet_hours():
        return
    now = _local_now()
    key = now.strftime("%Y-%m-%d-%H")
    if state.get("hourly_sent") == key:
        return
    res = eval_today(state, items)
    if not res:          # ziadne dnesne signaly -> ticho
        return
    state["hourly_sent"] = key
    send_telegram(summary_text(res, "⏱ Hodinové vyhodnotenie"))


def maybe_daily_eval(state, items):
    if not ENABLE_DAILY_EVAL:
        return
    now = _local_now()
    if now.hour < DAILY_EVAL_HOUR:
        return
    key = now.strftime("%Y-%m-%d")
    if state.get("daily_eval_sent") == key:
        return
    state["daily_eval_sent"] = key
    send_telegram(summary_text(eval_today(state, items), "📅 Koniec dňa — vyhodnotenie"))


def build_snapshot_item(name, closes, highs, lows, atr_val):
    """Prehlad jedneho trhu pre dashboard."""
    last = closes[-1]
    r = rsi(closes, RSI_PERIOD)
    rsi_v = r[-1] if r[-1] is not None else 50.0
    ef = ema(closes, EMA_FAST); es = ema(closes, EMA_SLOW)
    trend = "up" if ef[-1] >= es[-1] else "down"
    atr_pct = (atr_val / last * 100.0) if (atr_val and last) else 0.0
    ref = closes[-1 - RAPID_BARS] if len(closes) > RAPID_BARS else closes[0]
    chg = ((last - ref) / ref * 100.0) if ref else 0.0
    wh = max(highs[-1 - BREAKOUT_LOOKBACK:-1])
    wl = min(lows[-1 - BREAKOUT_LOOKBACK:-1])
    dist_high = ((last - wh) / wh * 100.0) if wh else 0.0
    dist_low = ((last - wl) / wl * 100.0) if wl else 0.0
    extreme = (rsi_v >= RSI_OVERBOUGHT or rsi_v <= RSI_OVERSOLD
               or last > wh or last < wl or abs(chg) >= RAPID_PCT_MIN * 2)
    moderate = (rsi_v >= 60 or rsi_v <= 40 or abs(chg) >= RAPID_PCT_MIN)
    status = "green" if extreme else ("orange" if moderate else "gray")

    # --- seria pre graf (poslednych W sviecok) + znacky signalov ---
    W = 48
    n = len(closes)
    s = max(0, n - W)
    c_w  = [round(float(x), 4) for x in closes[s:]]
    ef_w = [round(float(x), 4) for x in ef[s:]]
    es_w = [round(float(x), 4) for x in es[s:]]
    rsi_w = [None if r[i] is None else round(float(r[i])) for i in range(s, n)]
    marks = []
    for i in range(max(s, 2), n):
        x = i - s
        if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
            marks.append({"x": x, "t": "trend", "d": "up"})
        elif ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
            marks.append({"x": x, "t": "trend", "d": "down"})
        if i >= BREAKOUT_LOOKBACK + 2:
            wh2 = max(highs[i - 1 - BREAKOUT_LOOKBACK:i - 1])
            wl2 = min(lows[i - 1 - BREAKOUT_LOOKBACK:i - 1])
            if closes[i - 1] <= wh2 and closes[i] > wh2:
                marks.append({"x": x, "t": "breakout", "d": "up"})
            elif closes[i - 1] >= wl2 and closes[i] < wl2:
                marks.append({"x": x, "t": "breakout", "d": "down"})
        if r[i] is not None and r[i - 1] is not None:
            if r[i - 1] < RSI_OVERBOUGHT and r[i] >= RSI_OVERBOUGHT:
                marks.append({"x": x, "t": "rsi", "d": "down"})
            elif r[i - 1] > RSI_OVERSOLD and r[i] <= RSI_OVERSOLD:
                marks.append({"x": x, "t": "rsi", "d": "up"})
        if atr_val and i >= RAPID_BARS + 1:
            def _rap(j):
                b = closes[j - RAPID_BARS]
                if not b:
                    return 0
                m2 = closes[j] - b
                if abs(m2) >= RAPID_ATR_MULT * atr_val and abs(m2 / b * 100) >= RAPID_PCT_MIN:
                    return 1 if m2 > 0 else -1
                return 0
            cur = _rap(i)
            if cur and cur != _rap(i - 1):   # len zaciatok pohybu
                marks.append({"x": x, "t": "rapid", "d": "up" if cur > 0 else "down"})

    return {"name": name, "price": round(last, 2), "rsi": round(rsi_v),
            "trend": trend, "atr_pct": round(atr_pct, 2), "chg_pct": round(chg, 2),
            "dist_high": round(dist_high, 2), "dist_low": round(dist_low, 2),
            "status": status,
            "c": c_w, "ef": ef_w, "es": es_w, "rsi_s": rsi_w, "marks": marks}


def publish_dashboard(items, recent=None):
    """Zapise prehlad do verejneho Gistu (ak su nastavene GIST_TOKEN a GIST_ID)."""
    token = os.environ.get("GIST_TOKEN", "")
    gid = os.environ.get("GIST_ID", "")
    if not (token and gid):
        return
    payload = {"updated": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
               "updated_ts": int(time.time()),
               "items": items, "signals": recent or []}
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json"},
            json={"files": {"dashboard.json": {"content": json.dumps(payload, ensure_ascii=False)}}},
            timeout=20)
        if r.status_code not in (200, 201):
            print(f"!! Gist update {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"!! Gist update chyba: {e}")


def scan_once(state):
    found = 0
    now_ts = time.time()
    items = []
    for name, ticker in INSTRUMENTS.items():
        df = fetch(ticker)
        if df is None:
            continue
        closes = list(df["Close"].dropna().values)
        highs  = list(df["High"].dropna().values)
        lows   = list(df["Low"].dropna().values)
        atr_val = atr(highs, lows, closes, ATR_PERIOD)

        if len(closes) >= BREAKOUT_LOOKBACK + 2:
            items.append(build_snapshot_item(name, closes, highs, lows, atr_val))

        # --- velky denny pohyb (raz denne na trh) ---
        if ENABLE_DAILY_MOVE and len(closes) > RAPID_BARS:
            dm = daily_change(df)
            if dm and abs(dm[0]) >= DAILY_MOVE_PCT:
                chg_d, dkey = dm
                k = f"daily|{name}|{dkey}"
                if not state.get(k):
                    state[k] = True
                    dd = "up" if chg_d > 0 else "down"
                    sip = "📈" if dd == "up" else "📉"
                    dmsg = (f"{sip} <b>VEĽKÝ DENNÝ POHYB</b> {name}: {chg_d:+.2f}% za deň "
                            f"(cena {closes[-1]:.2f})")
                    print("[SIGNAL] " + dmsg)
                    send_telegram(dmsg)
                    log_signal(state, name, "daily", dd, closes[-1], "blue")
                    found += 1

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

        # --- SILA SIGNALU: cervena=slaby, oranzova=stredny (RSI), zelena=zhoda ---
        def _dot(s):
            if conf_dir and s["dir"] == conf_dir:
                return "\U0001F7E2"   # zelena - zhoda
            if s["typ"].startswith("rsi"):
                return "\U0001F7E0"   # oranzova - strednejsi (RSI ma edge)
            return "\U0001F534"        # cervena - slaby (rapid/breakout/trend)

        if conf_dir:
            smer = "HORE" if conf_dir == "up" else "DOLE"
            head = (f"\U0001F7E2 <b>SILNY SIGNAL — ZHODA {smer}</b> "
                    f"({conf_n} signalov sa zhoduje)\n")
        elif any(s["typ"].startswith("rsi") for s in fresh):
            head = "\U0001F7E0 <b>STREDNY SIGNAL</b>\n"
        else:
            head = "\U0001F534 <b>SLABY SIGNAL</b>\n"

        body = "\n".join(f"{_dot(s)} {s['sprava']}" for s in fresh)
        legenda = "\n\n🔴 slabý  🟠 strednejší  🟢 zhoda viacerých"

        # riziko (podla prveho signalu / jeho smeru)
        direction = fresh[0]["dir"]
        risk = compute_risk(name, direction, closes[-1], atr_val)

        # varovanie na ekonomicku udalost
        warn = event_warning_for(name)

        # AI kontext
        ai = ai_context(head + body)

        text = f"{head}{body}{risk}{warn}{ai}{legenda}\n⏱ {ts}"
        for s in fresh:
            print(f"[SIGNAL] {s['sprava']}")

        chart = make_chart(name, df) if SEND_CHART else None
        if not (chart and send_telegram_photo(chart, text)):
            send_telegram(text)
        for s in fresh:
            stg = ("green" if (conf_dir and s["dir"] == conf_dir)
                   else ("orange" if s["typ"].startswith("rsi") else "red"))
            log_signal(state, name, s["typ"], s["dir"], closes[-1], stg)
        found += len(fresh)

    # interaktivita + vyhodnotenie + suhrn + dashboard
    handle_scan_command(state, items)
    maybe_hourly_eval(state, items)
    maybe_daily_eval(state, items)
    maybe_weekly_summary(state)
    publish_dashboard(items, enrich_recent(state, items))
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
        maybe_send_morning_brief(state)
        n = scan_once(state)
        save_state(state)
        print(f"[RUN_ONCE] sken hotovy, signalov: {n}")
        return

    send_telegram("✅ Skener trhu v2 spusteny a sleduje trh.")
    while True:
        try:
            if not in_quiet_hours():
                maybe_send_morning_brief(state)
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
