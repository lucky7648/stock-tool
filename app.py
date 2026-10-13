import streamlit as st
import yfinance as yf
import pandas as pd

st.set_page_config(page_title="My Stock Tool", layout="centered")
st.title("📈 My Stock Tool")

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

symbol = st.text_input("NSE stock symbol", "RELIANCE").strip().upper()
ticker = symbol if symbol.endswith(".NS") else symbol + ".NS"
use_ai = st.checkbox("AI news summary (needs Claude API key)")
go = st.button("Analyse", type="primary")


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def atr(df, n=14):
    pc = df["Close"].shift()
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n).mean()


if go:
    t = yf.Ticker(ticker)
    df = t.history(period="1y")
    if df.empty:
        st.error("No data found. Check the symbol.")
        st.stop()

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

    # ---- Technical ----
    st.subheader("Technical")
    above50 = last["Close"] > last["DMA50"]
    macd_up = last["MACD"] > last["SIG"]
    st.write(f"RSI: **{last['RSI']:.1f}**")
    st.write(f"Price vs 50-DMA: **{'above' if above50 else 'below'}**")
    st.write(f"Price vs 200-DMA: **{'above' if last['Close'] > last['DMA200'] else 'below'}**")
    st.write(f"MACD: **{'bullish' if macd_up else 'bearish'}**")
    st.write(f"Support (20d low): **₹{df['Low'].tail(20).min():.2f}** | "
             f"Resistance (20d high): **₹{df['High'].tail(20).max():.2f}**")

    # ---- Entry / Exit ----
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

    # ---- Fundamentals ----
    st.subheader("Fundamentals")
    info = t.info or {}
    fields = {
        "PE": "trailingPE",
        "ROE": "returnOnEquity",
        "Debt/Equity": "debtToEquity",
        "Earnings growth": "earningsGrowth",
        "Market cap": "marketCap",
    }
    for label, key in fields.items():
        st.write(f"{label}: **{info.get(key, 'N/A')}**")

    # ---- News ----
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
                model="claude-sonnet-5-5",
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": f"Stock: {ticker}. In 4 short lines, give the overall "
                               f"sentiment (bullish/neutral/bearish) and why, from "
                               f"these headlines:\n" + "\n".join(titles),
                }],
            )
            st.info(msg.content[0].text)
        except Exception as e:
            st.warning(f"AI summary failed: {e}")

    st.caption("Educational tool only, not financial advice. Backtest and paper-trade first.")
