import os, json
import numpy as np, pandas as pd, yfinance as yf, requests

# ===== UNIVERSO (editable) =====
TICKERS = [
    # Índices vía ETF (los índices no traen volumen intradía en Yahoo)
    "SPY", "QQQ", "DIA",
    # Energía
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "PSX", "MPC",
    # Tecnología
    "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "GOOGL", "META", "AMZN", "ORCL", "CRM", "ADBE", "QCOM", "CSCO",
    # Health care
    "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "AMGN", "GILD", "ISRG",
]
SMA_N = 6
NY, LON = "America/New_York", "Europe/London"
CFG = {  # tf: (intervalo Yahoo, periodo, minutos, modo VWAP D=diario W=semanal)
    "5m":  ("5m",  "5d",  5,  "D"),
    "15m": ("15m", "5d",  15, "D"),
    "30m": ("30m", "10d", 30, "D"),
    "1h":  ("1h",  "1mo", 60, "D"),
}
TOKEN, CHAT = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
NOW = pd.Timestamp.now(tz=NY)


def load(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def get(raw, t):
    try:
        d = raw[t][["High", "Low", "Close", "Volume"]].dropna().copy()
    except KeyError:
        return None
    if d.empty:
        return None
    d.index = d.index.tz_convert(NY)
    return d[d.Volume > 0]


def add_vwap(d, mode):
    key = d.index.strftime("%Y-%m-%d" if mode == "D" else "%G-%V")
    tp = (d.High + d.Low + d.Close) / 3
    d["P"] = key
    d["VWAP"] = (tp * d.Volume).groupby(key).cumsum() / d.Volume.groupby(key).cumsum()
    return d


def closed(d, minutes):
    """Solo velas ya cerradas (la última vela de la sesión cierra a las 16:00 NY)."""
    ends = d.index + pd.Timedelta(minutes=minutes)
    cap = d.index.normalize() + pd.Timedelta(hours=16)
    ends = ends.where(ends < cap, cap)
    return d[ends <= NOW]


def to_4h(d):
    """Agrupa velas de 1H en bloques 09:30-13:30 y 13:30-16:00 NY."""
    mins = d.index.hour * 60 + d.index.minute
    key = [f"{a}_{int(b)}" for a, b in zip(d.index.strftime("%Y-%m-%d"), mins >= 810)]
    o = d.assign(T=d.index).groupby(key, sort=False).agg(
        T=("T", "first"), Close=("Close", "last"), VWAP=("VWAP", "last"), P=("P", "last")
    ).set_index("T")
    m = o.index.hour * 60 + o.index.minute
    end = o.index.normalize() + pd.to_timedelta(np.where(m < 810, 810, 960), unit="m")
    return o[end <= NOW]


def check(t, tf, d, state, nuevas, estado):
    d = d.dropna(subset=["VWAP"]).copy()
    if len(d) < SMA_N + 1:
        return
    d["SMA"] = d.Close.rolling(SMA_N).mean()
    a, b = d.iloc[-2], d.iloc[-1]
    if pd.isna(a.SMA):
        return
    estado.setdefault(t, {})[tf] = "ARRIBA" if b.SMA > b.VWAP else "ABAJO"
    if a.P != b.P:  # cruce provocado por el reinicio del VWAP: se ignora
        return
    sig = None
    if a.SMA <= a.VWAP and b.SMA > b.VWAP:
        sig = "COMPRA"
    elif a.SMA >= a.VWAP and b.SMA < b.VWAP:
        sig = "VENDE"
    if not sig:
        return
    ts = d.index[-1]
    k = f"{t}|{tf}"
    if state.get(k) == str(ts):
        return
    state[k] = str(ts)
    nuevas.append({
        "ticker": t, "tf": tf, "senal": sig,
        "hora": ts.tz_convert(LON).strftime("%d/%m %H:%M"),
        "precio": round(float(b.Close), 2),
        "sma": round(float(b.SMA), 2),
        "vwap": round(float(b.VWAP), 2),
    })


def send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e)


def main():
    state = load("state.json", {})
    hist = load("signals.json", {}).get("senales", [])
    estado, nuevas, raw1h = {}, [], None

    for tf, (iv, per, mins, mode) in CFG.items():
        raw = yf.download(TICKERS, interval=iv, period=per, group_by="ticker",
                          auto_adjust=False, prepost=False, progress=False, threads=True)
        if tf == "1h":
            raw1h = raw
        for t in TICKERS:
            d = get(raw, t)
            if d is not None:
                check(t, tf, closed(add_vwap(d, mode), mins), state, nuevas, estado)

    # 4H con VWAP semanal, construido desde 1H
    for t in TICKERS:
        d = get(raw1h, t)
        if d is not None:
            check(t, "4h", to_4h(closed(add_vwap(d, "W"), 60)), state, nuevas, estado)

    if nuevas and TOKEN and CHAT:
        msg = ""
        for s in nuevas:
            ico = "🟢" if s["senal"] == "COMPRA" else "🔴"
            line = (f"{ico} {s['senal']} · {s['ticker']} · {s['tf']}\n"
                    f"Precio {s['precio']} | SMA6 {s['sma']} | VWAP {s['vwap']}\n"
                    f"Vela {s['hora']} (Londres)")
            if len(msg) + len(line) > 3800:
                send(msg); msg = ""
            msg += line + "\n\n"
        if msg:
            send(msg)

    with open("signals.json", "w") as f:
        json.dump({"actualizado": NOW.tz_convert(LON).strftime("%d/%m/%Y %H:%M"),
                   "senales": (hist + nuevas)[-300:], "estado": estado}, f, ensure_ascii=False)
    with open("state.json", "w") as f:
        json.dump(state, f)
    print(f"{len(nuevas)} señales nuevas")


if __name__ == "__main__":
    main()
