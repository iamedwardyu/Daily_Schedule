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
        "2-Yr Treasury Yield": "^TWO",
        "5-Yr Treasury Yield": "^FVX",
        "10-Yr Treasury Yield": "^TNX"
    }
    
    metrics_summary = []
    for name, symbol in tickers.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if len(hist) >= 1:
                latest_price = hist['Close'].iloc[-1]
                
                # Treasury Yield Handling (e.g. 45.2 -> 4.52%)
                if symbol in ["^TWO", "^FVX", "^TNX"]:
                    yield_val = latest_price / 10.0 if latest_price > 15 else latest_price
                    val_str = f"{yield_val:.2f}%"
                else:
                    val_str = f"{latest_price:,.2f}"
                
                # Calculate daily change
                if len(hist) >= 2:
                    prev_close = hist['Close'].iloc[-2]
                    change = latest_price - prev_close
                    pct_change = (change / prev_close) * 100
                    
                    if symbol in ["^TWO", "^FVX", "^TNX"]:
                        bps_change = (change / 10.0) * 100 if latest_price > 15 else change * 100
                        diff_str = f"{bps_change:+.1f} bps ({pct_change:+.2f}%)"
                    else:
                        diff_str = f"{change:+.2f} ({pct_change:+.2f}%)"
                    metrics_summary.append(f"{name}: {val_str} [{diff_str}]")
                else:
                    metrics_summary.append(f"{name}: {val_str}")
        except Exception as e:
            metrics_summary.append(f"{name}: Data temporarily unavailable")
            
    return "\n".join(metrics_summary)

# 2. Fetch Macro & Hotel / Lodging Industry RSS Feeds
def fetch_all_news():
    news_sources = {
        "Macro & Fed Economy": [
            "https://www.cnbc.com/id/10000664/device/rss/rss.html",
            "https://finance.yahoo.com/news/rssindex"
        ],
        "Hotel Industry & Lodging REITs": [
            "https://www.hotelnewsresource.com/HNR_RSS.xml",
            "https://skift.com/feed/",
            "https://lodgingmagazine.com/feed/",
            "https://www.hotelmanagement.net/rss.xml"
        ]
    }
    
    collected_headlines = []
    for category, urls in news_sources.items():
        collected_headlines.append(f"\n--- {category.upper()} ---")
        for url in urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:4]:
                    collected_headlines.append(f"- {entry.title} ({entry.link})")
            except Exception:
                continue
                
    return "\n".join(collected_headlines)

# 3. Synthesize Executive Brief via Gemini AI
def generate_ai_digest(market_data, news_text):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    
    prompt = f"""
    You are an executive chief of staff for a Vice President of Hospitality Investments and Finance. 
    Synthesize the provided 9:15 AM market metrics, economic data, and lodging industry updates into a crisp, high-impact HTML morning email brief.
    
    LIVE MARKET & TREASURY DATA:
    {market_data}
    
    LATEST NEWS FEEDS:
    {news_text}
    
    EMAIL STRUCTURAL & CONTENT REQUIREMENTS:
    1. Structure: Professional HTML email template with dark gray section headers (#1A2530), clean data tables, and bullet points.
    2. Section 1: Financial & Yield Overview: Clean summary table of S&P 500, QQQ, 2-Yr, 5-Yr, and 10-Yr Treasury Yields with bps changes.
    3. Section 2: Macro & Monetary Drivers: Highlight macroeconomic developments (Fed interest rate policies/hikes/cuts, CPI inflation reports, jobs/payrolls data, rate environment impact on debt/refinancing).
    4. Section 3: Hotel & Lodging Industry Highlights: Synthesize key lodging news, including earnings releases/guidance from major hotel brands (Hilton, Marriott, Hyatt, Wyndham) and lodging REITs (Host Hotels, Park Hotels, Pebblebrook, Apple Hospitality), notable M&A/transactions, or STR/CoStar trend commentary.
    5. Tone: Quantitative, direct, executive-ready, and actionable. Avoid extra conversational filler.
    """
    
    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )
    return response.text

# 4. Send Email via Gmail SMTP
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
    news_str = fetch_all_news()
    html_digest = generate_ai_digest(market_str, news_str)
    send_email(html_digest)
