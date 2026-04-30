from agents.application.executor import Executor as Agent
from agents.application.paper_trading import PaperTrader
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket

import ast
import logging
import os
import re
import shutil

import requests

logger = logging.getLogger(__name__)


def _send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception:
        pass


class Trader:
    def __init__(self):
        self.polymarket = Polymarket()
        self.gamma = Gamma()
        self.agent = Agent()
        self.paper_mode = os.getenv("PAPER_TRADING", "false").lower() == "true"
        self.paper_trader = PaperTrader(polymarket=self.polymarket)
        if self.paper_mode:
            logger.warning("PAPER TRADING MODE ENABLED — no real orders will be placed")

    def pre_trade_logic(self) -> None:
        self.clear_local_dbs()

    def clear_local_dbs(self) -> None:
        try:
            shutil.rmtree("/tmp/local_db_events")
        except:
            pass
        try:
            shutil.rmtree("/tmp/local_db_markets")
        except:
            pass

    def one_best_trade(self) -> dict | None:
        """

        one_best_trade is a strategy that evaluates all events, markets, and orderbooks

        leverages all available information sources accessible to the autonomous agent

        then executes that trade without any human intervention

        """
        self.pre_trade_logic()

        events = self.polymarket.get_all_tradeable_events()
        logger.info("1. FOUND %d EVENTS", len(events))
        if not events:
            msg = "STEP 1 FAILED: No tradeable events returned from Gamma API."
            logger.warning(msg)
            _send_telegram(msg)
            return None

        filtered_events = self.agent.filter_events_with_rag(events)
        logger.info("2. FILTERED TO %d EVENTS (from %d)", len(filtered_events), len(events))
        if not filtered_events:
            msg = f"STEP 2 FAILED: RAG filter returned 0 results from {len(events)} events."
            logger.warning(msg)
            _send_telegram(msg)
            return None

        markets = self.agent.map_filtered_events_to_markets(filtered_events)
        logger.info("3. FOUND %d MARKETS", len(markets))
        if not markets:
            msg = f"STEP 3 FAILED: No markets mapped from {len(filtered_events)} filtered events."
            logger.warning(msg)
            _send_telegram(msg)
            return None

        filtered_markets = self.agent.filter_markets(markets)
        logger.info("4. FILTERED TO %d MARKETS (from %d)", len(filtered_markets), len(markets))
        if not filtered_markets:
            msg = f"STEP 4 FAILED: Market RAG filter returned 0 results from {len(markets)} markets."
            logger.warning(msg)
            _send_telegram(msg)
            return None

        # Pick the first market that has valid outcome prices AND CLOB token IDs
        market = None
        market_meta = None
        for i, candidate in enumerate(filtered_markets):
            meta = candidate[0].dict()["metadata"]
            raw_prices = meta.get("outcome_prices") or "[]"
            raw_clob = meta.get("clob_token_ids") or "[]"
            try:
                prices = ast.literal_eval(raw_prices)
            except Exception:
                prices = []
            try:
                clob_ids = ast.literal_eval(raw_clob)
            except Exception:
                clob_ids = []
            neg_risk = meta.get("neg_risk", False)
            logger.info(
                "4b. Candidate %d: %r → prices=%r, clob_ids=%r, neg_risk=%s",
                i, meta.get("question", "")[:80], prices, clob_ids, neg_risk
            )
            if prices and clob_ids and not neg_risk:
                # Verify CLOB actually has an active orderbook
                token_id = clob_ids[0]
                has_book = self.polymarket.has_active_orderbook(token_id)
                logger.info(
                    "4b. Orderbook check for %r (token %s...): %s",
                    meta.get("question", "")[:60], token_id[:16], has_book
                )
                if not has_book:
                    logger.info("4b. SKIPPING — no active orderbook")
                    continue
                market = candidate
                market_meta = meta
                break

        if market is None:
            questions = [c[0].dict()["metadata"].get("question", "?")[:60] for c in filtered_markets]
            msg = f"STEP 4b FAILED: None of {len(filtered_markets)} markets had valid prices+CLOB IDs.\nCandidates: {questions}"
            logger.warning(msg)
            _send_telegram(msg)
            return None

        if not market_meta.get("active", True) or market_meta.get("closed", False):
            msg = "STEP 4c FAILED: Best market is no longer active/open."
            logger.warning(msg)
            _send_telegram(msg)
            return None

        try:
            best_trade = self.agent.source_best_trade(market)
            best_trade = re.sub(r'(?i)paper trade[^:]*:\s*', '', best_trade).strip()
        except Exception as e:
            msg = f"STEP 5 FAILED: Claude trade analysis raised an exception — {type(e).__name__}: {e}"
            logger.error(msg)
            _send_telegram(msg)
            return None
        logger.info("5. CALCULATED TRADE %s", best_trade)

        try:
            amount, outcome = self.agent.format_trade_prompt_for_execution(best_trade)
        except Exception as e:
            msg = f"STEP 5b FAILED: Could not parse trade parameters from Claude response — {type(e).__name__}: {e}"
            logger.error(msg)
            _send_telegram(msg)
            return None
        logger.info("5b. TRADE SIZE $%.2f (capped at 10%% of wallet), OUTCOME: %s", amount, outcome)

        if amount < 1.0:
            msg = f"STEP 5b FAILED: Trade size ${amount:.2f} below $1 minimum. Check USDC balance."
            logger.warning(msg)
            _send_telegram(msg)
            return None

        if self.paper_mode:
            record = self.paper_trader.record_paper_trade(market, amount, outcome, best_trade)
            logger.info("6. PAPER TRADE recorded: %s", record["id"])
            msg = (
                f"PAPER TRADE (not real):\n"
                f"{best_trade[:300]}\n\n"
                f"Outcome: {record['outcome']}\n"
                f"Trade size: ${amount:.2f}\n"
                f"Predicted price: {record['predicted_price']}\n"
                f"Live price at signal: {record['current_market_price']}"
            )
            _send_telegram(msg)
            return {"trade": best_trade, "amount_usd": amount, "tx": "PAPER", "paper": record}

        try:
            trade = self.polymarket.execute_market_order(market, amount, outcome)
        except Exception as e:
            logger.error("Full exception details: %s", repr(e))
            logger.error("Exception type: %s", type(e).__name__)
            err_str = str(e)
            if "order_version_mismatch" in err_str:
                error_msg = (
                    "Trade execution failed: order_version_mismatch\n"
                    "Your wallet is not onboarded with Polymarket.\n"
                    "Fix: visit polymarket.com, connect your wallet, and complete signup.\n"
                    "Then set POLY_SIGNATURE_TYPE=1 and POLY_FUNDER=<proxy wallet address> in Railway."
                )
            else:
                error_msg = f"Trade execution failed: {e}"
            logger.error(error_msg)
            _send_telegram(f"Full exception: {repr(e)}")
            _send_telegram(error_msg)
            return None

        logger.info("6. TRADED %s", trade)
        return {"trade": best_trade, "amount_usd": amount, "tx": trade}

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
