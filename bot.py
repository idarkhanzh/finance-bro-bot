import os
import io
import asyncio
import math
import datetime as dt
import pandas as pd
import requests
import yfinance as yf
from diskcache import Cache

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, filters
)

# ------------------ Configuration -------------------
TOKEN = "7985642444:AAFhbjzjuIxX4W9fHUOGa6Z4E2mxOPq3yWg"  # <-- Your real bot token
FMP_KEY = "YOUR_FMP_KEY_HERE"                           # <-- FinancialModelingPrep API key

# Cache to store industry averages for 12 hours
cache = Cache(directory=os.path.expanduser("~/.financebro_cache"))

# ------------------ Helper Functions -------------------
def fmt(n, digits=2):
    """Format a number or show '—' if None/NaN."""
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "—"
    return f"{n:,.{digits}f}"

def get_company_ratios(ticker: str) -> dict:
    """
    Fetch ratios and price for the given ticker using yfinance.
    This function is synchronous; we'll call it in a worker thread.
    """
    t = yf.Ticker(ticker)
    info = t.info
    bs = t.balance_sheet

    ev = info.get("enterpriseValue")
    ebitda = info.get("ebitda")
    total_rev = info.get("totalRevenue")
    pe = info.get("trailingPE")
    pb = info.get("priceToBook")

    assets, debt = None, None
    if not bs.empty:
        if "Total Assets" in bs.index:
            assets = bs.loc["Total Assets"][0]
        if "Total Debt" in bs.index:
            debt = bs.loc["Total Debt"][0]

    history = t.history(period="1d")
    last_price = history["Close"].iloc[-1] if not history.empty else None
    target = info.get("targetMeanPrice")

    return {
        "price": last_price,
        "target": target,
        "pe": pe,
        "ev_ebitda": ev / ebitda if ev and ebitda else None,
        "assets_debt": assets / debt if assets and debt else None,
        "pb": pb,
        "ev_rev": ev / total_rev if ev and total_rev else None,
        "industry": info.get("industry")
    }

def fmp(endpoint: str, params: dict = None):
    """
    Call the FinancialModelingPrep API.
    """
    base = "https://financialmodelingprep.com/api/v3"
    params = params or {}
    params["apikey"] = FMP_KEY
    r = requests.get(f"{base}/{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def get_industry_average(industry: str) -> dict:
    """
    Retrieve and cache industry averages for key ratios from FMP.
    """
    key = f"ind_avg::{industry}"
    if key in cache:
        return cache[key]

    # Up to 50 peer tickers in the same industry
    peers = fmp("stock-screener", {"industry": industry, "limit": 50})
    symbols = [p["symbol"] for p in peers]

    # Get bulk ratios
    ratios = fmp("ratios-ttm-bulk", {"symbol": ",".join(symbols)})
    df = pd.DataFrame(ratios)

    mapping = {
        "peRatioTTM": "pe",
        "evToEbitdaTTM": "ev_ebitda",
        "debtToAssetsTTM": "assets_debt_inv",  # invert later to get assets/debt
        "priceToBookRatioTTM": "pb",
        "evToSalesTTM": "ev_rev"
    }
    agg = {}
    for fmp_key, our_key in mapping.items():
        if fmp_key not in df:
            continue
        s = pd.to_numeric(df[fmp_key], errors="coerce")
        avg = s.mean(skipna=True)
        if our_key == "assets_debt_inv" and avg:
            # debtToAssets -> invert -> assets/debt
            avg = 1 / avg
            our_key = "assets_debt"
        agg[our_key] = avg

    cache.set(key, agg, expire=60 * 60 * 12)  # 12-hour cache
    return agg

def build_msg_plain(ticker: str, comp: dict, ind: dict) -> str:
    """
    Build a plain-text message comparing company ratios with industry averages.
    No HTML or Markdown, so Telegram won't parse anything.
    """
    lines = []
    lines.append(f"Ticker: {ticker}")
    lines.append(f"Industry: {comp['industry'] if comp['industry'] else 'N/A'}")
    lines.append("")

    # Table header
    lines.append("{:<14} | {:>10} | {:>10}".format("Metric", "Company", "Industry"))

    # Each row + up/down hint
    def arrow(cval, ival):
        if cval and ival:
            if cval > ival:
                return "(UP)"
            elif cval < ival:
                return "(DOWN)"
        return ""

    rows = [
        ("P/E", comp["pe"], ind.get("pe")),
        ("EV/EBITDA", comp["ev_ebitda"], ind.get("ev_ebitda")),
        ("Assets/Debt", comp["assets_debt"], ind.get("assets_debt")),
        ("Price/Book", comp["pb"], ind.get("pb")),
        ("EV/Revenue", comp["ev_rev"], ind.get("ev_rev")),
    ]
    for metric, comp_val, ind_val in rows:
        lines.append("{:<14} | {:>10} | {:>10} {}".format(
            metric, fmt(comp_val), fmt(ind_val), arrow(comp_val, ind_val)
        ))

    lines.append("")
    lines.append(f"Last price:  {fmt(comp['price'])} USD")
    lines.append(f"1-yr target: {fmt(comp['target'])} USD")

    return "\n".join(lines)

# ------------------ Telegram Handlers -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! I am financebro.\n"
        "Send me a stock ticker (e.g. AAPL) or use /compare TICKER to see ratios vs. industry.\n"
        "No fancy formatting, so we won't get parse errors!"
    )

async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /compare TICKER")
        return
    ticker = context.args[0].upper()
    await handle_ticker(update, ticker)

async def echo_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ticker = update.message.text.strip().upper()
    # Simple check to skip if not purely alphabetic
    if not ticker.isalpha():
        return
    await handle_ticker(update, ticker)

async def handle_ticker(update: Update, ticker: str):
    try:
        comp = await asyncio.to_thread(get_company_ratios, ticker)
        if not comp["industry"]:
            await update.message.reply_text("Ticker not found or insufficient data.")
            return

        ind = await asyncio.to_thread(get_industry_average, comp["industry"])
        msg = build_msg_plain(ticker, comp, ind)

        # Send as plain text, no parse mode
        await update.message.reply_text(msg, disable_web_page_preview=True, parse_mode=None)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# ------------------ Main Function -------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("compare", compare))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_ticker))

    print("financebro is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
