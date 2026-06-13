#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_scanner.py
-----------------
Skener trhu, ktory ta UPOZORNI cez Telegram, ked na sledovanych instrumentoch
uvidi: (1) rychly pokles/narast ceny, (2) zmenu trendu (kriz kluzavych priemerov),
(3) prelomenie hladiny (breakout).

NEOBCHODUJE automaticky. Len pozoruje a posiela signaly. Rozhodnutie je na tebe.

Data: yfinance (zadarmo, mierne oneskorene - radovo minuty).
Spustenie: python market_scanner.py
"""

import os
import time
import datetime as dt

import requests

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("Chyba: chyba kniznica yfinance. Spusti: pip install yfinance requests")


# ============================================================
#  KONFIGURACIA  -  toto si uprav podla seba
# ============================================================

# --- Telegram (vid README ako ziskat token a chat_id) ---
# Token a chat_id mozes zadat tu, alebo cez premenne prostredia
# TELEGRAM_BOT_TOKEN a TELEGRAM_CHAT_ID (bezpecnejsie).
# Token a chat_id sa citaju z premennych prostredia (GitHub Secrets v cloude).
# NEVKLADAJ token priamo sem, ak ma byt repozitar verejny!
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- Instrumenty: nazov  ->  yfinance ticker ---
INSTRUMENTS = {
    "Zlato (XAU/USD)":   "GC=F",     # Gold futures
    "Striebro (XAG/USD)":"SI=F",     # Silver futures
    "US100 (Nasdaq)":    "NQ=F",     # Nasdaq-100 futures
    "US30 (Dow)":        "YM=F",     # Dow futures
    "US500 (S&P 500)":   "ES=F",     # S&P 500 futures
    "DE40 (DAX)":        "^GDAXI",   # DAX index
    "Ropa WTI":          "CL=F",     # Crude Oil WTI futures
    "NatGas":            "NG=F",     # Natural Gas futures
}

# --- Ako casto skenovat (sekundy) ---
POLL_SECONDS = 60

# --- Parametre sviecky / historie ---
INTERVAL = "5m"      # velkost sviecky: 1m, 2m, 5m, 15m...
PERIOD   = "2d"      # kolko historie nacitat (potrebne na priemery)

# --- Prah pre RYCHLY POHYB ---
# Ak sa cena pohne o viac ako X % za RAPID_BARS sviecok -> signal.
RAPID_PCT  = 0.30    # v percentach (0.30 = 0,3 %)
RAPID_BARS = 3       # pocet sviecok (3 x 5m = 15 minut)

# --- Parametre TREND (kriz kluzavych priemerov EMA) ---
EMA_FAST = 9
EMA_SLOW = 21

# --- Parametre BREAKOUT (prelomenie hladiny) ---
# Cena prelomi maximum/minimum poslednych N sviecok (okrem aktualnej).
BREAKOUT_LOOKBACK = 24   # 24 x 5m = 2 hodiny

# --- Parametre RSI ---
RSI_PERIOD     = 14
RSI_OVERBOUGHT = 70      # nad tymto = prekupene (mozny obrat dole)
RSI_OVERSOLD   = 30      # pod tymto = prepredane (mozny obrat hore)

# --- Ktore signaly chces (True/False) ---
ENABLE_RAPID    = True
ENABLE_TREND    = True
ENABLE_BREAKOUT = True
ENABLE_RSI      = True

# --- Posielat ku signalu aj graf (obrazok)? ---
SEND_CHART  = True
CHART_BARS  = 60         # kolko poslednych sviecok vykreslit

# --- Cooldown: rovnaky typ signalu pre rovnaky instrument
#     sa znovu neposle skor ako po tolkoto minutach (proti spamu) ---
COOLDOWN_MINUTES = 30


# ============================================================
#  TELEGRAM
# ============================================================

def resolve_chat_id() -> str:
    """Zisti chat_id z poslednej spravy poslanej botovi (getUpdates)."""
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, timeout=15)
        data = r.json()
        for upd in reversed(data.get("result", [])):
            msg = upd.get("message") or upd.get("channel_post")
            if msg and "chat" in msg:
                TELEGRAM_CHAT_ID = str(msg["chat"]["id"])
                print(f"   Zistene chat_id: {TELEGRAM_CHAT_ID}")
                return TELEGRAM_CHAT_ID
    except Exception as e:
        print(f"!! Nepodarilo sa zistit chat_id: {e}")
    return ""


def send_telegram(text: str) -> bool:
    """Posle spravu na Telegram. Vrati True ak OK."""
    if "SEM_VLOZ" in TELEGRAM_BOT_TOKEN or not TELEGRAM_BOT_TOKEN:
        print("!! Telegram token chyba - vypisujem len do konzoly:")
        print("   " + text.replace("\n", "\n   "))
        return False
    chat_id = resolve_chat_id()
    if not chat_id:
        print("!! Nemam chat_id. Napis botovi v Telegrame spravu a skus znova.")
        print("   " + text.replace("\n", "\n   "))
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=15)
        if r.status_code != 200:
            print(f"!! Telegram chyba {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"!! Telegram vynimka: {e}")
        return False


# ============================================================
#  INDIKATORY
# ============================================================

def ema(values, span):
    """Exponencialny kluzavy priemer (bez pandas zavislosti na .ewm pre istotu)."""
    k = 2.0 / (span + 1.0)
    out = []
    prev = None
    for v in values:
        prev = v if prev is None else (v * k + prev * (1.0 - k))
        out.append(prev)
    return out


def rsi(values, period=14):
    """RSI (Wilderov vyhladeny). Vrati zoznam rovnakej dlzky, zaciatok = None."""
    if len(values) <= period:
        return [None] * len(values)
    gains, losses = [], []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    out = [None] * len(values)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    def _calc(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))
    out[period] = _calc(avg_g, avg_l)
    for i in range(period + 1, len(values)):
        avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
        avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = _calc(avg_g, avg_l)
    return out


# ============================================================
#  DETEKCIA SIGNALOV
# ============================================================

def detect_signals(name, df):
    """
    Vrati zoznam signalov (dict: typ, smer, sprava) z dataframe so stlpcami
    Open/High/Low/Close. Pracuje s poslednou UZAVRETOU sviecou.
    """
    signals = []
    closes = list(df["Close"].dropna().values)
    highs  = list(df["High"].dropna().values)
    lows   = list(df["Low"].dropna().values)

    if len(closes) < max(EMA_SLOW + 2, RAPID_BARS + 1, BREAKOUT_LOOKBACK + 2):
        return signals  # malo dat

    last = closes[-1]

    # --- 1) RYCHLY POHYB ---
    if ENABLE_RAPID:
        ref = closes[-1 - RAPID_BARS]
        if ref:
            pct = (last - ref) / ref * 100.0
            if abs(pct) >= RAPID_PCT:
                smer = "NARAST" if pct > 0 else "POKLES"
                sip = "\U0001F4C8" if pct > 0 else "\U0001F4C9"
                signals.append({
                    "typ": "rapid",
                    "sprava": f"{sip} <b>RYCHLY {smer}</b> {name}: {pct:+.2f} % "
                              f"za ~{RAPID_BARS*int(INTERVAL.rstrip('m'))} min (cena {last:.2f})",
                })

    # --- 2) TREND: kriz EMA ---
    if ENABLE_TREND:
        ef = ema(closes, EMA_FAST)
        es = ema(closes, EMA_SLOW)
        # kriz medzi predposlednou a poslednou sviecou
        if ef[-2] <= es[-2] and ef[-1] > es[-1]:
            signals.append({
                "typ": "trend_up",
                "sprava": f"\U0001F7E2 <b>TREND HORE</b> {name}: EMA{EMA_FAST} prerazila "
                          f"EMA{EMA_SLOW} zdola (cena {last:.2f})",
            })
        elif ef[-2] >= es[-2] and ef[-1] < es[-1]:
            signals.append({
                "typ": "trend_down",
                "sprava": f"\U0001F534 <b>TREND DOLE</b> {name}: EMA{EMA_FAST} prerazila "
                          f"EMA{EMA_SLOW} zhora (cena {last:.2f})",
            })

    # --- 3) BREAKOUT: prelomenie hladiny (edge-triggered: len v momente prerazenia) ---
    if ENABLE_BREAKOUT:
        prev = closes[-2]
        window_high = max(highs[-2 - BREAKOUT_LOOKBACK:-2])  # po predposlednu sviecu
        window_low  = min(lows[-2 - BREAKOUT_LOOKBACK:-2])
        if prev <= window_high and last > window_high:
            signals.append({
                "typ": "breakout_up",
                "sprava": f"\U0001F680 <b>BREAKOUT HORE</b> {name}: cena {last:.2f} "
                          f"prerazila {BREAKOUT_LOOKBACK*int(INTERVAL.rstrip('m'))}-min "
                          f"maximum {window_high:.2f}",
            })
        elif prev >= window_low and last < window_low:
            signals.append({
                "typ": "breakout_down",
                "sprava": f"⚠️ <b>BREAKOUT DOLE</b> {name}: cena {last:.2f} "
                          f"prerazila {BREAKOUT_LOOKBACK*int(INTERVAL.rstrip('m'))}-min "
                          f"minimum {window_low:.2f}",
            })

    # --- 4) RSI extremy (edge-triggered: len ked RSI prave prekroci hranicu) ---
    if ENABLE_RSI:
        r = rsi(closes, RSI_PERIOD)
        if r[-1] is not None and r[-2] is not None:
            val, prev_val = r[-1], r[-2]
            if prev_val < RSI_OVERBOUGHT and val >= RSI_OVERBOUGHT:
                signals.append({
                    "typ": "rsi_overbought",
                    "sprava": f"\U0001F525 <b>RSI PREKUPENE</b> {name}: RSI {val:.0f} "
                              f"(prekrocilo {RSI_OVERBOUGHT}) - mozny obrat dole (cena {last:.2f})",
                })
            elif prev_val > RSI_OVERSOLD and val <= RSI_OVERSOLD:
                signals.append({
                    "typ": "rsi_oversold",
                    "sprava": f"\U0001F9CA <b>RSI PREPREDANE</b> {name}: RSI {val:.0f} "
                              f"(kleslo pod {RSI_OVERSOLD}) - mozny obrat hore (cena {last:.2f})",
                })

    return signals


# ============================================================
#  GRAF
# ============================================================

def make_chart(name, df):
    """Vykresli posledne sviecky + EMA a vrati cestu k PNG (alebo None)."""
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
        ef = ema(closes, EMA_FAST)
        es = ema(closes, EMA_SLOW)
        x = range(len(closes))
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(x, closes, color="#222", linewidth=1.6, label="Cena")
        ax.plot(x, ef, color="#1f9d55", linewidth=1.0, label=f"EMA{EMA_FAST}")
        ax.plot(x, es, color="#c0392b", linewidth=1.0, label=f"EMA{EMA_SLOW}")
        ax.set_title(name)
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f"_chart_{name.split()[0].replace('/', '')}.png",
        )
        fig.savefig(path, dpi=90)
        plt.close(fig)
        return path
    except Exception as e:
        print(f"!! Chyba pri grafe: {e}")
        return None


def send_telegram_photo(path: str, caption: str) -> bool:
    """Posle obrazok na Telegram."""
    if "SEM_VLOZ" in TELEGRAM_BOT_TOKEN or not TELEGRAM_BOT_TOKEN:
        return False
    chat_id = resolve_chat_id()
    if not chat_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(path, "rb") as f:
            r = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f},
                timeout=30,
            )
        return r.status_code == 200
    except Exception as e:
        print(f"!! Telegram foto chyba: {e}")
        return False


# ============================================================
#  HLAVNA SLUCKA
# ============================================================

_last_alert = {}  # (instrument, typ) -> datetime poslednej spravy

def on_cooldown(name, typ):
    key = (name, typ)
    now = dt.datetime.now()
    last = _last_alert.get(key)
    if last and (now - last).total_seconds() < COOLDOWN_MINUTES * 60:
        return True
    _last_alert[key] = now
    return False


def fetch(ticker):
    """Nacita historiu pre ticker. Vrati DataFrame alebo None."""
    try:
        df = yf.Ticker(ticker).history(period=PERIOD, interval=INTERVAL)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"!! Chyba pri nacitani {ticker}: {e}")
        return None


def scan_once():
    found = 0
    for name, ticker in INSTRUMENTS.items():
        df = fetch(ticker)
        if df is None:
            continue
        fresh = [s for s in detect_signals(name, df) if not on_cooldown(name, s["typ"])]
        if not fresh:
            continue
        ts = dt.datetime.now().strftime("%H:%M:%S")
        for sig in fresh:
            print(f"[SIGNAL] {sig['sprava']}")
        text = "\n".join(s["sprava"] for s in fresh) + f"\n⏱ {ts}"
        chart = make_chart(name, df) if SEND_CHART else None
        if chart and send_telegram_photo(chart, text):
            pass  # graf + popis odoslane spolu
        else:
            send_telegram(text)
        found += len(fresh)
    return found


def main():
    print("=" * 60)
    print(" SKENER TRHU - spusteny")
    print(f" Instrumenty: {', '.join(INSTRUMENTS.keys())}")
    print(f" Interval skenu: {POLL_SECONDS}s | sviecka: {INTERVAL}")
    print(f" Signaly: rapid={ENABLE_RAPID} trend={ENABLE_TREND} "
          f"breakout={ENABLE_BREAKOUT} rsi={ENABLE_RSI} graf={SEND_CHART}")
    print("=" * 60)

    # RUN_ONCE: jeden sken a koniec (pre GitHub Actions / cloud cron).
    if os.environ.get("RUN_ONCE", "").strip() in ("1", "true", "True", "yes"):
        n = scan_once()
        print(f"[RUN_ONCE] sken hotovy, signalov: {n}")
        return

    # uvodna sprava do Telegramu (test spojenia)
    send_telegram("✅ Skener trhu spusteny a sleduje trh.")

    while True:
        try:
            n = scan_once()
            stamp = dt.datetime.now().strftime("%H:%M:%S")
            print(f"[{stamp}] sken hotovy, signalov: {n}")
        except KeyboardInterrupt:
            print("\nUkoncene pouzivatelom.")
            break
        except Exception as e:
            print(f"!! Chyba v slucke: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
