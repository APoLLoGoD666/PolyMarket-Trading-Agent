import os
import json
import ast
import re
import logging
from typing import List, Dict, Any, Optional

import math
import anthropic

from dotenv import load_dotenv

from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.utils.objects import SimpleEvent, SimpleMarket
from agents.application.prompts import Prompter
from agents.polymarket.polymarket import Polymarket

log = logging.getLogger(__name__)


class __Document:
    """Minimal stand-in for langchain _Document — avoids the entire langchain dependency."""
    def __init__(self, page_content: str, metadata: dict):
        self.page_content = page_content
        self.metadata = metadata

    def dict(self):
        return {"page_content": self.page_content, "metadata": self.metadata}

    def json(self):
        return json.dumps(self.dict(), default=str)


class Executor:
    def __init__(self, default_model='claude-sonnet-4-6') -> None:
        load_dotenv()
        self.model = default_model
        self.prompter = Prompter()
        self.client = anthropic.Anthropic()
        self.gamma = Gamma()
        self.polymarket = Polymarket()

    def _invoke(self, prompt: str, system: Optional[str] = None) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        response = self.client.messages.create(**kwargs)
        return response.content[0].text

    def get_llm_response(self, user_input: str) -> str:
        system = str(self.prompter.market_analyst())
        return self._invoke(user_input, system=system)

    def get_superforecast(self, event_title: str, market_question: str, outcome: str) -> str:
        prompt = self.prompter.superforecaster(
            description=event_title, question=market_question, outcome=outcome
        )
        return self._invoke(prompt)

    def filter_events_with_rag(self, events: "list[SimpleEvent]") -> list:
        """Ask Claude to pick the most tradeable events — no ChromaDB needed."""
        if not events:
            return []

        sample = events[:40]
        lines = "\n".join(
            f"{i}: {e.title} — {(e.description or '')[:120]}"
            for i, e in enumerate(sample)
        )
        prompt = (
            f"You are a Polymarket prediction market analyst.\n\n"
            f"Here are {len(sample)} active prediction market events:\n{lines}\n\n"
            f"Select the 4 most interesting and tradeable events for right now.\n"
            f"Reply with ONLY a comma-separated list of numbers, e.g.: 2,7,15,33"
        )
        try:
            raw = self._invoke(prompt).strip()
            indices = [int(x.strip()) for x in re.split(r'[,\s]+', raw) if x.strip().isdigit()]
            indices = [i for i in indices if i < len(sample)][:4]
            log.info("Claude selected event indices: %s", indices)
        except Exception as e:
            log.warning("Claude event filter failed (%s) — using first 4 events", e)
            indices = list(range(min(4, len(sample))))

        result = []
        for i in indices:
            e = sample[i]
            doc = _Document(
                page_content=e.description or e.title or "",
                metadata={"id": str(e.id), "markets": e.markets},
            )
            result.append((doc, 1.0))
        return result

    def map_filtered_events_to_markets(self, filtered_events: list) -> list:
        markets = []
        for e in filtered_events:
            try:
                data = json.loads(e[0].json())
                market_ids = data["metadata"]["markets"].split(",")
            except Exception as ex:
                log.warning("Skipping event — failed to parse metadata: %s", ex)
                continue
            for market_id in market_ids:
                market_id = market_id.strip()
                if not market_id:
                    continue
                try:
                    market_data = self.gamma.get_market(market_id)
                    if not isinstance(market_data, dict) or "id" not in market_data:
                        log.warning("Skipping market_id=%s — unexpected response: %r", market_id, str(market_data)[:120])
                        continue
                    formatted = self.polymarket.map_api_to_market(market_data)
                    markets.append(formatted)
                except Exception as ex:
                    log.warning("Skipping market_id=%s — error: %s", market_id, ex)
        log.info("map_filtered_events_to_markets: collected %d markets", len(markets))
        return markets

    def filter_markets(self, markets: list) -> list:
        """Ask Claude to pick the most tradeable markets — no ChromaDB needed."""
        if not markets:
            return []

        sample = markets[:30]
        lines = "\n".join(
            f"{i}: {m.get('question', 'N/A')[:100]} | prices: {m.get('outcome_prices', 'N/A')}"
            for i, m in enumerate(sample)
        )
        prompt = (
            f"You are a Polymarket prediction market analyst.\n\n"
            f"Here are {len(sample)} active markets:\n{lines}\n\n"
            f"Select the 4 markets most suitable for profitable trading right now.\n"
            f"Reply with ONLY a comma-separated list of numbers, e.g.: 0,3,7,12"
        )
        try:
            raw = self._invoke(prompt).strip()
            indices = [int(x.strip()) for x in re.split(r'[,\s]+', raw) if x.strip().isdigit()]
            indices = [i for i in indices if i < len(sample)][:4]
            log.info("Claude selected market indices: %s", indices)
        except Exception as e:
            log.warning("Claude market filter failed (%s) — using first 4 markets", e)
            indices = list(range(min(4, len(sample))))

        result = []
        for i in indices:
            m = sample[i]
            doc = _Document(
                page_content=m.get("description") or m.get("question") or "",
                metadata={
                    "id": str(m.get("id", "")),
                    "question": m.get("question") or "",
                    "outcomes": m.get("outcomes") or "[]",
                    "outcome_prices": m.get("outcome_prices") or "[]",
                    "clob_token_ids": m.get("clob_token_ids") or "[]",
                    "active": bool(m.get("active", True)),
                    "closed": bool(m.get("closed", False)),
                },
            )
            result.append((doc, 1.0))
        return result

    @staticmethod
    def _clamp_price(price: float) -> float:
        return max(0.01, min(0.99, price))

    def source_best_trade(self, market_object) -> str:
        market_document = market_object[0].dict()
        market = market_document["metadata"]
        outcome_prices = [
            str(self._clamp_price(float(p)))
            for p in ast.literal_eval(market["outcome_prices"])
        ]
        outcomes = ast.literal_eval(market["outcomes"])
        question = market["question"]
        description = market_document["page_content"]

        prompt = self.prompter.superforecaster(question, description, outcomes)
        log.info("Superforecaster prompt sent")
        content = self._invoke(prompt)
        log.info("Superforecaster result: %s", content[:200])

        prompt = self.prompter.one_best_trade(content, outcomes, outcome_prices)
        log.info("one_best_trade prompt sent")
        content = self._invoke(prompt)
        log.info("one_best_trade result: %s", content[:200])
        return content

    def format_trade_prompt_for_execution(self, best_trade: str) -> tuple:
        size_match = re.search(r'size\s*[:=]\s*(\d+\.?\d*)', best_trade, re.IGNORECASE)
        if not size_match:
            raise ValueError(f"Could not parse size from trade response: {best_trade[:200]}")
        size = float(size_match.group(1))
        if size > 1.0:
            size = size / 100.0

        outcome_match = re.search(r'outcome\s*[:=]\s*[\'"]?(\w+)[\'"]?', best_trade, re.IGNORECASE)
        outcome = outcome_match.group(1) if outcome_match else None
        log.info("Parsed trade — size fraction: %.4f, outcome: %s", size, outcome)

        usdc_balance = self.polymarket.get_usdc_balance()
        log.info("USDC balance: $%.2f", usdc_balance)
        amount = size * usdc_balance
        max_amount = 0.10 * usdc_balance
        if amount > max_amount:
            log.info("Trade size $%.2f exceeds 10%% cap ($%.2f), clamping.", amount, max_amount)
            amount = max_amount
        if amount < 1.0 and usdc_balance >= 2.0:
            log.info("Trade size $%.2f below $1 minimum — bumping to $1.00", amount)
            amount = 1.0
        return amount, outcome

    def source_best_market_to_create(self, filtered_markets) -> str:
        prompt = self.prompter.create_new_market(filtered_markets)
        return self._invoke(prompt)
