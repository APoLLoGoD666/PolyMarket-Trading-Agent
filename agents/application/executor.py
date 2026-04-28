import os
import json
import ast
import re
from typing import List, Dict, Any, Optional

import math
import anthropic

from dotenv import load_dotenv

from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.connectors.chroma import PolymarketRAG as Chroma
from agents.utils.objects import SimpleEvent, SimpleMarket
from agents.application.prompts import Prompter
from agents.polymarket.polymarket import Polymarket

def retain_keys(data, keys_to_retain):
    if isinstance(data, dict):
        return {
            key: retain_keys(value, keys_to_retain)
            for key, value in data.items()
            if key in keys_to_retain
        }
    elif isinstance(data, list):
        return [retain_keys(item, keys_to_retain) for item in data]
    else:
        return data

class Executor:
    def __init__(self, default_model='claude-sonnet-4-6') -> None:
        load_dotenv()
        max_token_model = {'claude-sonnet-4-6': 180000}
        self.model = default_model
        self.token_limit = max_token_model.get(default_model, 180000)
        self.prompter = Prompter()
        self.client = anthropic.Anthropic()
        self.gamma = Gamma()
        self.chroma = Chroma()
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

    def get_superforecast(
        self, event_title: str, market_question: str, outcome: str
    ) -> str:
        prompt = self.prompter.superforecaster(
            description=event_title, question=market_question, outcome=outcome
        )
        return self._invoke(prompt)


    def estimate_tokens(self, text: str) -> int:
        # This is a rough estimate. For more accurate results, consider using a tokenizer.
        return len(text) // 4  # Assuming average of 4 characters per token

    def process_data_chunk(self, data1: List[Dict[Any, Any]], data2: List[Dict[Any, Any]], user_input: str) -> str:
        system = str(self.prompter.prompts_polymarket(data1=data1, data2=data2))
        return self._invoke(user_input, system=system)


    def divide_list(self, original_list, i):
        # Calculate the size of each sublist
        sublist_size = math.ceil(len(original_list) / i)

        # Use list comprehension to create sublists
        return [original_list[j:j+sublist_size] for j in range(0, len(original_list), sublist_size)]

    def get_polymarket_llm(self, user_input: str) -> str:
        data1 = self.gamma.get_current_events()
        data2 = self.gamma.get_current_markets()

        combined_data = str(self.prompter.prompts_polymarket(data1=data1, data2=data2))

        # Estimate total tokens
        total_tokens = self.estimate_tokens(combined_data)

        # Set a token limit (adjust as needed, leaving room for system and user messages)
        token_limit = self.token_limit
        if total_tokens <= token_limit:
            # If within limit, process normally
            return self.process_data_chunk(data1, data2, user_input)
        else:
            # If exceeding limit, process in chunks
            chunk_size = len(combined_data) // ((total_tokens // token_limit) + 1)
            print(f'total tokens {total_tokens} exceeding llm capacity, now will split and answer')
            group_size = (total_tokens // token_limit) + 1 # 3 is safe factor
            keys_no_meaning = ['image','pagerDutyNotificationEnabled','resolvedBy','endDate','clobTokenIds','negRiskMarketID','conditionId','updatedAt','startDate']
            useful_keys = ['id','questionID','description','liquidity','clobTokenIds','outcomes','outcomePrices','volume','startDate','endDate','question','questionID','events']
            data1 = retain_keys(data1, useful_keys)
            cut_1 = self.divide_list(data1, group_size)
            cut_2 = self.divide_list(data2, group_size)
            cut_data_12 = zip(cut_1, cut_2)

            results = []

            for cut_data in cut_data_12:
                sub_data1 = cut_data[0]
                sub_data2 = cut_data[1]
                sub_tokens = self.estimate_tokens(str(self.prompter.prompts_polymarket(data1=sub_data1, data2=sub_data2)))

                result = self.process_data_chunk(sub_data1, sub_data2, user_input)
                results.append(result)

            combined_result = " ".join(results)



            return combined_result
    def filter_events(self, events: "list[SimpleEvent]") -> str:
        prompt = self.prompter.filter_events(events)
        return self._invoke(prompt)

    def filter_events_with_rag(self, events: "list[SimpleEvent]") -> str:
        prompt = self.prompter.filter_events()
        print()
        print("... prompting ... ", prompt)
        print()
        return self.chroma.events(events, prompt)

    def map_filtered_events_to_markets(
        self, filtered_events: "list[SimpleEvent]"
    ) -> "list[SimpleMarket]":
        import logging
        log = logging.getLogger(__name__)
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
                    formatted_market_data = self.polymarket.map_api_to_market(market_data)
                    markets.append(formatted_market_data)
                except Exception as ex:
                    log.warning("Skipping market_id=%s — error: %s", market_id, ex)
        log.info("map_filtered_events_to_markets: collected %d markets from %d events", len(markets), len(filtered_events))
        return markets

    def filter_markets(self, markets) -> "list[tuple]":
        prompt = self.prompter.filter_markets()
        print()
        print("... prompting ... ", prompt)
        print()
        return self.chroma.markets(markets, prompt)

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
        print()
        print("... prompting ... ", prompt)
        print()
        content = self._invoke(prompt)

        print("result: ", content)
        print()
        prompt = self.prompter.one_best_trade(content, outcomes, outcome_prices)
        print("... prompting ... ", prompt)
        print()
        content = self._invoke(prompt)

        print("result: ", content)
        print()
        return content

    def format_trade_prompt_for_execution(self, best_trade: str) -> tuple:
        import logging
        log = logging.getLogger(__name__)

        size_match = re.search(r'size\s*[:=]\s*(\d+\.?\d*)', best_trade, re.IGNORECASE)
        if not size_match:
            raise ValueError(f"Could not parse size from trade response: {best_trade[:200]}")
        size = float(size_match.group(1))
        # Claude should output a decimal fraction (0.05 = 5%). Guard against Claude
        # returning a whole percentage like 5 instead of 0.05.
        if size > 1.0:
            size = size / 100.0

        outcome_match = re.search(r'outcome\s*[:=]\s*[\'"]?(\w+)[\'"]?', best_trade, re.IGNORECASE)
        outcome = outcome_match.group(1) if outcome_match else None
        log.info("Parsed trade — size fraction: %.4f, outcome: %s", size, outcome)

        usdc_balance = self.polymarket.get_usdc_balance()
        amount = size * usdc_balance
        max_amount = 0.10 * usdc_balance
        if amount > max_amount:
            log.info("Trade size $%.2f exceeds 10%% cap ($%.2f), clamping.", amount, max_amount)
            amount = max_amount
        return amount, outcome

    def source_best_market_to_create(self, filtered_markets) -> str:
        prompt = self.prompter.create_new_market(filtered_markets)
        print()
        print("... prompting ... ", prompt)
        print()
        return self._invoke(prompt)
