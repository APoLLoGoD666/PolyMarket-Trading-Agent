from contextlib import asynccontextmanager
from typing import Union
from datetime import date
import logging
import threading

from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler

from agents.application.trade import Trader

logger = logging.getLogger(__name__)

_trader: Trader | None = None
_scheduler: BackgroundScheduler | None = None
_is_paused = False
_state_lock = threading.Lock()


def _run_trade():
    with _state_lock:
        paused = _is_paused
    if paused:
        logger.info("Trading loop paused, skipping scheduled run")
        return
    try:
        _trader.one_best_trade()
    except Exception as e:
        logger.error(f"Trading loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _trader, _scheduler
    _trader = Trader()
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(_run_trade, "interval", minutes=60, id="trading_loop")
    _scheduler.start()
    logger.info("Trading scheduler started — running every 60 minutes")
    yield
    _scheduler.shutdown()
    logger.info("Trading scheduler stopped")


app = FastAPI(lifespan=lifespan)


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
    return {
        "running": _scheduler is not None and _scheduler.running,
        "paused": _is_paused,
        "next_run": str(job.next_run_time) if job else None,
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
