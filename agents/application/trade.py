from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket

import ast
import logging
import os
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
            logger.warning("STEP 1 FAILED: No tradeable events returned from API.")
            return None

        filtered_events = self.agent.filter_events_with_rag(events)
        logger.info("2. FILTERED %d EVENTS", len(filtered_events))
        if not filtered_events:
            logger.warning("STEP 2 FAILED: RAG filter removed all %d events.", len(events))
            return None

        markets = self.agent.map_filtered_events_to_markets(filtered_events)
        logger.info("3. FOUND %d MARKETS", len(markets))
        if not markets:
            logger.warning("STEP 3 FAILED: No markets mapped from %d filtered events.", len(filtered_events))
            return None

        filtered_markets = self.agent.filter_markets(markets)
        logger.info("4. FILTERED %d MARKETS", len(filtered_markets))
        if not filtered_markets:
            logger.warning("STEP 4 FAILED: Market filter removed all %d markets.", len(markets))
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
            logger.info(
                "4b. Candidate %d: %r → prices=%r, clob_ids=%r",
                i, meta.get("question", "")[:80], prices, clob_ids
            )
            if prices and clob_ids:
                market = candidate
                market_meta = meta
                break

        if market is None:
            logger.warning(
                "STEP 4b FAILED: None of the %d filtered markets had valid outcome_prices.",
                len(filtered_markets)
            )
            return None

        if not market_meta.get("active", True) or market_meta.get("closed", False):
            logger.warning("STEP 4c FAILED: Best market is no longer active.")
            return None

        best_trade = self.agent.source_best_trade(market)
        logger.info("5. CALCULATED TRADE %s", best_trade)

        amount, outcome = self.agent.format_trade_prompt_for_execution(best_trade)
        logger.info("5b. TRADE SIZE $%.2f (capped at 10%% of wallet), OUTCOME: %s", amount, outcome)

        if amount < 1.0:
            logger.warning("STEP 5b FAILED: Trade size $%.2f too small, skipping.", amount)
            return None

        try:
            trade = self.polymarket.execute_market_order(market, amount, outcome)
        except Exception as e:
            error_msg = f"Trade execution failed: {e}"
            logger.error(error_msg)
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
