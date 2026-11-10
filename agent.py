"""
Daily Market & Hotel Intelligence Brief

Design rule: every NUMBER in the email is computed here in Python from a named
source. Gemini never sees a blank it can fill with a guess: it only summarizes
headlines it is handed, and it must cite them by ID. Links are inserted by
this script, never written by the model.

Sources
  Equities ........ Yahoo Finance via yfinance (^GSPC, QQQ)
  Treasury yields . U.S. Treasury daily par yield curve (official close),
                    FRED fallback; live intraday 5y/10y from Yahoo (^FVX, ^TNX)
  Macro news ...... Federal Reserve press RSS, BLS release RSS, Google News
  Hotel news ...... Google News searches (Hilton, Marriott, Hyatt/IHG/etc.,
                    lodging REITs, transactions, RevPAR) + trade press RSS
"""

import csv
import html
import io
import json
import os
import re
import smtplib
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf
from google import genai
from google.genai import types

CT = ZoneInfo("America/Chicago")
NOW_CT = datetime.now(CT)
UA = {"User-Agent": "Mozilla/5.0 (daily-brief; personal use)"}
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")


# ---------------------------------------------------------------------------
# 1. Equities
# ---------------------------------------------------------------------------
def fetch_equities():
    rows = []
    for name, symbol in [("S&P 500", "^GSPC"), ("QQQ (Nasdaq-100 ETF)", "QQQ")]:
        try:
            hist = yf.Ticker(symbol).history(period="7d", interval="1d")
            hist = hist.dropna(subset=["Close"])
            last, prev = hist["Close"].iloc[-1], hist["Close"].iloc[-2]
            last_date = hist.index[-1].date()
            live = last_date == NOW_CT.date()
            chg = last - prev
            rows.append({
                "name": name,
                "value": f"{last:,.2f}",
                "change": f"{chg:+,.2f} ({chg / prev * 100:+.2f}%)",
                "as_of": f"{'Live' if live else 'Close'} {last_date:%b %d}",
                "dir": chg,
            })
        except Exception as e:
            rows.append({"name": name, "value": "n/a", "change": "",
                         "as_of": f"error: {e.__class__.__name__}", "dir": 0})
    return rows


# ---------------------------------------------------------------------------
# 2. Treasury yields
#    Official close = U.S. Treasury par yield curve (same series Bloomberg/WSJ
#    quote as the "constant maturity" close). Published ~3:30pm ET, so at
#    9:15 CT it is the PRIOR business day.
# ---------------------------------------------------------------------------
TENORS = [("2-Yr", "2 Yr", "DGS2"), ("5-Yr", "5 Yr", "DGS5"), ("10-Yr", "10 Yr", "DGS10")]


def _treasury_rows(year):
    url = ("https://home.treasury.gov/resource-center/data-chart-center/"
           f"interest-rates/daily-treasury-rates.csv/{year}/all"
           f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&_format=csv")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def _official_from_treasury():
    rows = _treasury_rows(NOW_CT.year)
    if len(rows) < 2:  # early January
        rows += _treasury_rows(NOW_CT.year - 1)
    rows.sort(key=lambda r: datetime.strptime(r["Date"], "%m/%d/%Y"), reverse=True)
    latest, prior = rows[0], rows[1]
    d = datetime.strptime(latest["Date"], "%m/%d/%Y").date()
    return {label: (float(latest[col]), float(prior[col]), d) for label, col, _ in TENORS}


def _official_from_fred():
    out = {}
    for label, _, series in TENORS:
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}",
                         headers=UA, timeout=30)
        r.raise_for_status()
        pts = [row for row in csv.reader(io.StringIO(r.text))][1:]
        pts = [(row[0], float(row[1])) for row in pts if row[1] not in (".", "")]
        (_, prev), (d, last) = pts[-2], pts[-1]
        out[label] = (last, prev, datetime.strptime(d, "%Y-%m-%d").date())
    return out


def _live_yield(symbol):
    """Yahoo ^FVX/^TNX. Yahoo has quoted these both as % (4.25) and as
    % x 10 (42.5) over the years; normalise so either works."""
    try:
        hist = yf.Ticker(symbol).history(period="5d", interval="1d").dropna(subset=["Close"])
        if hist.index[-1].date() != NOW_CT.date():
            return None
        v = float(hist["Close"].iloc[-1])
        return v / 10 if v > 20 else v
    except Exception:
        return None


def fetch_yields():
    source = "U.S. Treasury"
    try:
        official = _official_from_treasury()
    except Exception:
        official, source = _official_from_fred(), "FRED (Treasury constant maturity)"

    live = {"5-Yr": _live_yield("^FVX"), "10-Yr": _live_yield("^TNX")}
    rows = []
    for label, _, _ in TENORS:
        last, prev, d = official[label]
        bps = round((last - prev) * 100)
        lv = live.get(label)
        live_txt = ""
        if lv is not None:
            live_txt = f"{lv:.2f}% ({round((lv - last) * 100):+d} bps vs close)"
        rows.append({
            "name": f"{label} Treasury",
            "value": f"{last:.2f}%",
            "change": f"{bps:+d} bps",
            "as_of": f"Close {d:%b %d}",
            "live": live_txt,
            "dir": bps,
        })
    curve = official["10-Yr"][0] - official["2-Yr"][0]
    return rows, f"{curve * 100:+.0f} bps", source


# ---------------------------------------------------------------------------
# 3. News
# ---------------------------------------------------------------------------
def gnews(q):
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:2d")
            + "&hl=en-US&gl=US&ceid=US:en")


MACRO_FEEDS = [
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss"),
    ("News", gnews('CPI OR "consumer price index" OR inflation report')),
    ("News", gnews('"Federal Reserve" OR FOMC interest rates')),
    ("News", gnews('jobs report OR nonfarm payrolls OR jobless claims')),
    ("News", gnews('Treasury yields bond market')),
]

HOTEL_FEEDS = [
    ("News", gnews('"Hilton" earnings OR guidance OR RevPAR')),
    ("News", gnews('"Marriott" earnings OR guidance OR RevPAR')),
    ("News", gnews('Hyatt OR IHG OR Wyndham OR "Choice Hotels" earnings OR RevPAR')),
    ("News", gnews('"Host Hotels" OR "Park Hotels" OR Pebblebrook OR "Apple Hospitality" '
                   'OR "Ryman Hospitality" OR "Sunstone Hotel" OR DiamondRock OR "Xenia Hotels" '
                   'OR "RLJ Lodging" OR "Summit Hotel"')),
    ("News", gnews('"hotel REIT" OR "lodging REIT"')),
    ("News", gnews('hotel acquisition OR "hotel sale" OR "hotel portfolio" OR "hotel loan"')),
    ("News", gnews('hotel Texas OR Florida OR California sale OR development OR RevPAR')),
    ("News", gnews('CoStar STR hotel performance RevPAR occupancy')),
    ("Hotel Dive", "https://www.hoteldive.com/feeds/news/"),
    ("Hotel Management", "https://www.hotelmanagement.net/rss.xml"),
    ("Skift", "https://skift.com/feed/"),
]


def _published(entry):
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def fetch_news(feeds, max_items=25):
    # Monday's brief looks back over the weekend
    lookback = timedelta(hours=72 if NOW_CT.weekday() == 0 else 36)
    cutoff = datetime.now(timezone.utc) - lookback
    seen, items = set(), []
    for label, url in feeds:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            feed = feedparser.parse(r.content)
        except Exception:
            continue
        for e in feed.entries[:15]:
            pub = _published(e)
            if pub is None or pub < cutoff:
                continue
            title = html.unescape(e.get("title", "")).strip()
            source = label
            # Google News titles end with " - Publisher"
            if label == "News" and " - " in title:
                title, source = title.rsplit(" - ", 1)
            key = re.sub(r"[^a-z0-9]", "", title.lower())[:70]
            if not title or key in seen:
                continue
            seen.add(key)
            items.append({"title": title, "source": source,
                          "link": e.get("link", ""), "published": pub})
    items.sort(key=lambda x: x["published"], reverse=True)
    return items[:max_items]


# ---------------------------------------------------------------------------
# 4. Gemini: pick and summarise ONLY from the supplied headlines
# ---------------------------------------------------------------------------
def summarize(macro, hotel, yield_rows, curve):
    def listing(prefix, items):
        return "\n".join(f"{prefix}{i}: [{x['source']}, {x['published']:%b %d %H:%M} UTC] {x['title']}"
                         for i, x in enumerate(items))

    yields_txt = "; ".join(f"{r['name']} {r['value']} ({r['change']})" for r in yield_rows)
    prompt = f"""You write the morning brief for a VP of Hospitality Investments & Finance.
Today is {NOW_CT:%A, %B %d, %Y}.

Official Treasury data (already shown in the email, for context only):
{yields_txt}; 2s10s curve {curve}.

MACRO HEADLINES:
{listing("M", macro) or "(none)"}

HOTEL / LODGING / REIT HEADLINES:
{listing("H", hotel) or "(none)"}

Rules:
- Use ONLY the headlines above. Do not add facts, figures, dates or events that are not
  in a headline. If a headline lacks the number, do not supply one.
- Every bullet must cite at least one headline ID (e.g. "M3" or "H0").
- Prioritise: CPI/PCE/jobs data releases, Fed decisions and Fed speakers, rate moves;
  Hilton/Marriott/Hyatt/IHG earnings and guidance, lodging REIT earnings and deals,
  hotel transactions and financing, RevPAR/performance data, Texas/Florida/California.
- Skip fluff, listicles, travel-consumer stories and duplicates.
- Macro: up to 5 bullets. Hotel: up to 7 bullets. Each bullet one sentence, <= 30 words.
- "rates_take": one sentence on what the yield moves mean for hotel cap rates/financing,
  using only the Treasury data above. No invented numbers.
"""
    schema = {
        "type": "object",
        "properties": {
            "macro": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["text", "ids"]}},
            "hotel": {"type": "array", "items": {"type": "object", "properties": {
                "text": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["text", "ids"]}},
            "rates_take": {"type": "string"},
        },
        "required": ["macro", "hotel", "rates_take"],
    }
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        temperature=0.2,
        response_mime_type="application/json",
        response_schema=schema,
    )
    # Gemini returns 503 (overloaded) / 429 (rate limit) / 500 at busy times.
    # Retry each model with backoff, then move on to the next model.
    fallbacks = [m.strip() for m in os.environ.get(
        "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash").split(",") if m.strip()]
    last_err = None
    for model in [GEMINI_MODEL] + fallbacks:
        for attempt, wait in enumerate([0, 15, 45, 90]):
            if wait:
                print(f"  retrying {model} in {wait}s (attempt {attempt + 1})")
                time.sleep(wait)
            try:
                resp = client.models.generate_content(model=model, contents=prompt, config=config)
                print(f"Gemini OK ({model})")
                return json.loads(resp.text)
            except Exception as e:
                last_err = e
                msg = str(e)
                print(f"  {model} attempt {attempt + 1} failed: {msg[:120]}")
                if not any(code in msg for code in ("503", "429", "500", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                    break  # not a temporary error (e.g. bad model name) -> try next model
    raise last_err


# ---------------------------------------------------------------------------
# 5. HTML email (built here, not by the model)
# ---------------------------------------------------------------------------
H2 = "font:600 15px Arial;color:#333;border-bottom:1px solid #ddd;padding-bottom:4px;margin:22px 0 8px"
TD = "padding:6px 10px;border-bottom:1px solid #eee;font:14px Arial"


def _color(v):
    return "#1a7f37" if v > 0 else "#c62828" if v < 0 else "#555"


def _table(rows, live_col=False):
    head = ["", "Level", "Chg", "As of"] + (["Live (intraday)"] if live_col else [])
    out = ["<table style='border-collapse:collapse;width:100%'><tr>"]
    out += [f"<th style='{TD};text-align:left;color:#777;font-weight:600'>{h}</th>" for h in head]
    out.append("</tr>")
    for r in rows:
        out.append(
            f"<tr><td style='{TD}'>{html.escape(r['name'])}</td>"
            f"<td style='{TD}'><b>{r['value']}</b></td>"
            f"<td style='{TD};color:{_color(r['dir'])}'>{r['change']}</td>"
            f"<td style='{TD};color:#777'>{r['as_of']}</td>"
            + (f"<td style='{TD};color:#555'>{r.get('live') or '—'}</td>" if live_col else "")
            + "</tr>")
    out.append("</table>")
    return "".join(out)


def _bullets(points, lookup):
    if not points:
        return "<p style='font:14px Arial;color:#777'>Nothing material in the last day.</p>"
    lis = []
    for p in points:
        refs = []
        for i in p.get("ids", []):
            x = lookup.get(i.strip().upper())
            if x:
                refs.append(f"<a href='{html.escape(x['link'])}' style='color:#555'>"
                            f"{html.escape(x['source'])}</a>")
        if not refs:  # model cited nothing real -> drop the bullet
            continue
        lis.append(f"<li style='margin-bottom:6px'>{html.escape(p['text'])} "
                   f"<span style='font-size:12px'>({', '.join(refs)})</span></li>")
    return f"<ul style='font:14px Arial;color:#222;padding-left:18px'>{''.join(lis)}</ul>"


def build_email(eq, yrows, curve, ysrc, summary, macro, hotel):
    lookup = {f"M{i}": x for i, x in enumerate(macro)} | {f"H{i}": x for i, x in enumerate(hotel)}
    return f"""<div style="max-width:680px;margin:auto;font-family:Arial;color:#222">
<p style="font:12px Arial;color:#777;margin:0">{NOW_CT:%A, %B %d, %Y · %I:%M %p} CT</p>
<h1 style="font:600 20px Arial;margin:4px 0 0">Market &amp; Hotel Brief</h1>

<h2 style="{H2}">Equities</h2>
{_table(eq)}

<h2 style="{H2}">Treasury Yields</h2>
{_table(yrows, live_col=True)}
<p style="font:13px Arial;color:#555;margin:6px 0">2s10s curve: <b>{curve}</b>.
{html.escape(summary.get("rates_take", ""))}</p>
<p style="font:11px Arial;color:#999;margin:2px 0">Close = {ysrc} official par yield
(prior business day; published ~3:30pm ET). Live = Yahoo intraday where available.</p>

<h2 style="{H2}">Macro &amp; Fed</h2>
{_bullets(summary.get("macro", []), lookup)}

<h2 style="{H2}">Hotels, Brands &amp; Lodging REITs</h2>
{_bullets(summary.get("hotel", []), lookup)}
</div>"""


def send_email(subject, html_body):
    sender = os.environ["SENDER_EMAIL"]
    receiver = os.environ["RECEIVER_EMAIL"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, sender, receiver
    plain = re.sub(r"<[^>]+>", "", html_body)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


if __name__ == "__main__":
    equities = fetch_equities()
    yield_rows, curve, ysrc = fetch_yields()
    macro_news = fetch_news(MACRO_FEEDS)
    hotel_news = fetch_news(HOTEL_FEEDS, max_items=35)
    print(f"Fetched {len(macro_news)} macro and {len(hotel_news)} hotel headlines")

    try:
        summary = summarize(macro_news, hotel_news, yield_rows, curve)
    except Exception as e:
        print(f"Gemini failed ({e}); sending numbers + raw headlines")
        summary = {"rates_take": "",
                   "macro": [{"text": x["title"], "ids": [f"M{i}"]} for i, x in enumerate(macro_news[:5])],
                   "hotel": [{"text": x["title"], "ids": [f"H{i}"]} for i, x in enumerate(hotel_news[:7])]}

    body = build_email(equities, yield_rows, curve, ysrc, summary, macro_news, hotel_news)
    if os.environ.get("DRY_RUN"):
        with open("brief_preview.html", "w") as f:
            f.write(body)
        print("DRY_RUN: wrote brief_preview.html")
    else:
        send_email(f"Market & Hotel Brief — {NOW_CT:%a %b %d}", body)
        print("Sent.")
