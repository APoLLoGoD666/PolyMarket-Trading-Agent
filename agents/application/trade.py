from agents.application.executor import Executor as Agent
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket

import ast
import os
import shutil

import requests


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
            shutil.rmtree("local_db_events")
        except:
            pass
        try:
            shutil.rmtree("local_db_markets")
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
        print(f"1. FOUND {len(events)} EVENTS")
        if not events:
            print("No tradeable events available.")
            return None

        filtered_events = self.agent.filter_events_with_rag(events)
        print(f"2. FILTERED {len(filtered_events)} EVENTS")
        if not filtered_events:
            print("No events passed the relevance filter.")
            return None

        markets = self.agent.map_filtered_events_to_markets(filtered_events)
        print()
        print(f"3. FOUND {len(markets)} MARKETS")
        if not markets:
            print("No markets found for filtered events.")
            return None

        print()
        filtered_markets = self.agent.filter_markets(markets)
        print(f"4. FILTERED {len(filtered_markets)} MARKETS")
        if not filtered_markets:
            print("No markets passed the relevance filter.")
            return None

        market = filtered_markets[0]
        market_meta = market[0].dict()["metadata"]

        outcome_prices = ast.literal_eval(market_meta.get("outcome_prices", "[]"))
        if not outcome_prices:
            print("Market has no valid orderbook (empty outcome_prices), skipping.")
            return None

        if not market_meta.get("active", True) or market_meta.get("closed", False):
            print("Market is no longer active, skipping.")
            return None

        best_trade = self.agent.source_best_trade(market)
        print(f"5. CALCULATED TRADE {best_trade}")

        amount = self.agent.format_trade_prompt_for_execution(best_trade)
        print(f"5b. TRADE SIZE ${amount:.2f} (capped at 10% of wallet)")

        if amount < 1.0:
            print("Trade size too small, skipping")
            return None

        try:
            trade = self.polymarket.execute_market_order(market, amount)
        except Exception as e:
            error_msg = f"Trade execution failed: {e}"
            print(error_msg)
            _send_telegram(error_msg)
            return None

        print(f"6. TRADED {trade}")
        return {"trade": best_trade, "amount_usd": amount, "tx": trade}

    def maintain_positions(self):
        pass

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    t = Trader()
    t.one_best_trade()
