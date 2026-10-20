import io
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import altair as alt
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

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
tab_a, tab_m, tab_i, tab_p = st.tabs(["🔍 Analyse", "🔥 Movers", "💡 Ideas", "📒 Paper"])

with tab_a:
    symbol = st.selectbox(
        "Stock (type 2-3 letters)", options=list(labels.keys()),
        format_func=lambda s: labels[s], index=None,
        placeholder="e.g. REL, TCS, HDFC", key="symbol_sel")
    st.caption("Tip: you can also type part of a company name, like tata, bank or pharma.")
    company = labels[symbol].split(" - ", 1)[-1] if symbol else ""

    mode = st.radio("What do you want to see?", ["Stock", "Futures", "Options"],
                    horizontal=True, key="mode_sel")

    with st.expander("⚙️ My capital and risk"):
        capital = st.number_input("Trading capital (₹)", min_value=10000,
                                  value=100000, step=10000)
        risk_pct = st.slider("Max loss per trade (% of capital)", 0.5, 3.0, 1.0, 0.5)
        st.caption("Rule: never risk more than 1-2% of your capital on one trade.")

    use_ai = st.checkbox("AI news summary (needs Claude API key)")
    go = st.button("Analyse", type="primary")


# ---------- Data helpers ----------
@st.cache_data(ttl=300)
def get_hist(ticker, period, interval):
    try:
        h = yf.Ticker(ticker).history(period=period, interval=interval)
        return h.dropna(subset=["Close"]) if not h.empty else h
    except Exception:
        return pd.DataFrame()


INFO_KEYS = ["trailingPE", "returnOnEquity", "debtToEquity", "earningsGrowth",
             "marketCap", "sector", "profitMargins"]


def screener_data(sym):
    out = {}
    hdr = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
    for path in ("consolidated/", ""):
        try:
            r = requests.get(f"https://www.screener.in/company/{quote_plus(sym)}/{path}",
                             headers=hdr, timeout=10)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        pairs = re.findall(r'<span class="name">\s*(.*?)\s*</span>\s*'
                           r'<span class="nowrap value">(.*?)</li>', r.text, re.S)
        for name, val in pairs:
            m = re.search(r'class="number">\s*([-\d,\.]+)', val)
            if not m:
                continue
            try:
                num = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            name = re.sub(r"<.*?>|\s+", " ", name).strip()
            if name == "Stock P/E":
                out["trailingPE"] = num
            elif name == "ROE":
                out["returnOnEquity"] = num / 100
            elif name == "Market Cap":
                out["marketCap"] = num * 1e7
        if out:
            break
    return out


@st.cache_data(ttl=3600)
def get_info(sym):
    out = {k: None for k in INFO_KEYS}
    src = "none"
    try:
        i = yf.Ticker(sym + ".NS").info or {}
        for k in INFO_KEYS:
            out[k] = i.get(k)
        if any(isnum(out[k]) for k in ("trailingPE", "returnOnEquity", "earningsGrowth")):
            src = "Yahoo Finance"
    except Exception:
        pass
    if src == "none":
        sc = screener_data(sym)
        if sc:
            out.update(sc)
            src = "Screener.in"
    out["source"] = src
    return out


def market_up():
    n = get_hist("^NSEI", "1y", "1d")
    if n.empty or len(n) < 60:
        return None
    return bool(n["Close"].iloc[-1] > n["Close"].rolling(50).mean().iloc[-1])


POS_STEMS = ("profit", "surg", "gain", "jump", "record", "upgrad", "growth",
             "rally", "strong", "bullish", "dividend", "expan", "beats")
POS_EXACT = {"beat", "win", "wins", "buy"}
NEG_STEMS = ("loss", "slump", "downgrad", "fraud", "penalt", "weak", "plunge",
             "declin", "bearish", "lawsuit", "default", "crash", "slide",
             "raid", "probe", "fall", "drop")
NEG_EXACT = {"sell", "cut", "ban"}


@st.cache_data(ttl=900)
def get_news(query, sym):
    items = []
    try:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(query) +
               "&hl=en-IN&gl=IN&ceid=IN:en")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            src = it.find("source")
            items.append({"title": it.findtext("title") or "",
                          "link": it.findtext("link") or "",
                          "date": (it.findtext("pubDate") or "")[:16],
                          "source": src.text if src is not None else ""})
            if len(items) >= 8:
                break
    except Exception:
        pass
    if not items:
        try:
            for n in (yf.Ticker(sym + ".NS").news or [])[:8]:
                c = n.get("content", n)
                title = c.get("title")
                link = (c.get("canonicalUrl") or {}).get("url") or c.get("link", "")
                if title:
                    items.append({"title": title, "link": link, "date": "",
                                  "source": (c.get("provider") or {}).get("displayName", "")})
        except Exception:
            pass
    return items


def news_tone(items):
    pos = neg = 0
    for it in items:
        for w in re.findall(r"[a-z]+", it["title"].lower()):
            if w in POS_EXACT or w.startswith(POS_STEMS):
                pos += 1
            if w in NEG_EXACT or w.startswith(NEG_STEMS):
                neg += 1
    if pos - neg >= 2:
        return "positive"
    if neg - pos >= 2:
        return "negative"
    return "mixed"


@st.cache_data(ttl=1800, show_spinner="Asking Claude...")
def ai_summary(ticker, titles):
    import anthropic
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-5-5", max_tokens=400,
        messages=[{"role": "user", "content":
                   f"Stock: {ticker}. In 4 short, simple lines, give the overall "
                   f"sentiment (bullish/neutral/bearish) and why, from these "
                   f"headlines:\n" + "\n".join(titles)}])
    return msg.content[0].text


# ---------- Indicators ----------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down)


def add_indicators(df):
    df = df.copy()
    c = df["Close"]
    df["DMA50"] = c.rolling(50).mean()
    df["DMA200"] = c.rolling(200).mean()
    df["RSI"] = rsi(c)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    df["MACD"] = macd
    df["SIG"] = macd.ewm(span=9, adjust=False).mean()
    pc = c.shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(),
                    (df["Low"] - pc).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()
    df["VOL20"] = df["Volume"].rolling(20).mean()
    return df


def isnum(x):
    return isinstance(x, (int, float)) and not pd.isna(x)


# ---------- Verdict engine ----------
def verdict_engine(df, info, mkt_up):
    last = df.iloc[-1]
    close = float(last["Close"])
    rsi_v = float(last["RSI"]) if isnum(last["RSI"]) else 50.0
    atr_v = float(last["ATR"]) if isnum(last["ATR"]) and last["ATR"] > 0 else close * 0.02
    items = []

    def add(group, pts, ok, text):
        items.append((group, pts, None if ok is None else bool(ok), text))

    # Technical (max 50)
    add("Technical", 8, close > last["DMA50"],
        "Price is above the 50-day average (short-term trend is up)")
    add("Technical", 8, close > last["DMA200"],
        "Price is above the 200-day average (long-term trend is up)")
    add("Technical", 4, last["DMA50"] > last["DMA200"],
        "50-day average is above the 200-day average (healthy trend)")
    add("Technical", 10, last["MACD"] > last["SIG"],
        "MACD is bullish (momentum is building)")
    add("Technical", 10, 40 <= rsi_v <= 65,
        f"RSI is {rsi_v:.0f} (healthy zone is 40-65, above 70 means overheated)")
    prev_vol = float(df["Volume"].iloc[-2]) if len(df) > 1 else 0.0
    add("Technical", 5, prev_vol > float(last["VOL20"]) if isnum(last["VOL20"]) else None,
        "Volume is above its 20-day average (more people are trading it)")
    add("Technical", 5, mkt_up, "Overall market (Nifty) is in an uptrend")

    # Fundamentals (max 30)
    roe = info.get("returnOnEquity")
    de = info.get("debtToEquity")
    eg = info.get("earningsGrowth")
    pe = info.get("trailingPE")
    fin = "financial" in str(info.get("sector", "")).lower()
    add("Fundamentals", 8, (roe > 0.15) if isnum(roe) else None,
        f"ROE is {roe * 100:.0f}% (above 15% is good)" if isnum(roe) else "ROE data not available")
    if fin:
        add("Fundamentals", 7, None, "Debt/equity is not meaningful for banks and NBFCs")
    else:
        add("Fundamentals", 7, (de < 100) if isnum(de) else None,
            f"Debt/equity is {de:.0f}% (below 100% is comfortable)" if isnum(de) else "Debt/equity data not available")
    add("Fundamentals", 8, (eg > 0) if isnum(eg) else None,
        f"Profits are growing ({eg * 100:.0f}%)" if isnum(eg) and eg > 0
        else "Profit growth is positive" if not isnum(eg) else f"Profits are falling ({eg * 100:.0f}%)")
    add("Fundamentals", 7, (0 < pe < 40) if isnum(pe) else None,
        f"PE ratio is {pe:.0f} (below 40 is not too expensive, but it varies by sector)"
        if isnum(pe) else "PE data not available")

    # Risk-reward (max 20)
    support = float(df["Low"].tail(20).min())
    res = float(df["High"].tail(60).max())
    stop = close - 1.5 * atr_v
    risk = close - stop
    target2 = close + 2 * risk
    if res > close + risk:
        target = res
        rr = (res - close) / risk
        tnote = "nearest resistance (60-day high)"
    else:
        target = target2
        rr = 2.0
        tnote = "1:2 risk-reward (price is near its 60-day high)"
    rr_pts = 20 if rr >= 2 else 12 if rr >= 1.5 else 5 if rr >= 1 else 0
    if not close > last["DMA50"]:
        rr_pts = 0  # no reward credit while the stock is in a downtrend

    groups = {}
    for g in ("Technical", "Fundamentals"):
        e = sum(p for gg, p, ok, _ in items if gg == g and ok)
        a = sum(p for gg, p, ok, _ in items if gg == g and ok is not None)
        groups[g] = (e, a)
    groups["Risk-reward"] = (rr_pts, 20)
    earned = sum(e for e, a in groups.values())
    avail = sum(a for e, a in groups.values())
    score = round(100 * earned / avail) if avail else 0

    te, ta = groups["Technical"]
    frac = te / ta if ta else 0.5
    bias = "bullish" if frac >= 0.7 else "bearish" if frac <= 0.35 else "neutral"

    trend_ok = bool(close > last["DMA50"])
    trend_bad = bool(close < last["DMA50"] and close < last["DMA200"])
    overbought = rsi_v > 70
    if score >= 70 and rr >= 1.5 and trend_ok and not overbought:
        verdict = "BUY"
        advice = (f"Setup looks good. Enter near ₹{close:.2f} (don't chase if it "
                  f"jumps more than 1-2% higher) and keep the stop-loss at ₹{stop:.2f}.")
    elif score >= 50 and not trend_bad:
        verdict = "WAIT"
        if overbought:
            why = "the stock is overheated (RSI above 70)"
        elif rr < 1.5:
            why = "the reward is small compared to the risk"
        else:
            why = "not all signals agree yet"
        advice = (f"Not ideal right now because {why}. Keep it on your watchlist: "
                  f"look for a dip near support ₹{support:.2f} or a breakout above ₹{res:.2f}.")
    else:
        verdict = "AVOID"
        advice = ("Trend or fundamentals are weak right now. Don't buy. "
                  "Look for better stocks.")
    return dict(score=score, groups=groups, items=items, verdict=verdict,
                advice=advice, entry=close, stop=stop, target=target,
                target2=target2, tnote=tnote, rr=rr, support=support,
                resistance=res, bias=bias, atr=atr_v, rsi=rsi_v)


def get_analysis(sym):
    df = get_hist(sym + ".NS", "2y", "1d")
    if df.empty or len(df) < 60:
        return None
    df = add_indicators(df)
    info = get_info(sym)
    return df, info, verdict_engine(df, info, market_up())


def size_position(entry, stop, cap, rpct):
    vals = (entry, stop, cap, rpct)
    if not all(isnum(x) and abs(x) != float("inf") for x in vals):
        return 0, 0.0, 0.0
    rps = entry - stop
    if rps <= 0 or entry <= 0:
        return 0, 0.0, 0.0
    qty = int((cap * rpct / 100) // rps)
    qty = min(qty, int(cap // entry))
    return qty, qty * entry, qty * rps


TF = {"1D": ("1d", "5m"), "5D": ("5d", "15m"), "1M": ("1mo", "1d"),
      "3M": ("3mo", "1d"), "6M": ("6mo", "1d"), "1Y": ("1y", "1d"),
      "5Y": ("5y", "1wk")}
TF_ROWS = {"1M": 22, "3M": 66, "6M": 130, "1Y": 252}


def draw_chart(sym, df_daily):
    tf = st.radio("Chart time frame", list(TF), index=4, horizontal=True)
    period, interval = TF[tf]
    if tf in TF_ROWS:
        d = df_daily[["Close", "DMA50", "DMA200"]].tail(TF_ROWS[tf])
    else:
        h = get_hist(sym + ".NS", period, interval)
        if h.empty:
            st.info("No data for this time frame (market may be closed).")
            return
        d = h[["Close"]]
    d = d.copy()
    d.index.name = "Date"
    d = d.reset_index()
    d["Date"] = pd.to_datetime(d["Date"]).dt.tz_localize(None)
    long = d.melt(id_vars="Date", var_name="Line", value_name="Price").dropna()
    chart = alt.Chart(long).mark_line().encode(
        x=alt.X("Date:T", title=None),
        y=alt.Y("Price:Q", scale=alt.Scale(zero=False), title=None),
        color=alt.Color("Line:N", legend=alt.Legend(orient="bottom", title=None)),
    ).properties(height=300)
    st.altair_chart(chart, use_container_width=True)


def render_checks(v):
    for g, (e, a) in v["groups"].items():
        st.write(f"**{g}:** {e}/{a} points")
    with st.expander("Why this verdict? (all checks)"):
        for g, pts, ok, text in v["items"]:
            icon = "➖" if ok is None else "✅" if ok else "❌"
            st.write(f"{icon} {text}")


def render_news(sym, name):
    st.subheader("Latest news")
    q = f"{name} share" if name else sym
    news = get_news(q + " when:7d", sym) or get_news(q, sym)
    if not news:
        st.info("No news found right now.")
        return news, "mixed"
    tone = news_tone(news)
    for n in news:
        extra = " | ".join(x for x in (n["source"], n["date"]) if x)
        st.markdown(f"- [{n['title']}]({n['link']})" + (f"  \n  _{extra}_" if extra else ""))
    st.caption(f"Rough news tone (keyword based): **{tone}**")
    return news, tone


def render_glossary():
    with st.expander("📘 Beginner guide: what do these terms mean?"):
        st.markdown(
            "- **Stop-loss:** the price where you exit to limit your loss. Decide it BEFORE buying.\n"
            "- **Risk-reward (R:R):** how much you can gain vs how much you can lose. 1:2 means you risk ₹1 to make ₹2.\n"
            "- **50 / 200-day average:** the average price of the last 50 / 200 days. Price above them = uptrend.\n"
            "- **RSI:** speed of price move (0-100). Above 70 = overheated, below 30 = oversold.\n"
            "- **MACD:** momentum indicator. Bullish when it is above its signal line.\n"
            "- **ATR:** how much the stock moves on a normal day. We use it to set the stop-loss.\n"
            "- **OI (open interest):** number of open F&O contracts. Rising OI with rising price = strong trend.\n"
            "- **PCR:** put OI / call OI. Above 1 leans bullish, below 0.7 leans bearish.\n"
            "- **Lot size:** F&O is traded in fixed quantities (lots), not single shares.\n"
            "- **Theta (time decay):** option premium falls every day, fastest in the last few days before expiry.")


# ---------- Stock view ----------
def show_stock(sym, name):
    res = get_analysis(sym)
    if res is None:
        st.error("No data found for this symbol.")
        return
    df, info, v = res

    st.subheader(f"{sym}  ₹{v['entry']:.2f}")
    box = {"BUY": st.success, "WAIT": st.warning, "AVOID": st.error}[v["verdict"]]
    box(f"### Verdict: {v['verdict']}  ({v['score']}/100)\n{v['advice']}")
    render_checks(v)
    if v["groups"]["Fundamentals"][1] == 0:
        st.caption("Fundamentals were not available, so this score uses technicals and risk-reward only.")

    st.subheader("Trade plan (if you buy)")
    plan = pd.DataFrame({
        "Level": ["Entry", "Stop-loss", f"Target: {v['tnote']}", "Target: 1:2", "Risk-reward"],
        "Price": [f"₹{v['entry']:.2f}", f"₹{v['stop']:.2f}", f"₹{v['target']:.2f}",
                  f"₹{v['target2']:.2f}", f"1 : {v['rr']:.1f}"]})
    st.dataframe(plan, hide_index=True)
    qty, invest, maxloss = size_position(v["entry"], v["stop"], capital, risk_pct)
    if qty > 0:
        st.write(f"With your capital, buy **{qty} shares** (about ₹{invest:,.0f}). "
                 f"If the stop-loss hits you lose about **₹{maxloss:,.0f}** "
                 f"({maxloss / capital * 100:.1f}% of capital). "
                 f"If the target hits you gain about **₹{qty * (v['target'] - v['entry']):,.0f}**.")
    else:
        st.warning("Even 1 share is too expensive or too risky for your capital and risk limit.")
    paper_stock_ticket(sym, v, qty)

    draw_chart(sym, df)

    news, tone = render_news(sym, name)
    if tone == "negative" and v["verdict"] == "BUY":
        st.warning("News tone is negative. Read the headlines before you buy.")
    if use_ai and news:
        try:
            has_key = "ANTHROPIC_API_KEY" in st.secrets
        except Exception:
            has_key = False
        if not has_key:
            st.info("AI summary needs a Claude API key, which is not added yet. "
                    "Everything else works without it.")
        else:
            try:
                st.info(ai_summary(sym, tuple(n["title"] for n in news)))
            except Exception as e:
                st.warning(f"AI summary failed: {e}")

    with st.expander("Fundamental numbers"):
        if info.get("source") == "none":
            st.info("Fundamental data is not available right now (Yahoo sometimes "
                    "blocks cloud servers). The verdict uses technicals and "
                    "risk-reward only.")
        else:
            st.caption(f"Source: {info['source']}")

        def fmt(x, kind):
            if not isnum(x):
                return "N/A"
            if kind == "pct":
                return f"{x * 100:.1f}%"
            if kind == "cr":
                return f"₹{x / 1e7:,.0f} Cr"
            return f"{x:.1f}"

        for label, key, kind in [("PE", "trailingPE", "num"), ("ROE", "returnOnEquity", "pct"),
                                 ("Debt/Equity", "debtToEquity", "num"),
                                 ("Earnings growth", "earningsGrowth", "pct"),
                                 ("Profit margin", "profitMargins", "pct"),
                                 ("Market cap", "marketCap", "cr")]:
            st.write(f"{label}: **{fmt(info.get(key), kind)}**")
    render_glossary()


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


def underlying_box(v):
    box = {"BUY": st.success, "WAIT": st.warning, "AVOID": st.error}[v["verdict"]]
    box(f"Underlying stock verdict: **{v['verdict']}** ({v['score']}/100). "
        f"Trend bias: **{v['bias']}**.")


def show_futures(sym):
    fno = load_fno()
    f = fno[(fno["name"] == sym) & (fno["instrumenttype"] == "FUTSTK")]
    f = f.sort_values("exp")
    if f.empty:
        st.warning("No futures found (this stock may not have F&O).")
        return
    q = get_quotes(tuple(f["token"]))
    res = get_analysis(sym)
    spot = float(res[0]["Close"].iloc[-1]) if res else None
    rows = []
    for _, r in f.iterrows():
        d = q.get(r["token"], {})
        ltp = d.get("ltp")
        rows.append({"Expiry": r["expiry"], "LTP": ltp,
                     "Chg %": d.get("percentChange"),
                     "OI": d.get("opnInterest"),
                     "Volume": d.get("tradeVolume"),
                     "Lot": r["lotsize"],
                     "Premium vs spot ₹": round(ltp - spot, 2) if ltp and spot else None,
                     "Premium %": round((ltp - spot) / spot * 100, 2) if ltp and spot else None})
    st.subheader(f"{sym} Futures")
    if spot and rows[0]["LTP"]:
        diff = rows[0]["LTP"] - spot
        st.write(f"Spot price: **₹{spot:.2f}** | Near-month futures: **₹{rows[0]['LTP']:.2f}** | "
                 f"Premium over spot: **₹{diff:+.2f}** ({diff / spot * 100:+.2f}%)")
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("Futures premium = futures price minus spot price (a minus sign means a discount). "
               "You do not pay a premium for futures, you only block margin. "
               "Price up + OI up = long buildup. Price down + OI up = short buildup.")
    if not res:
        return
    v = res[2]
    underlying_box(v)

    near = rows[0]
    entry, lot = near["LTP"], int(near["Lot"])
    if not entry:
        return
    st.subheader(f"Trade idea (near month, {near['Expiry']})")
    if v["bias"] == "neutral":
        st.warning("No clear direction right now. Staying out is a valid decision.")
        return
    long_side = v["bias"] == "bullish"
    risk_pts = 1.5 * v["atr"]
    stop = entry - risk_pts if long_side else entry + risk_pts
    target = entry + 2 * risk_pts if long_side else entry - 2 * risk_pts
    risk_lot = risk_pts * lot
    st.write(f"Direction: **{'LONG (buy)' if long_side else 'SHORT (sell)'}**")
    st.dataframe(pd.DataFrame({
        "Level": ["Entry", "Stop-loss", "Target (1:2)", "1 lot value", "Loss per lot at stop"],
        "Value": [f"₹{entry:.2f}", f"₹{stop:.2f}", f"₹{target:.2f}",
                  f"₹{entry * lot:,.0f}", f"₹{risk_lot:,.0f}"]}), hide_index=True)
    allowed = int((capital * risk_pct / 100) // risk_lot) if risk_lot > 0 else 0
    if allowed >= 1:
        st.write(f"Within your risk limit you can trade up to **{allowed} lot(s)**. "
                 "Futures need margin (a part of the lot value). Check the exact amount in the Angel One app.")
    else:
        st.warning(f"One lot risks about ₹{risk_lot:,.0f}, more than your limit of "
                   f"₹{capital * risk_pct / 100:,.0f}. This is too big for your capital. "
                   "Skip it, or look at options (loss is limited to the premium).")
    paper_fut_ticket(sym, near["Expiry"], str(f.iloc[0]["token"]), long_side, lot,
                     entry, stop, target)


def show_options(sym):
    fno = load_fno()
    o = fno[(fno["name"] == sym) & (fno["instrumenttype"] == "OPTSTK")]
    if o.empty:
        st.warning("No options found (this stock may not have F&O).")
        return
    expiries = sorted(o["expiry"].unique(),
                      key=lambda e: pd.to_datetime(e, format="%d%b%Y"))
    c1, c2 = st.columns(2)
    expiry = c1.selectbox("Expiry", expiries)
    n = c2.slider("Strikes each side", 3, 10, 6)
    o = o[o["expiry"] == expiry]
    res = get_analysis(sym)
    spot = float(res[0]["Close"].iloc[-1]) if res else None
    strikes = sorted(o["strike"].unique())
    i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot)) if spot else len(strikes) // 2
    window = strikes[max(i - n, 0): i + n + 1]
    atm = strikes[i]
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
    lot = int(o["lotsize"].iloc[0])
    days_left = (pd.to_datetime(expiry, format="%d%b%Y") - pd.Timestamp.today().normalize()).days

    st.subheader(f"{sym} Options")
    atm_ce = df.loc[df["Strike"] == atm, "CE LTP"].iloc[0]
    atm_pe = df.loc[df["Strike"] == atm, "PE LTP"].iloc[0]
    if spot:
        st.write(f"Spot price: **₹{spot:.2f}** | Nearest (ATM) strike: **{atm:g}**")
    if isnum(atm_ce) and isnum(atm_pe):
        st.write(f"ATM premium: **CE ₹{atm_ce:.2f}** | **PE ₹{atm_pe:.2f}** | "
                 f"1 lot costs about **₹{atm_ce * lot:,.0f}** (CE) / **₹{atm_pe * lot:,.0f}** (PE)")
        if spot:
            st.caption(f"CE + PE together = ₹{atm_ce + atm_pe:.2f}. The market is roughly "
                       f"expecting a move of about ±{(atm_ce + atm_pe) / spot * 100:.1f}% by expiry.")
    pcr = pe.sum() / max(ce.sum(), 1)
    support = df.loc[pe.idxmax(), "Strike"]
    resistance = df.loc[ce.idxmax(), "Strike"]
    st.write(f"Lot size: **{lot}** | Days to expiry: **{days_left}**")
    st.write(f"PCR (near spot): **{pcr:.2f}** "
             f"({'bullish lean' if pcr > 1.2 else 'bearish lean' if pcr < 0.7 else 'neutral'})")
    st.write(f"Resistance (max call OI): **{resistance}** | Support (max put OI): **{support}**")
    st.dataframe(df, hide_index=True, column_config={
        "CE LTP": st.column_config.NumberColumn("CE premium ₹"),
        "PE LTP": st.column_config.NumberColumn("PE premium ₹")})

    if not res:
        return
    v = res[2]
    underlying_box(v)
    st.subheader("Options verdict")
    bias = v["bias"]
    if bias == "bullish" and pcr >= 1.0:
        st.success("Stock trend is bullish and option data agrees. **CE (call) side** can suit a buyer.")
        default_side = 0
    elif bias == "bearish" and pcr <= 0.8:
        st.success("Stock trend is bearish and option data agrees. **PE (put) side** can suit a buyer.")
        default_side = 1
    elif bias == "neutral":
        st.warning("No clear direction. Avoid buying options now, because premium decays every day.")
        default_side = 0
    else:
        st.warning("Stock trend and option data do not agree. Better to wait for a clearer signal.")
        default_side = 0 if bias == "bullish" else 1
    if days_left <= 3:
        st.warning("Expiry is very close. Time decay is severe, so options can lose value fast.")

    st.subheader("Trade planner (buying)")
    side = st.radio("Side", ["CE", "PE"], index=default_side, horizontal=True,
                    format_func=lambda s: "CE (call: bet on rise)" if s == "CE" else "PE (put: bet on fall)")
    strike = st.selectbox("Strike", window, index=window.index(atm))
    prem = df.loc[df["Strike"] == strike, f"{side} LTP"].iloc[0]
    if not isnum(prem) or prem <= 0:
        st.info("No price available for this strike (no trades yet).")
        return
    cost = prem * lot
    be = strike + prem if side == "CE" else strike - prem
    move = abs(be - spot) / spot * 100 if spot else None
    stop_prem = prem * 0.65
    target_prem = prem * 1.6
    allowed = int((capital * risk_pct / 100) // (cost * 0.35))
    st.dataframe(pd.DataFrame({
        "Item": ["Premium", "Cost of 1 lot (max loss if held to zero)", "Break-even at expiry",
                 "Move needed to break even", "Rule of thumb exit (premium -35%)",
                 "Rule of thumb target (premium +60%)"],
        "Value": [f"₹{prem:.2f}", f"₹{cost:,.0f}", f"₹{be:.2f}",
                  f"{move:.1f}%" if move is not None else "N/A",
                  f"₹{stop_prem:.2f}", f"₹{target_prem:.2f}"]}), hide_index=True)
    if allowed >= 1:
        st.write(f"If you exit at -35%, you can buy up to **{allowed} lot(s)** within your risk limit.")
    else:
        st.warning("One lot costs more than your risk limit allows even with an exit at -35%. Skip it.")
    if move is not None and move > 5:
        st.warning("The stock needs a big move to break even. Think twice.")
    m = o[(o["strike"] == strike) & (o["symbol"].str.endswith(side))]
    if not m.empty:
        paper_opt_ticket(sym, f"{sym} {strike:g} {side} {expiry}", str(m["token"].iloc[0]),
                         lot, prem, stop_prem, target_prem)
    st.caption("Option buying can lose 100% of the premium. Decide your exit before entering.")


# ---------- Market scanner (movers and ideas) ----------
SECTORS = {
    "Banks": "HDFCBANK ICICIBANK SBIN KOTAKBANK AXISBANK INDUSINDBK BANKBARODA PNB CANBK FEDERALBNK IDFCFIRSTB AUBANK",
    "IT": "TCS INFY HCLTECH WIPRO TECHM PERSISTENT COFORGE MPHASIS",
    "Pharma": "SUNPHARMA DRREDDY CIPLA DIVISLAB LUPIN AUROPHARMA TORNTPHARM ZYDUSLIFE ALKEM",
    "Auto": "MARUTI M&M BAJAJ-AUTO EICHERMOT HEROMOTOCO TVSMOTOR ASHOKLEY BOSCHLTD",
    "FMCG": "HINDUNILVR ITC NESTLEIND BRITANNIA DABUR GODREJCP MARICO COLPAL TATACONSUM",
    "Oil, Gas and Power": "RELIANCE ONGC IOC BPCL GAIL NTPC POWERGRID TATAPOWER COALINDIA",
    "Metals": "TATASTEEL JSWSTEEL HINDALCO VEDL JINDALSTEL SAIL NMDC",
    "Finance": "BAJFINANCE BAJAJFINSV SHRIRAMFIN CHOLAFIN MUTHOOTFIN PFC RECLTD SBILIFE HDFCLIFE",
    "Infra and Capital goods": "LT ADANIPORTS SIEMENS ABB HAL BEL DLF",
}
UNIVERSES = ["Nifty 50", "All F&O stocks"]


def valid_syms(names):
    if len(labels) > 500:
        return tuple(s for s in names if s in labels)
    return tuple(names)


def universe_symbols(choice):
    if choice == "Nifty 50":
        return valid_syms(NIFTY50)
    return valid_syms(sorted(load_fno()["name"].unique()))


@st.cache_data(ttl=600, show_spinner="Loading prices (first time takes a little while)...")
def scan_universe(symbols):
    tickers = [s + ".NS" for s in symbols]
    raw = yf.download(tickers, period="1y", interval="1d", group_by="ticker",
                      threads=True, progress=False)
    mkt = market_up()
    rows = []
    for s in symbols:
        try:
            d = raw[s + ".NS"].dropna(subset=["Close"])
            c = d["Close"].dropna()
            if len(c) < 60:
                continue
            chg = (c.iloc[-1] / c.iloc[-2] - 1) * 100
            v = verdict_engine(add_indicators(d), {}, mkt)
            rows.append({"Symbol": s,
                         "Company": labels.get(s, s).split(" - ", 1)[-1][:28],
                         "Price": round(float(c.iloc[-1]), 2),
                         "Chg %": round(float(chg), 2),
                         "Score": int(v["score"]), "Verdict": v["verdict"],
                         "R:R": round(float(v["rr"]), 1)})
        except Exception:
            continue
    return pd.DataFrame(rows)


def open_in_analysis(sym):
    if sym not in labels:
        return
    st.session_state["symbol_sel"] = sym
    st.session_state["mode_sel"] = "Stock"
    st.session_state["run"] = True


def pick_and_open(df, key):
    sym = st.selectbox("Open one in the Analyse tab", df["Symbol"].tolist(), index=None,
                       key=key, placeholder="Pick a stock",
                       format_func=lambda s: labels.get(s, s))
    if sym:
        st.button(f"Analyse {sym}", key=key + "_btn", on_click=open_in_analysis, args=(sym,))
        st.caption("After tapping, switch to the Analyse tab.")


# ---------- Market cap filter ----------
CAPS = ["All sizes", "Large cap", "Mid cap", "Small cap"]


@st.cache_data(ttl=86400, show_spinner="Loading market caps (first time only, can take a minute)...")
def get_mcaps(symbols):
    from concurrent.futures import ThreadPoolExecutor

    def one(sym):
        try:
            fi = yf.Ticker(sym + ".NS").fast_info
            try:
                mc = fi.market_cap
            except Exception:
                mc = fi["marketCap"]
            return float(mc) if mc and mc > 0 else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        vals = list(ex.map(one, symbols))
    return dict(zip(symbols, vals))


def cap_class(sym, mc):
    if isnum(mc) and mc > 0:
        cr = mc / 1e7
        return "Large cap" if cr >= 100000 else "Mid cap" if cr >= 30000 else "Small cap"
    return "Large cap" if sym in NIFTY50 else None


def apply_cap(tbl, choice):
    if choice == CAPS[0] or tbl.empty:
        return tbl
    mcs = get_mcaps(tuple(tbl["Symbol"]))
    t = tbl.copy()
    t["Cap"] = [cap_class(x, mcs.get(x)) for x in t["Symbol"]]
    if t["Cap"].notna().mean() < 0.3:
        st.info("Market-cap data is not available right now, so all sizes are shown.")
        return tbl
    st.caption("Approximate sizes: Large = above ₹1 lakh crore, Mid = ₹30,000 crore to "
               "₹1 lakh crore, Small = below ₹30,000 crore.")
    return t[t["Cap"] == choice]


# ---------- Paper trading ----------
PAPER_FILE = "paper_trading.json"
FUT_MARGIN = 0.20  # rough margin blocked for futures; the real margin varies


def _now():
    return pd.Timestamp.now(tz="Asia/Kolkata").strftime("%d %b %H:%M")


def _gist_cfg():
    try:
        return st.secrets["GITHUB_TOKEN"], st.secrets["GIST_ID"]
    except Exception:
        return None


def _gist_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def book_load():
    if st.session_state.get("book_loaded"):
        return st.session_state.get("book")
    book = None
    cfg = _gist_cfg()
    if cfg:
        try:
            r = requests.get(f"https://api.github.com/gists/{cfg[1]}",
                             headers=_gist_headers(cfg[0]), timeout=10)
            r.raise_for_status()
            f = r.json().get("files", {}).get(PAPER_FILE)
            if f:
                data = json.loads(f.get("content") or "{}")
                if isinstance(data, dict) and data.get("start"):
                    book = data
        except Exception as e:
            st.session_state["book_err"] = f"Could not load your saved account: {e}"
            return None
    st.session_state.pop("book_err", None)
    st.session_state["book"] = book
    st.session_state["book_loaded"] = True
    return book


def book_save(book):
    st.session_state["book"] = book
    st.session_state["book_loaded"] = True
    cfg = _gist_cfg()
    if not cfg:
        return
    try:
        r = requests.patch(f"https://api.github.com/gists/{cfg[1]}",
                           headers=_gist_headers(cfg[0]), timeout=10,
                           json={"files": {PAPER_FILE: {"content": json.dumps(book or {})}}})
        r.raise_for_status()
        st.session_state.pop("book_save_err", None)
    except Exception as e:
        st.session_state["book_save_err"] = f"Could not save permanently: {e}"


def _pnl(p, price):
    return (price - p["entry"]) * p["qty"] * (1 if p["side"] == "LONG" else -1)


def paper_open(kind, symbol, label, side, qty, entry, stop, target, token=None):
    book = book_load()
    if book is None:
        return False, "Start your paper account first in the 📒 Paper tab."
    qty = int(qty)
    if qty < 1 or not isnum(entry) or entry <= 0:
        return False, "Invalid quantity or price."
    blocked = qty * entry * (FUT_MARGIN if kind == "future" else 1)
    if blocked > book["cash"]:
        return False, (f"Not enough virtual cash. Needed ₹{blocked:,.0f}, "
                       f"available ₹{book['cash']:,.0f}.")
    book["cash"] -= blocked
    book["open"].append({"id": book["next_id"], "kind": kind, "symbol": symbol,
                         "label": label, "side": side, "qty": qty,
                         "entry": float(entry), "stop": float(stop),
                         "target": float(target), "token": token,
                         "blocked": float(blocked), "opened": _now()})
    book["next_id"] += 1
    book_save(book)
    return True, f"Paper trade opened: {side} {qty} x {label} at ₹{entry:.2f}. See the 📒 Paper tab."


def paper_close(pid, price, reason):
    book = book_load()
    pos = next((p for p in book["open"] if p["id"] == pid), None)
    if pos is None:
        return
    pnl = _pnl(pos, price)
    book["cash"] += pos["blocked"] + pnl
    book["open"].remove(pos)
    book["closed"].append({**pos, "exit": float(price), "pnl": round(float(pnl), 2),
                           "reason": reason, "closed": _now()})
    book_save(book)


def live_prices(book):
    prices = {}
    for x in {p["symbol"] for p in book["open"] if p["kind"] == "stock"}:
        h = get_hist(x + ".NS", "5d", "1d")
        if not h.empty:
            prices[("stock", x)] = float(h["Close"].iloc[-1])
    toks = tuple(sorted({p["token"] for p in book["open"]
                         if p["kind"] != "stock" and p.get("token")}))
    if toks:
        try:
            q = get_quotes(toks)
            for t in toks:
                ltp = q.get(t, {}).get("ltp")
                if isnum(ltp) and ltp > 0:
                    prices[("fo", t)] = float(ltp)
        except Exception:
            st.session_state["fo_price_err"] = True
    return prices


def price_of(p, prices):
    key = ("stock", p["symbol"]) if p["kind"] == "stock" else ("fo", p["token"])
    return prices.get(key)


def auto_exits(book, prices):
    msgs = []
    for p in list(book["open"]):
        px = price_of(p, prices)
        if px is None:
            continue
        lng = p["side"] == "LONG"
        if (lng and px <= p["stop"]) or (not lng and px >= p["stop"]):
            reason = "Stop-loss hit"
        elif (lng and px >= p["target"]) or (not lng and px <= p["target"]):
            reason = "Target hit"
        else:
            continue
        paper_close(p["id"], px, reason)
        msgs.append(f"{p['label']}: {reason} at ₹{px:.2f}")
    return msgs


def _ticket_result(ok, msg):
    (st.success if ok else st.error)(msg)


def paper_stock_ticket(sym, v, qty):
    with st.expander("📒 Paper trade this"):
        if v["verdict"] != "BUY":
            st.caption("The verdict is not BUY. Practising against it is fine, "
                       "just notice how it turns out.")
        n = st.number_input("Shares (virtual)", min_value=1, value=max(int(qty), 1),
                            step=1, key=f"pt_q_{sym}")
        st.caption(f"Uses about ₹{n * v['entry']:,.0f} of virtual cash. "
                   f"Stop ₹{v['stop']:.2f}, target ₹{v['target']:.2f}.")
        if st.button("Buy in paper account", key=f"pt_b_{sym}"):
            _ticket_result(*paper_open("stock", sym, f"{sym} shares", "LONG", n,
                                       v["entry"], v["stop"], v["target"]))


def paper_fut_ticket(sym, expiry, token, long_side, lot, entry, stop, target):
    with st.expander("📒 Paper trade this"):
        side = "LONG" if long_side else "SHORT"
        lots = st.number_input("Lots (virtual)", min_value=1, value=1, step=1,
                               key=f"pf_q_{sym}_{expiry}")
        st.caption(f"{side} {lots * lot} units. Blocks about ₹{lots * lot * entry * FUT_MARGIN:,.0f} "
                   f"as margin (rough {FUT_MARGIN * 100:.0f}% assumption). "
                   f"Stop ₹{stop:.2f}, target ₹{target:.2f}.")
        if st.button(f"{side.title()} in paper account", key=f"pf_b_{sym}_{expiry}"):
            _ticket_result(*paper_open("future", sym, f"{sym} FUT {expiry}", side,
                                       lots * lot, entry, stop, target, token))


def paper_opt_ticket(sym, label, token, lot, prem, stop_prem, target_prem):
    with st.expander("📒 Paper trade this"):
        lots = st.number_input("Lots (virtual)", min_value=1, value=1, step=1,
                               key=f"po_q_{label}")
        st.caption(f"Buying {lots * lot} units. Costs ₹{lots * lot * prem:,.0f} "
                   f"(this is also your maximum loss). Exit at ₹{stop_prem:.2f}, "
                   f"target ₹{target_prem:.2f}.")
        if st.button("Buy in paper account", key=f"po_b_{label}"):
            _ticket_result(*paper_open("option", sym, label, "LONG", lots * lot,
                                       prem, stop_prem, target_prem, token))


def render_paper(book):
    prices = live_prices(book)
    for m in auto_exits(book, prices):
        st.warning("Auto-closed: " + m)
    if st.session_state.get("book_save_err"):
        st.warning(st.session_state["book_save_err"])
    if st.session_state.get("fo_price_err"):
        st.caption("Could not get live F&O prices right now.")

    rows, held = [], 0.0
    for p in book["open"]:
        px = price_of(p, prices)
        pnl = _pnl(p, px) if px is not None else None
        held += p["blocked"] + (pnl or 0)
        rows.append({"ID": p["id"], "Trade": p["label"], "Side": p["side"], "Qty": p["qty"],
                     "Entry": round(p["entry"], 2),
                     "Now": round(px, 2) if px is not None else None,
                     "P&L ₹": round(pnl) if pnl is not None else None,
                     "Stop": round(p["stop"], 2), "Target": round(p["target"], 2),
                     "Opened": p["opened"]})
    equity = book["cash"] + held
    total = equity - book["start"]
    closed = book["closed"]
    wins = sum(1 for c in closed if c["pnl"] > 0)

    st.subheader("Your paper account")
    st.write(f"Total value: **₹{equity:,.0f}**  ({total:+,.0f}, {total / book['start'] * 100:+.1f}%)")
    st.write(f"Virtual cash: **₹{book['cash']:,.0f}** | In open trades: **₹{held:,.0f}** | "
             f"Started with: ₹{book['start']:,.0f}")
    st.write(f"Closed trades: **{len(closed)}**"
             + (f" | Won: **{wins}** ({wins / len(closed) * 100:.0f}%)" if closed else "")
             + f" | Realised P&L: **₹{sum(c['pnl'] for c in closed):+,.0f}**")

    st.subheader("Open trades")
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        names = {r["ID"]: r["Trade"] for r in rows}
        cid = st.selectbox("Close a trade", list(names), index=None,
                           placeholder="Choose a trade", format_func=lambda i: names[i])
        if cid is not None and st.button("Close at current price"):
            pos = next(p for p in book["open"] if p["id"] == cid)
            px = price_of(pos, prices)
            if px is None:
                st.error("Current price is not available right now. Try again in a minute.")
            else:
                paper_close(cid, px, "Closed by you")
                st.rerun()
        st.caption("Stop-loss and target are checked whenever you open the app, at the price "
                   "then (it can be worse than your stop if the stock gaps). Brokerage and "
                   "taxes are not included.")
    else:
        st.info("No open trades. Open a stock, futures or options idea in the Analyse tab "
                "and tap '📒 Paper trade this'.")

    if closed:
        with st.expander("Trade history"):
            hist = pd.DataFrame(closed[::-1])[["label", "side", "qty", "entry", "exit",
                                               "pnl", "reason", "closed"]]
            hist.columns = ["Trade", "Side", "Qty", "Entry", "Exit", "P&L ₹", "Why closed", "Closed"]
            st.dataframe(hist, hide_index=True)

    with st.expander("Backup, restore and reset"):
        if _gist_cfg():
            st.success("Saved permanently (private Gist).")
        else:
            st.warning("Not saved permanently. If you refresh or close the page, the account "
                       "is lost. Download a backup below, or ask to set up permanent saving.")
        st.download_button("Download backup", json.dumps(book), "paper_backup.json",
                           "application/json")
        restore_ui()
        if st.checkbox("I understand: delete my paper account and start again"):
            if st.button("Reset paper account"):
                book_save(None)
                st.rerun()


def restore_ui():
    up = st.file_uploader("Restore from a backup file", type=["json"])
    if up is not None and st.button("Restore this backup"):
        try:
            data = json.loads(up.getvalue().decode("utf-8"))
            assert data.get("start") and "open" in data and "closed" in data
            book_save(data)
            st.rerun()
        except AssertionError:
            st.error("This file does not look like a valid backup.")
        except Exception:
            st.error("Could not read this file.")


# ---------- Run ----------
with tab_a:
    if go:
        st.session_state["run"] = True

    if st.session_state.get("run"):
        if not symbol:
            st.warning("Select a stock first.")
        elif mode == "Stock":
            show_stock(symbol, company)
        else:
            try:
                if mode == "Futures":
                    show_futures(symbol)
                else:
                    show_options(symbol)
            except Exception as e:
                st.error("Could not fetch Angel One data.")
                st.caption(f"Details: {e}")
        st.caption("Educational tool only, not financial advice. Rules are simple and "
                   "not backtested. Paper-trade first.")

with tab_m:
    st.caption("Biggest movers of the day. Prices come from Yahoo and can be slightly delayed.")
    uni_m = st.radio("Stocks to scan", UNIVERSES, horizontal=True, key="uni_m")
    cap_m = st.radio("Company size", CAPS, horizontal=True, key="cap_m")
    if st.button("Show top gainers and losers"):
        st.session_state["movers"] = True
    if st.session_state.get("movers"):
        try:
            full = scan_universe(universe_symbols(uni_m))
            if full.empty:
                st.warning("Could not load prices right now. Try again in a minute.")
            else:
                tbl = apply_cap(full, cap_m)
                if tbl.empty:
                    st.info("No stocks of this size in this list.")
                else:
                    tbl = tbl.sort_values("Chg %", ascending=False)
                    st.subheader("🟢 Top gainers")
                    st.dataframe(tbl.head(10), hide_index=True)
                    st.subheader("🔴 Top losers")
                    st.dataframe(tbl.tail(10).iloc[::-1], hide_index=True)
                    st.caption("A stock that jumped a lot today is not automatically a buy. "
                               "Open it in Analyse and check the verdict and risk-reward first.")
                    pick_and_open(pd.concat([tbl.head(10), tbl.tail(10)]), "pick_m")
        except Exception as e:
            st.error("Could not load market data.")
            st.caption(f"Details: {e}")

with tab_i:
    st.subheader("Best setups today")
    st.caption("Quick scan of many stocks using technicals and risk-reward only. "
               "Fundamentals are checked when you open a stock in Analyse.")
    uni_i = st.radio("Stocks to scan", UNIVERSES, horizontal=True, key="uni_i")
    cap_i = st.radio("Company size", CAPS, horizontal=True, key="cap_i")
    if st.button("Scan for setups"):
        st.session_state["scan"] = True
    if st.session_state.get("scan"):
        try:
            full = scan_universe(universe_symbols(uni_i))
            if full.empty:
                st.warning("Could not load prices right now. Try again in a minute.")
            else:
                tbl = apply_cap(full, cap_i)
                buys = tbl[tbl["Verdict"] == "BUY"].sort_values("Score", ascending=False)
                waits = tbl[tbl["Verdict"] == "WAIT"].sort_values("Score", ascending=False)
                if buys.empty:
                    st.info("No stock passes all BUY rules today. Waiting is also a good decision.")
                else:
                    st.success(f"{len(buys)} stock(s) pass the BUY rules")
                    st.dataframe(buys.head(15), hide_index=True)
                st.write("Close to a setup (WAIT list):")
                st.dataframe(waits.head(10), hide_index=True)
                pick_and_open(pd.concat([buys.head(15), waits.head(10)]), "pick_i")
        except Exception as e:
            st.error("Could not scan the market.")
            st.caption(f"Details: {e}")

    st.subheader("Browse by sector")
    sector = st.selectbox("Sector", list(SECTORS), index=None, placeholder="Choose a sector",
                          key="sector_sel")
    if sector:
        try:
            tbl = scan_universe(valid_syms(SECTORS[sector].split()))
            if tbl.empty:
                st.warning("Could not load prices right now.")
            else:
                st.dataframe(tbl.sort_values("Score", ascending=False), hide_index=True)
                pick_and_open(tbl, "pick_s")
        except Exception as e:
            st.error("Could not load this sector.")
            st.caption(f"Details: {e}")

with tab_p:
    cfg = _gist_cfg()
    book = book_load()
    if st.session_state.get("book_err") and cfg:
        st.error(st.session_state["book_err"])
        st.caption("Your saved account is untouched. Reload the page to try again.")
    elif book is None:
        st.subheader("Start paper trading")
        st.write("Practise with virtual money. No real orders are ever placed.")
        start = st.number_input("Virtual money to start with (₹)", min_value=10000,
                                max_value=10000000, value=100000, step=10000, key="paper_start")
        if st.button("Start paper account"):
            book_save({"start": float(start), "cash": float(start), "open": [],
                       "closed": [], "next_id": 1, "created": _now()})
            st.rerun()
        with st.expander("Restore a backup instead"):
            restore_ui()
        if not cfg:
            st.caption("Note: without permanent saving, the account is lost when you refresh the page.")
    else:
        render_paper(book)
