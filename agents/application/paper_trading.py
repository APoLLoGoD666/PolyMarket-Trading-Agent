import ast
import json
import logging
import uuid
from datetime import datetime, timezone

from agents.polymarket.gamma import GammaMarketClient

log = logging.getLogger(__name__)

TRADES_FILE = "/tmp/paper_trades.json"


class PaperTrader:
    def __init__(self, polymarket=None):
        # Accept an existing Polymarket instance so we don't double-initialise web3/CLOB.
        self._polymarket = polymarket
        self._gamma = GammaMarketClient()

    # ── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> list:
        try:
            with open(TRADES_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _save(self, trades: list) -> None:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2, default=str)

    # ── public API ───────────────────────────────────────────────────────────

    def record_paper_trade(
        self,
        market,
        amount: float,
        outcome: str | None,
        best_trade: str,
    ) -> dict:
        """Append a paper trade record and return it."""
        meta = market[0].dict()["metadata"]
        question = meta.get("question", "")
        market_id = str(meta.get("id", ""))

        try:
            outcomes = ast.literal_eval(meta.get("outcomes", "[]"))
        except Exception:
            outcomes = []
        try:
            outcome_prices = ast.literal_eval(meta.get("outcome_prices", "[]"))
        except Exception:
            outcome_prices = []
        try:
            clob_ids = ast.literal_eval(meta.get("clob_token_ids", "[]"))
        except Exception:
            clob_ids = []

        # Resolve the index of the chosen outcome.
        outcome_idx = 0
        if outcome and outcomes:
            try:
                outcome_idx = [o.strip().lower() for o in outcomes].index(
                    outcome.strip().lower()
                )
            except ValueError:
                outcome_idx = 0

        predicted_price = None
        if outcome_prices and outcome_idx < len(outcome_prices):
            try:
                predicted_price = float(outcome_prices[outcome_idx])
            except (ValueError, TypeError):
                pass

        # Try to get the live CLOB mid-price at trade time.
        current_market_price = predicted_price
        if self._polymarket and clob_ids and outcome_idx < len(clob_ids):
            try:
                current_market_price = self._polymarket.get_orderbook_price(
                    clob_ids[outcome_idx]
                )
            except Exception:
                pass

        record = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "question": question,
            "outcome": outcome or (outcomes[outcome_idx] if outcomes else "unknown"),
            "outcome_idx": outcome_idx,
            "amount_usd": amount,
            "predicted_price": predicted_price,
            "current_market_price": current_market_price,
            "clob_token_ids": meta.get("clob_token_ids", "[]"),
            "resolved": False,
            "won": None,
            "analysis": best_trade[:500],
        }

        trades = self._load()
        trades.append(record)
        self._save(trades)
        log.info("Paper trade recorded: %s %s $%.2f @ %.3f", question[:60], outcome, amount, predicted_price or 0)
        return record

    def get_performance_summary(self) -> dict:
        trades = self._load()
        resolved = [t for t in trades if t.get("resolved")]
        pending = [t for t in trades if not t.get("resolved")]
        won = [t for t in resolved if t.get("won")]
        lost = [t for t in resolved if t.get("won") is False]

        total_notional = sum(t.get("amount_usd", 0) for t in trades)

        # Realised P&L for closed trades.
        # Buy at price p, spend $amount → receive amount/p shares worth $1 each if correct.
        # Win:  profit = amount * (1 - p) / p
        # Loss: profit = -amount
        realised_pnl = 0.0
        for t in won:
            p = t.get("predicted_price") or 0.5
            if p > 0:
                realised_pnl += t.get("amount_usd", 0) * (1 - p) / p
        for t in lost:
            realised_pnl -= t.get("amount_usd", 0)

        # Unrealised P&L for pending trades (mark-to-market).
        # Value of position at current price q: amount/p * q  →  unrealised = amount*(q-p)/p
        unrealised_pnl = 0.0
        for t in pending:
            p = t.get("predicted_price")
            q = t.get("current_market_price")
            amt = t.get("amount_usd", 0)
            if p and q and p > 0:
                unrealised_pnl += amt * (q - p) / p

        return {
            "paper_trading": True,
            "total_trades": len(trades),
            "resolved": len(resolved),
            "pending": len(pending),
            "won": len(won),
            "lost": len(lost),
            "total_notional_usd": round(total_notional, 2),
            "realised_pnl_usd": round(realised_pnl, 2),
            "unrealised_pnl_usd": round(unrealised_pnl, 2),
            "total_pnl_usd": round(realised_pnl + unrealised_pnl, 2),
        }

    def get_recent_trades(self, n: int = 10) -> list:
        trades = self._load()
        return trades[-n:]

    def check_and_resolve_trades(self) -> int:
        """
        Fetch current Gamma data for each unresolved trade. If the market has
        closed, mark the trade won/lost based on the final outcomePrices.
        Returns the number of trades newly resolved.
        """
        trades = self._load()
        newly_resolved = 0

        for t in trades:
            if t.get("resolved"):
                continue
            market_id = t.get("market_id")
            if not market_id:
                continue
            try:
                market_data = self._gamma.get_market(market_id)
                if not isinstance(market_data, dict):
                    continue

                active = market_data.get("active", True)
                closed = market_data.get("closed", False)
                if active and not closed:
                    # Update unrealised mark with fresh price.
                    raw_prices = market_data.get("outcomePrices") or []
                    idx = t.get("outcome_idx", 0)
                    if raw_prices and idx < len(raw_prices):
                        try:
                            t["current_market_price"] = float(raw_prices[idx])
                        except (ValueError, TypeError):
                            pass
                    continue

                # Market closed — determine resolution.
                raw_prices = market_data.get("outcomePrices") or []
                idx = t.get("outcome_idx", 0)
                if raw_prices and idx < len(raw_prices):
                    try:
                        resolution_price = float(raw_prices[idx])
                        t["won"] = resolution_price >= 0.99
                    except (ValueError, TypeError):
                        t["won"] = None
                t["resolved"] = True
                newly_resolved += 1
            except Exception as e:
                log.warning("check_and_resolve_trades: error for market_id=%s: %s", market_id, e)

        self._save(trades)
        return newly_resolved
