import io
import requests
import streamlit as st
import yfinance as yf
import pandas as pd

st.set_page_config(page_title="My Stock Tool", layout="centered")
st.title("📈 My Stock Tool")

# ---------- Password gate ----------
try:
    APP_PASSWORD = st.secrets["APP_PASSWORD"]
except Exception:
    st.error("APP_PASSWORD is not set in Secrets.")
    st.stop()

if not st.session_state.get("authed"):
    pw = st.text_input("Password", type="password")
    if pw:
        if pw == APP_PASSWORD:
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()

# ---------- Symbol list (autocomplete) ----------
NIFTY50 = ("RELIANCE TCS HDFCBANK INFY ICICIBANK HINDUNILVR ITC SBIN BHARTIARTL "
           "KOTAKBANK LT AXISBANK ASIANPAINT MARUTI SUNPHARMA TITAN BAJFINANCE "
           "HCLTECH WIPRO ULTRACEMCO NTPC ONGC POWERGRID TATAMOTORS TATASTEEL M&M "
           "ADANIENT ADANIPORTS COALINDIA JSWSTEEL TECHM NESTLEIND BAJAJFINSV "
           "INDUSINDBK HINDALCO DRREDDY CIPLA EICHERMOT GRASIM BPCL BRITANNIA "
           "DIVISLAB HEROMOTOCO APOLLOHOSP SBILIFE HDFCLIFE TATACONSUM UPL "
           "BAJAJ-AUTO SHRIRAMFIN").split()


@st.cache_data(ttl=86400)
def load_symbols():
    try:
        r = requests.get(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        d = pd.read_csv(io.StringIO(r.text))
        d.columns = [c.strip() for c in d.columns]
        d = d[d["SERIES"].str.strip() == "EQ"]
        return {s.strip(): f"{s.strip()} - {n.strip()}"
                for s, n in zip(d["SYMBOL"], d["NAME OF COMPANY"])}
    except Exception:
        return {s: s for s in NIFTY50}


labels = load_symbols()
symbol = st.selectbox(
    "Stock (type 2-3 letters)", options=list(labels.keys()),
    format_func=lambda s: labels[s], index=None,
    placeholder="e.g. REL, TCS, HDFC")

mode = st.radio("What do you want to see?", ["Stock", "Futures", "Options"],
                horizontal=True)
use_ai = st.checkbox("AI news summary (needs Claude API key)")
go = st.button("Analyse", type="primary")


# ---------- Indicators ----------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def atr(df, n=14):
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(),
                    (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


# ---------- Stock view ----------
def show_stock(sym):
    ticker = sym + ".NS"
    t = yf.Ticker(ticker)
    df = t.history(period="1y")
    if df.empty:
        st.error("No data found for this symbol.")
        return
    c = df["Close"]
    df["DMA50"] = c.rolling(50).mean()
    df["DMA200"] = c.rolling(200).mean()
    df["RSI"] = rsi(c)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["MACD"] = macd
    df["SIG"] = macd.ewm(span=9, adjust=False).mean()
    df["ATR"] = atr(df)
    last = df.iloc[-1]

    st.subheader(f"{ticker}  ₹{last['Close']:.2f}")
    st.line_chart(df[["Close", "DMA50", "DMA200"]])

    st.subheader("Technical")
    above50 = last["Close"] > last["DMA50"]
    macd_up = last["MACD"] > last["SIG"]
    st.write(f"RSI: **{last['RSI']:.1f}**")
    st.write(f"Price vs 50-DMA: **{'above' if above50 else 'below'}**")
    st.write(f"Price vs 200-DMA: **{'above' if last['Close'] > last['DMA200'] else 'below'}**")
    st.write(f"MACD: **{'bullish' if macd_up else 'bearish'}**")
    st.write(f"Support (20d low): **₹{df['Low'].tail(20).min():.2f}** | "
             f"Resistance (20d high): **₹{df['High'].tail(20).max():.2f}**")

    st.subheader("Entry / Exit")
    entry_ok = above50 and macd_up and 40 <= last["RSI"] <= 60
    stop = last["Close"] - 1.5 * last["ATR"]
    target = last["Close"] + 2 * (last["Close"] - stop)
    if entry_ok:
        st.success("Entry conditions met")
    elif last["RSI"] > 70:
        st.warning("Overbought: avoid fresh entry")
    else:
        st.info("Entry conditions not met: wait")
    st.write(f"Reference entry: **₹{last['Close']:.2f}**")
    st.write(f"Stop-loss (1.5 x ATR): **₹{stop:.2f}**")
    st.write(f"Target (1:2 risk-reward): **₹{target:.2f}**")

    st.subheader("Fundamentals")
    info = t.info or {}
    for label, key in {"PE": "trailingPE", "ROE": "returnOnEquity",
                       "Debt/Equity": "debtToEquity",
                       "Earnings growth": "earningsGrowth",
                       "Market cap": "marketCap"}.items():
        st.write(f"{label}: **{info.get(key, 'N/A')}**")

    st.subheader("Latest news")
    titles = []
    for n in (t.news or [])[:8]:
        title = n.get("title") or n.get("content", {}).get("title")
        if title:
            titles.append(title)
            st.write("•", title)

    if use_ai and titles:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
            msg = client.messages.create(
                model="claude-sonnet-5-5", max_tokens=400,
                messages=[{"role": "user", "content":
                           f"Stock: {ticker}. In 4 short lines, give the overall "
                           f"sentiment (bullish/neutral/bearish) and why, from "
                           f"these headlines:\n" + "\n".join(titles)}])
            st.info(msg.content[0].text)
        except Exception as e:
            st.warning(f"AI summary failed: {e}")


# ---------- Angel One (futures / options) ----------
SCRIP_URL = ("https://margincalculator.angelbroking.com/OpenAPI_File/files/"
             "OpenAPIScripMaster.json")


@st.cache_resource(ttl=6 * 3600)
def angel_login():
    import pyotp
    from SmartApi import SmartConnect
    obj = SmartConnect(api_key=st.secrets["ANGEL_API_KEY"])
    totp = pyotp.TOTP(st.secrets["ANGEL_TOTP_SECRET"]).now()
    data = obj.generateSession(st.secrets["ANGEL_CLIENT_ID"],
                               st.secrets["ANGEL_PIN"], totp)
    if not data.get("status"):
        raise RuntimeError(f"Angel One login failed: {data.get('message')}")
    return obj


@st.cache_data(ttl=43200, show_spinner="Loading F&O contracts...")
def load_fno():
    rows = requests.get(SCRIP_URL, timeout=90).json()
    df = pd.DataFrame(rows)
    df = df[(df["exch_seg"] == "NFO") &
            (df["instrumenttype"].isin(["FUTSTK", "OPTSTK"]))].copy()
    df["strike"] = df["strike"].astype(float) / 100
    df["lotsize"] = df["lotsize"].astype(int)
    df["exp"] = pd.to_datetime(df["expiry"], format="%d%b%Y")
    df = df[df["exp"] >= pd.Timestamp.today().normalize()]
    return df[["token", "symbol", "name", "exp", "expiry", "strike",
               "lotsize", "instrumenttype"]]


@st.cache_data(ttl=15)
def get_quotes(tokens):
    for attempt in range(2):
        try:
            res = angel_login().getMarketData("FULL", {"NFO": list(tokens)})
            if res.get("status"):
                return {d["symbolToken"]: d for d in res["data"]["fetched"]}
            raise RuntimeError(res.get("message", "unknown error"))
        except Exception:
            if attempt == 1:
                raise
            angel_login.clear()


def spot_price(sym):
    h = yf.Ticker(sym + ".NS").history(period="5d")
    return float(h["Close"].iloc[-1]) if not h.empty else None


def show_futures(sym):
    fno = load_fno()
    f = fno[(fno["name"] == sym) & (fno["instrumenttype"] == "FUTSTK")]
    f = f.sort_values("exp")
    if f.empty:
        st.warning("No futures found (this stock may not have F&O).")
        return
    q = get_quotes(tuple(f["token"]))
    spot = spot_price(sym)
    rows = []
    for _, r in f.iterrows():
        d = q.get(r["token"], {})
        ltp = d.get("ltp")
        rows.append({"Expiry": r["expiry"], "LTP": ltp,
                     "Chg %": d.get("percentChange"),
                     "OI": d.get("opnInterest"),
                     "Volume": d.get("tradeVolume"),
                     "Lot": r["lotsize"],
                     "Vs spot": round(ltp - spot, 2) if ltp and spot else None})
    st.subheader(f"{sym} Futures" + (f"  (spot ₹{spot:.2f})" if spot else ""))
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("Price up + OI up = long buildup. Price down + OI up = short "
               "buildup. Compare OI with the previous day.")


def show_options(sym):
    fno = load_fno()
    o = fno[(fno["name"] == sym) & (fno["instrumenttype"] == "OPTSTK")]
    if o.empty:
        st.warning("No options found (this stock may not have F&O).")
        return
    expiries = sorted(o["expiry"].unique(),
                      key=lambda e: pd.to_datetime(e, format="%d%b%Y"))
    expiry = st.selectbox("Expiry", expiries)
    o = o[o["expiry"] == expiry]
    spot = spot_price(sym)
    strikes = sorted(o["strike"].unique())
    if spot:
        i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
    else:
        i = len(strikes) // 2
    window = strikes[max(i - 10, 0): i + 11]
    o = o[o["strike"].isin(window)]
    q = get_quotes(tuple(o["token"]))

    rows = []
    for k in window:
        row = {"Strike": k}
        for side in ("CE", "PE"):
            m = o[(o["strike"] == k) & (o["symbol"].str.endswith(side))]
            d = q.get(m["token"].iloc[0], {}) if not m.empty else {}
            row[f"{side} LTP"] = d.get("ltp")
            row[f"{side} OI"] = d.get("opnInterest")
        rows.append(row)
    df = pd.DataFrame(rows)
    ce = df["CE OI"].fillna(0)
    pe = df["PE OI"].fillna(0)

    st.subheader(f"{sym} Options" + (f"  (spot ₹{spot:.2f})" if spot else ""))
    st.write(f"Lot size: **{int(o['lotsize'].iloc[0])}**")
    pcr = pe.sum() / max(ce.sum(), 1)
    st.write(f"PCR (near spot): **{pcr:.2f}** "
             f"({'bullish bias' if pcr > 1.2 else 'bearish bias' if pcr < 0.7 else 'neutral'})")
    st.write(f"Resistance (max call OI): **{df.loc[ce.idxmax(), 'Strike']}** | "
             f"Support (max put OI): **{df.loc[pe.idxmax(), 'Strike']}**")
    st.dataframe(df, hide_index=True)
    st.caption("Shows 10 strikes above and below the spot price.")


# ---------- Run ----------
if go:
    st.session_state["run"] = True

if st.session_state.get("run"):
    if not symbol:
        st.warning("Select a stock first.")
    elif mode == "Stock":
        show_stock(symbol)
    else:
        try:
            if mode == "Futures":
                show_futures(symbol)
            else:
                show_options(symbol)
        except Exception as e:
            st.error("Could not fetch Angel One data.")
            st.caption(f"Details: {e}")
    st.caption("Educational tool only, not financial advice.")
