from contextlib import asynccontextmanager
from typing import Union
from datetime import date, datetime
from zoneinfo import ZoneInfo
import asyncio
import json
import logging
import os
import threading

from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from agents.application.trade import Trader

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_trader: Trader | None = None
_scheduler: BackgroundScheduler | None = None
_is_paused = False
_state_lock = threading.Lock()
_bot_app: Application | None = None
_event_loop: asyncio.AbstractEventLoop | None = None

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _send_alert(message: str) -> None:
    """Send a message to the group chat from a sync context."""
    if _bot_app is None or _event_loop is None or not TELEGRAM_CHAT_ID:
        logger.warning("_send_alert: bot not ready, dropping message: %s", message[:80])
        return
    future = asyncio.run_coroutine_threadsafe(
        _bot_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message),
        _event_loop,
    )
    try:
        future.result(timeout=10)
    except Exception as e:
        logger.error("_send_alert failed: %s", e)


def _trade_worker():
    """Runs the full trading pipeline in its own thread so the event loop stays free."""
    try:
        _send_alert("Trade cycle starting...")
        result = _trader.one_best_trade()
        if result:
            _send_alert(
                f"Trade executed:\n"
                f"{result['trade']}\n\n"
                f"Trade size: ${result['amount_usd']:.2f}\n"
                f"Transaction: {result['tx']}"
            )
        else:
            _send_alert("Trade cycle complete — no trade found.")
    except Exception as e:
        logger.error(f"Trading loop error: {e}")
        _send_alert(f"ERROR in trading loop: {e}")


def _run_trade():
    with _state_lock:
        paused = _is_paused
    if paused:
        logger.info("Trading loop paused, skipping scheduled run")
        return
    threading.Thread(target=_trade_worker, daemon=True, name="trade-worker").start()


_LONDON = ZoneInfo("Europe/London")


def _fmt_time(dt) -> str:
    """Return a datetime as 'HH:MM:SS UTC / HH:MM:SS BST' (or GMT in winter)."""
    if dt is None:
        return "N/A"
    utc_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    london_dt = dt.astimezone(_LONDON)
    tz_abbr = london_dt.strftime("%Z")  # BST or GMT
    london_str = london_dt.strftime("%Y-%m-%d %H:%M:%S ") + tz_abbr
    return f"{utc_str} / {london_str}"


def _fmt(data) -> str:
    """Format API data for Telegram (truncated to 4000 chars)."""
    text = json.dumps(data, indent=2, default=str) if not isinstance(data, str) else data
    return text[:4000] + ("…" if len(text) > 4000 else "")


# ── Bot command handlers ──────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job = _scheduler.get_job("trading_loop") if _scheduler else None
    with _state_lock:
        paused = _is_paused
    next_run = _fmt_time(job.next_run_time if job else None)
    now_utc = datetime.now(ZoneInfo("UTC"))
    now_str = _fmt_time(now_utc)
    await update.message.reply_text(
        f"Running: {_scheduler is not None and _scheduler.running}\n"
        f"Paused: {paused}\n"
        f"Now:      {now_str}\n"
        f"Next run: {next_run}"
    )


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _is_paused
    with _state_lock:
        _is_paused = True
    await update.message.reply_text("Trading paused.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _is_paused
    with _state_lock:
        _is_paused = False
    await update.message.reply_text("Trading resumed.")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _trader is None:
        await update.message.reply_text("Trader not initialised.")
        return
    try:
        orders = await asyncio.to_thread(_trader.polymarket.client.get_orders)
        await update.message.reply_text(f"Open positions:\n{_fmt(orders)}")
    except Exception as e:
        await update.message.reply_text(f"Error fetching positions: {e}")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _trader is None:
        await update.message.reply_text("Trader not initialised.")
        return
    try:
        trades = await asyncio.to_thread(_trader.polymarket.client.get_trades)
        await update.message.reply_text(f"P&L ({date.today()}):\n{_fmt(trades)}")
    except Exception as e:
        await update.message.reply_text(f"Error fetching P&L: {e}")


async def cmd_trade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with _state_lock:
        paused = _is_paused
    if paused:
        await update.message.reply_text("Trading is paused. Use /resume first.")
        return
    await update.message.reply_text("Triggering trade now...")
    asyncio.create_task(asyncio.to_thread(_run_trade))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/status    — running state & next trade time\n"
        "/trade     — trigger a trade immediately\n"
        "/pause     — pause trading\n"
        "/resume    — resume trading\n"
        "/positions — open positions\n"
        "/pnl       — today's P&L\n"
        "/help      — this message"
    )


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _trader, _scheduler, _bot_app, _event_loop

    _event_loop = asyncio.get_running_loop()

    if TELEGRAM_BOT_TOKEN:
        _bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        for cmd, fn in [
            ("status", cmd_status),
            ("trade", cmd_trade),
            ("pause", cmd_pause),
            ("resume", cmd_resume),
            ("positions", cmd_positions),
            ("pnl", cmd_pnl),
            ("help", cmd_help),
        ]:
            _bot_app.add_handler(CommandHandler(cmd, fn))
        await _bot_app.initialize()
        await _bot_app.start()
        await _bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started in polling mode")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled")

    _trader = Trader()
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run_trade, "interval", minutes=60, id="trading_loop")
    _scheduler.start()
    logger.info("Trading scheduler started — running every 60 minutes")
    threading.Thread(target=_trade_worker, daemon=True, name="trade-worker-startup").start()

    yield

    _scheduler.shutdown()
    logger.info("Trading scheduler stopped")
    if _bot_app:
        await _bot_app.updater.stop()
        await _bot_app.stop()
        await _bot_app.shutdown()
        logger.info("Telegram bot stopped")


app = FastAPI(lifespan=lifespan)


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: Union[str, None] = None):
    return {"item_id": item_id, "q": q}


@app.get("/trades/{trade_id}")
def read_trade(trade_id: int, q: Union[str, None] = None):
    return {"trade_id": trade_id, "q": q}


@app.get("/markets/{market_id}")
def read_market(market_id: int, q: Union[str, None] = None):
    return {"market_id": market_id, "q": q}


@app.get("/status")
def get_status():
    job = _scheduler.get_job("trading_loop") if _scheduler else None
    next_run_dt = job.next_run_time if job else None
    return {
        "running": _scheduler is not None and _scheduler.running,
        "paused": _is_paused,
        "next_run_utc": next_run_dt.strftime("%Y-%m-%d %H:%M:%S UTC") if next_run_dt else None,
        "next_run_london": _fmt_time(next_run_dt),
    }


@app.get("/positions")
def get_positions():
    try:
        orders = _trader.polymarket.client.get_orders()
        return {"positions": orders}
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return {"positions": [], "error": str(e)}


@app.get("/pnl")
def get_pnl():
    try:
        trades = _trader.polymarket.client.get_trades()
        return {"date": str(date.today()), "trades": trades}
    except Exception as e:
        logger.error(f"Failed to fetch P&L: {e}")
        return {"date": str(date.today()), "trades": [], "error": str(e)}


@app.post("/pause")
def pause_trading():
    global _is_paused
    with _state_lock:
        _is_paused = True
    return {"status": "paused"}


@app.post("/resume")
def resume_trading():
    global _is_paused
    with _state_lock:
        _is_paused = False
    return {"status": "resumed"}


