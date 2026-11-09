import os
import yfinance as yf
import feedparser
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google import genai

# 1. Fetch Live Financial Data via yfinance
def fetch_market_metrics():
    tickers = {
        "S&P 500": "^GSPC",
        "Nasdaq-100 (QQQ)": "QQQ",
        "5-Yr Treasury Yield": "^FVX",  # 5-Year Treasury Note Yield
        "10-Yr Treasury Yield": "^TNX"  # 10-Year Treasury Note Yield
    }
    
    metrics_summary = []
    for name, symbol in tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d")
            if len(hist) >= 1:
                latest_price = hist['Close'].iloc[-1]
                if symbol in ["^FVX", "^TNX"]:
                    val = f"{latest_price / 10:.2f}%"
                else:
                    val = f"{latest_price:,.2f}"
                
                if len(hist) >= 2:
                    prev_close = hist['Close'].iloc[-2]
                    change = latest_price - prev_close
                    pct_change = (change / prev_close) * 100
                    if symbol in ["^FVX", "^TNX"]:
                        diff_str = f"{change/10:+.2f} bps ({pct_change:+.2f}%)"
                    else:
                        diff_str = f"{change:+.2f} ({pct_change:+.2f}%)"
                    metrics_summary.append(f"{name}: {val} [{diff_str}]")
                else:
                    metrics_summary.append(f"{name}: {val}")
        except Exception as e:
            metrics_summary.append(f"{name}: Data unavailable ({str(e)})")
            
    return "\n".join(metrics_summary)

# 2. Fetch Hospitality & Macro News Feeds (RSS)
def fetch_industry_rss():
    rss_urls = [
        "https://www.hotelmanagement.net/rss.xml",
        "https://skift.com/feed/"
    ]
    headlines = []
    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                headlines.append(f"- {entry.title}: {entry.link}")
        except Exception:
            continue
    return "\n".join(headlines) if headlines else "No RSS headlines fetched."

# 3. Synthesize Data with Gemini AI
def generate_ai_digest(market_data, rss_news):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    
    prompt = f"""
    You are an executive chief of staff for a Vice President of Hospitality Investments and Finance. 
    Synthesize the following 9:15 AM market metrics and industry news into a concise, high-impact HTML daily morning email.
    
    LIVE MARKET DATA:
    {market_data}
    
    HOTEL & MACRO NEWS HEADLINES:
    {rss_news}
    
    EMAIL REQUIREMENTS:
    1. Structure: Clean, professional HTML with simple inline CSS styling (dark gray headings, bold numbers).
    2. Section 1: Financial & Yield Overview (Summary table or bullet list of S&P 500, QQQ, 5Yr, 10Yr yields).
    3. Section 2: Macro & Fed Drivers (Provide concise commentary on current inflation/CPI, jobs reports, rates, or Fed policy drivers based on yield context).
    4. Section 3: Hotel & Commercial Real Estate Highlights (Summarize top industry trends, transaction context, or lodging updates).
    5. Keep the tone concise, quantitative, direct, and actionable. Avoid extra fluff.
    """
    
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )
    return response.text

# 4. Send HTML Email via Gmail SMTP
def send_email(html_body):
    sender_email = os.environ["SENDER_EMAIL"]
    receiver_email = os.environ["RECEIVER_EMAIL"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Daily Morning Market & Hotel Digest — 9:15 AM CT"
    msg["From"] = sender_email
    msg["To"] = receiver_email

    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, app_password)
        server.send_message(msg)

if __name__ == "__main__":
    market_str = fetch_market_metrics()
    rss_str = fetch_industry_rss()
    html_digest = generate_ai_digest(market_str, rss_str)
    send_email(html_digest)
