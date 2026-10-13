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


# ---------- NSE derivatives (futures / options) ----------
HDR = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
       "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"}


def nse_json(url):
    s = requests.Session()
    s.get("https://www.nseindia.com", headers=HDR, timeout=10)
    r = s.get(url, headers=HDR, timeout=10)
    r.raise_for_status()
    return r.json()


def show_futures(sym):
    data = nse_json(f"https://www.nseindia.com/api/quote-derivative?symbol={sym}")
    rows = []
    for item in data.get("stocks", []):
        m = item.get("metadata", {})
        if m.get("instrumentType") == "Stock Futures":
            rows.append({"Expiry": m.get("expiryDate"),
                         "LTP": m.get("lastPrice"),
                         "Change %": m.get("pChange"),
                         "Open": m.get("openPrice"),
                         "High": m.get("highPrice"),
                         "Low": m.get("lowPrice")})
    if not rows:
        st.warning("No futures data found (this stock may not have F&O).")
        return
    st.subheader(f"{sym} Futures")
    st.dataframe(pd.DataFrame(rows), hide_index=True)


def show_options(sym):
    data = nse_json(f"https://www.nseindia.com/api/option-chain-equities?symbol={sym}")
    rec = data.get("records", {})
    expiries = rec.get("expiryDates", [])
    spot = rec.get("underlyingValue")
    if not expiries:
        st.warning("No options data found (this stock may not have F&O).")
        return
    expiry = st.selectbox("Expiry", expiries)
    rows = []
    for d in rec.get("data", []):
        if d.get("expiryDate") != expiry:
            continue
        ce, pe = d.get("CE", {}), d.get("PE", {})
        rows.append({"Strike": d["strikePrice"],
                     "CE OI": ce.get("openInterest", 0),
                     "CE ΔOI": ce.get("changeinOpenInterest", 0),
                     "CE LTP": ce.get("lastPrice", 0),
                     "PE LTP": pe.get("lastPrice", 0),
                     "PE ΔOI": pe.get("changeinOpenInterest", 0),
                     "PE OI": pe.get("openInterest", 0)})
    df = pd.DataFrame(rows).sort_values("Strike")
    st.subheader(f"{sym} Options  (spot ₹{spot})")
    pcr = df["PE OI"].sum() / max(df["CE OI"].sum(), 1)
    res = df.loc[df["CE OI"].idxmax(), "Strike"]
    sup = df.loc[df["PE OI"].idxmax(), "Strike"]
    st.write(f"PCR (put/call OI): **{pcr:.2f}** "
             f"({'bullish bias' if pcr > 1.2 else 'bearish bias' if pcr < 0.7 else 'neutral'})")
    st.write(f"Resistance (max call OI): **{res}** | Support (max put OI): **{sup}**")
    if spot:
        atm = (df["Strike"] - spot).abs().idxmin()
        pos = df.index.get_loc(atm)
        df = df.iloc[max(pos - 6, 0): pos + 7]
    st.dataframe(df, hide_index=True)


# ---------- Run ----------
if go:
    if not symbol:
        st.warning("Select a stock first.")
    elif mode == "Stock":
        show_stock(symbol)
    else:
        try:
            show_futures(symbol) if mode == "Futures" else show_options(symbol)
        except Exception as e:
            st.error("Could not fetch NSE derivatives data. NSE often blocks "
                     "cloud servers. A broker API is the reliable fix.")
            st.caption(f"Details: {e}")
    st.caption("Educational tool only, not financial advice.")
