import os, json
import numpy as np, pandas as pd, yfinance as yf, requests

# ===== UNIVERSO (editable) — ticker: (nombre, sector) =====
TICKERS = {
    "SPY": ("S&P 500 (ETF)", "Índices"), "QQQ": ("Nasdaq 100 (ETF)", "Índices"), "DIA": ("Dow Jones (ETF)", "Índices"),
    "XOM": ("Exxon Mobil", "Energía"), "CVX": ("Chevron", "Energía"), "COP": ("ConocoPhillips", "Energía"),
    "EOG": ("EOG Resources", "Energía"), "SLB": ("SLB", "Energía"), "OXY": ("Occidental", "Energía"),
    "PSX": ("Phillips 66", "Energía"), "MPC": ("Marathon Petroleum", "Energía"),
    "AAPL": ("Apple", "Tecnología"), "MSFT": ("Microsoft", "Tecnología"), "NVDA": ("Nvidia", "Tecnología"),
    "AVGO": ("Broadcom", "Tecnología"), "AMD": ("AMD", "Tecnología"), "GOOGL": ("Alphabet", "Tecnología"),
    "META": ("Meta", "Tecnología"), "AMZN": ("Amazon", "Tecnología"), "ORCL": ("Oracle", "Tecnología"),
    "CRM": ("Salesforce", "Tecnología"), "ADBE": ("Adobe", "Tecnología"), "QCOM": ("Qualcomm", "Tecnología"),
    "CSCO": ("Cisco", "Tecnología"),
    "UNH": ("UnitedHealth", "Salud"), "JNJ": ("Johnson & Johnson", "Salud"), "LLY": ("Eli Lilly", "Salud"),
    "ABBV": ("AbbVie", "Salud"), "MRK": ("Merck", "Salud"), "PFE": ("Pfizer", "Salud"),
    "TMO": ("Thermo Fisher", "Salud"), "ABT": ("Abbott", "Salud"), "AMGN": ("Amgen", "Salud"),
    "GILD": ("Gilead", "Salud"), "ISRG": ("Intuitive Surgical", "Salud"),
}
SMA_N = 6
NY, LON = "America/New_York", "Europe/London"
# tf: (intervalo Yahoo, periodo, minutos, VWAP D=diario W=semanal, velas que se guardan)
CFG = {
    "5m":  ("5m",  "5d",  5,  "D", 300),
    "15m": ("15m", "10d", 15, "D", 200),
    "30m": ("30m", "20d", 30, "D", 200),
    "1h":  ("1h",  "3mo", 60, "D", 200),
}
KEEP_4H = 120
TOKEN, CHAT = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
NOW = pd.Timestamp.now(tz=NY)


def load_json(p, default):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def get(raw, t):
    try:
        d = raw[t][["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
    except KeyError:
        return None
    d = d[d.Volume > 0]
    if d.empty:
        return None
    d.index = d.index.tz_convert(NY)
    return d


def add_vwap(d, mode):
    key = d.index.strftime("%Y-%m-%d" if mode == "D" else "%G-%V")
    tp = (d.High + d.Low + d.Close) / 3
    d["P"] = pd.factorize(key)[0]
    d["VWAP"] = (tp * d.Volume).groupby(key).cumsum() / d.Volume.groupby(key).cumsum()
    return d


def closed(d, minutes):
    """Solo velas cerradas (la última de la sesión cierra a las 16:00 NY)."""
    ends = d.index + pd.Timedelta(minutes=minutes)
    cap = d.index.normalize() + pd.Timedelta(hours=16)
    ends = ends.where(ends < cap, cap)
    return d[ends <= NOW]


def to_4h(d):
    """Bloques 09:30-13:30 y 13:30-16:00 NY construidos desde 1H."""
    mins = d.index.hour * 60 + d.index.minute
    key = [f"{a}_{int(b)}" for a, b in zip(d.index.strftime("%Y-%m-%d"), mins >= 810)]
    o = d.assign(T=d.index).groupby(key, sort=False).agg(
        T=("T", "first"), Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), VWAP=("VWAP", "last"), P=("P", "last"),
    ).set_index("T")
    m = o.index.hour * 60 + o.index.minute
    end = o.index.normalize() + pd.to_timedelta(np.where(m < 810, 810, 960), unit="m")
    return o[end <= NOW]


def pack(d, n):
    d = d.dropna(subset=["VWAP"]).tail(n)
    if len(d) < SMA_N + 2:
        return {"err": "Historial insuficiente"}
    r2 = lambda s: [round(float(x), 2) for x in s]
    return {"t": [int(x.timestamp()) for x in d.index], "o": r2(d.Open), "h": r2(d.High),
            "l": r2(d.Low), "c": r2(d.Close), "vw": r2(d.VWAP), "p": [int(x) for x in d.P]}


def last_signal(x):
    c, vw, p = x["c"], x["vw"], x["p"]
    if len(c) < SMA_N + 1 or p[-1] != p[-2]:   # cruce por reinicio del VWAP: no cuenta
        return None
    s1 = sum(c[-SMA_N:]) / SMA_N
    s0 = sum(c[-SMA_N - 1:-1]) / SMA_N
    if s0 <= vw[-2] and s1 > vw[-1]:
        return "COMPRA", s1
    if s0 >= vw[-2] and s1 < vw[-1]:
        return "VENDE", s1
    return None


def send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e)


def main():
    syms = list(TICKERS)
    state = load_json("state.json", {})
    out = {"updated": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), "sma": SMA_N,
           "tickers": [{"s": k, "name": v[0], "sec": v[1]} for k, v in TICKERS.items()], "d": {}}
    raw1h = None

    for tf, (iv, per, mins, mode, keep) in CFG.items():
        raw = yf.download(syms, interval=iv, period=per, group_by="ticker",
                          auto_adjust=False, prepost=False, progress=False, threads=True)
        if tf == "1h":
            raw1h = raw
        out["d"][tf] = {}
        for s in syms:
            d = get(raw, s)
            out["d"][tf][s] = pack(closed(add_vwap(d, mode), mins), keep) if d is not None else {"err": "Sin datos de Yahoo"}

    out["d"]["4h"] = {}
    for s in syms:
        d = get(raw1h, s)
        out["d"]["4h"][s] = pack(to_4h(closed(add_vwap(d, "W"), 60)), KEEP_4H) if d is not None else {"err": "Sin datos de Yahoo"}

    nuevas = []
    for tf, block in out["d"].items():
        for s, x in block.items():
            if "err" in x:
                continue
            r = last_signal(x)
            if not r:
                continue
            k = f"{s}|{tf}"
            if state.get(k) == x["t"][-1]:
                continue
            state[k] = x["t"][-1]
            hora = pd.Timestamp(x["t"][-1], unit="s", tz="UTC").tz_convert(LON).strftime("%d/%m %H:%M")
            nuevas.append(f"{'🟢' if r[0] == 'COMPRA' else '🔴'} {r[0]} · {s} · {tf}\n"
                          f"Precio {x['c'][-1]} | SMA6 {round(r[1], 2)} | VWAP {x['vw'][-1]}\n"
                          f"Vela {hora} (Londres)")

    if nuevas and TOKEN and CHAT:
        msg = ""
        for line in nuevas:
            if len(msg) + len(line) > 3800:
                send(msg); msg = ""
            msg += line + "\n\n"
        if msg:
            send(msg)

    with open("data.json", "w") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
    with open("state.json", "w") as f:
        json.dump(state, f)
    print(f"{len(nuevas)} señales nuevas")


if __name__ == "__main__":
    main()
