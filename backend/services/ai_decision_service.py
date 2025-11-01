"""
AI Decision Service - Handles AI model API calls for trading decisions
"""
import logging
import random
import json
import time
from decimal import Decimal
from typing import Dict, Optional, List

import requests
from sqlalchemy.orm import Session

from database.models import Position, Account, AIDecisionLog
from services.asset_calculator import calc_positions_value
from services.news_feed import fetch_latest_news


logger = logging.getLogger(__name__)

#  mode API keys that should be skipped
DEMO_API_KEYS = {
    "default-key-please-update-in-settings",
    "default",
    "",
    None
}

SUPPORTED_SYMBOLS: Dict[str, str] = {
    "BTC": "Bitcoin",
    "ETH": "Ethereum",
    "SOL": "Solana",
    "DOGE": "Dogecoin",
    "XRP": "Ripple",
    "BNB": "Binance Coin",
}


def _is_default_api_key(api_key: str) -> bool:
    """Check if the API key is a default/placeholder key that should be skipped"""
    return api_key in DEMO_API_KEYS


def _get_portfolio_data(db: Session, account: Account) -> Dict:
    """Get current portfolio positions and values"""
    positions = db.query(Position).filter(
        Position.account_id == account.id,
        Position.market == "CRYPTO"
    ).all()

    portfolio = {}
    for pos in positions:
        if float(pos.quantity) > 0:
            portfolio[pos.symbol] = {
                "quantity": float(pos.quantity),
                "avg_cost": float(pos.avg_cost),
                "current_value": float(pos.quantity) * float(pos.avg_cost),
                "side": (pos.side or "LONG").upper(),  # Include position direction
                "leverage": pos.leverage  # Include leverage
            }

    return {
        "cash": float(account.current_cash),
        "frozen_cash": float(account.frozen_cash),
        "positions": portfolio,
        "total_assets": float(account.current_cash) + calc_positions_value(db, account.id)
    }


def call_ai_for_decision(account: Account, portfolio: Dict, prices: Dict[str, float]) -> Optional[Dict]:
    """Call AI model API to get trading decision"""
    # Check if this is a default API key
    if _is_default_api_key(account.api_key):
        logger.info(f"Skipping AI trading for account {account.name} - using default API key")
        return None

    try:
        news_summary = fetch_latest_news()
        news_section = news_summary if news_summary else "No recent CoinJournal news available."

        prompt = f"""You are a cryptocurrency trading AI. Based on the following portfolio and market data, decide on a trading action.

Portfolio Data:
- Cash Available: ${portfolio['cash']:.2f}
- Frozen Cash: ${portfolio['frozen_cash']:.2f}
- Total Assets: ${portfolio['total_assets']:.2f}
- Current Positions (each shows quantity, avg_cost, current_value, side: LONG/SHORT, leverage): 
{json.dumps(portfolio['positions'], indent=2)}

Current Market Prices:
{json.dumps(prices, indent=2)}

Latest Crypto News (CoinJournal):
{news_section}

Analyze the market and portfolio, then respond with ONLY a JSON object in this exact format:
{{
  "operation": "open" or "close" or "hold",
  "symbol": "BTC" or "ETH" or "SOL" or "BNB" or "XRP" or "DOGE",
  "direction": "long" or "short",
  "target_portion_of_balance": 0.2,
  "leverage": 3,
  "reason": "Brief explanation of your decision"
}}

Rules:
- Only ONE position per coin allowed.
- operation must be "open", "close", or "hold"
- direction must be "long" or "short"
- For "open": Open a new position. You can open LONG (betting price goes up) or SHORT (betting price goes down)
  - symbol: which coin to trade
  - direction: "long" or "short"
  - target_portion_of_balance: % of available cash to use (0.0-1.0)
  - leverage: leverage multiplier (1-10, higher = more risk/reward)
- For "close": Close an existing position
  - symbol: which coin position to close
  - direction: must match the position side you want to close ("long" or "short")
  - target_portion_of_balance: % of position to close (0.0-1.0, use 1.0 to close entire position)
- For "hold": no action taken, direction can be omitted
- IMPORTANT: You can only hold ONE position per coin at a time (either long OR short, not both)
- Before opening a new position, check Current Positions to see if you already have a position on that coin
- You can only close positions that you currently hold (check Current Positions for side: "LONG" or "SHORT")
- leverage is typically 1-10x; only use high leverage (>5x) if you're very confident
- Only choose symbols you have price data for"""

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {account.api_key}"
        }

        # Use OpenAI-compatible chat completions format
        payload = {
            "model": account.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7,
            "max_tokens": 1000
        }

        # Construct API endpoint URL
        # Remove trailing slash from base_url if present
        base_url = account.base_url.rstrip('/')
        # Use /chat/completions endpoint (OpenAI-compatible)
        api_endpoint = f"{base_url}/chat/completions"

        # Retry logic for rate limiting
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    api_endpoint,
                    headers=headers,
                    json=payload,
                    timeout=30,
                    verify=False  # Disable SSL verification for custom AI endpoints
                )

                if response.status_code == 200:
                    break  # Success, exit retry loop
                elif response.status_code == 429:
                    # Rate limited, wait and retry
                    wait_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff with jitter
                    logger.warning(f"AI API rate limited (attempt {attempt + 1}/{max_retries}), waiting {wait_time:.1f}s...")
                    if attempt < max_retries - 1:  # Don't wait on the last attempt
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"AI API rate limited after {max_retries} attempts: {response.text}")
                        return None
                else:
                    logger.error(f"AI API returned status {response.status_code}: {response.text}")
                    return None
            except requests.RequestException as req_err:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.warning(f"AI API request failed (attempt {attempt + 1}/{max_retries}), retrying in {wait_time:.1f}s: {req_err}")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error(f"AI API request failed after {max_retries} attempts: {req_err}")
                    return None

        result = response.json()

        # Extract text from OpenAI-compatible response format
        if "choices" in result and len(result["choices"]) > 0:
            choice = result["choices"][0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            # Check if response was truncated due to length limit
            if finish_reason == "length":
                logger.warning(f"AI response was truncated due to token limit. Consider increasing max_tokens.")
                # Try to get content from reasoning field if available (some models put partial content there)
                text_content = message.get("reasoning", "") or message.get("content", "")
            else:
                text_content = message.get("content", "")

            if not text_content:
                logger.error(f"Empty content in AI response: {result}")
                return None

            # Try to extract JSON from the text
            # Sometimes AI might wrap JSON in markdown code blocks
            text_content = text_content.strip()
            if "```json" in text_content:
                text_content = text_content.split("```json")[1].split("```", 1)[0].strip()
            elif "```" in text_content:
                text_content = text_content.split("```", 1)[1].split("```", 1)[0].strip()

            # Handle potential JSON parsing issues with escape sequences
            try:
                decision = json.loads(text_content)
            except json.JSONDecodeError as parse_err:
                # Try to fix common JSON issues
                logger.warning(f"Initial JSON parse failed: {parse_err}")
                logger.warning(f"Problematic content: {text_content[:200]}...")

                # Try to clean up the text content
                cleaned_content = text_content

                # Replace problematic characters that might break JSON
                cleaned_content = cleaned_content.replace('\n', ' ')
                cleaned_content = cleaned_content.replace('\r', ' ')
                cleaned_content = cleaned_content.replace('\t', ' ')

                # Handle unescaped quotes in strings by escaping them
                import re
                # Try a simpler approach to fix common JSON issues
                # Replace smart quotes and em-dashes with regular equivalents
                cleaned_content = cleaned_content.replace('"', '"').replace('"', '"')
                cleaned_content = cleaned_content.replace('’', "'").replace('‘', "'")
                cleaned_content = cleaned_content.replace('–', '-').replace('—', '-')
                cleaned_content = cleaned_content.replace('‑', '-')  # Non-breaking hyphen

                # Try parsing again
                try:
                    decision = json.loads(cleaned_content)
                    logger.info("Successfully parsed JSON after cleanup")
                except json.JSONDecodeError:
                    # If still failing, try to extract just the essential parts
                    logger.error("JSON parsing failed even after cleanup, attempting manual extraction")
                    try:
                        # Extract operation, symbol, direction, portion, leverage, reason
                        operation_match = re.search(r'"operation":\s*"([^"]+)"', text_content)
                        symbol_match = re.search(r'"symbol":\s*"([^"]+)"', text_content)
                        direction_match = re.search(r'"direction":\s*"([^"]+)"', text_content)
                        portion_match = re.search(r'"target_portion_of_balance":\s*([0-9.]+)', text_content)
                        leverage_match = re.search(r'"leverage":\s*([0-9]+)', text_content)
                        reason_match = re.search(r'"reason":\s*"([^"]*)', text_content)

                        if operation_match and symbol_match and portion_match:
                            decision = {
                                "operation": operation_match.group(1),
                                "symbol": symbol_match.group(1),
                                "direction": direction_match.group(1).lower() if direction_match else "long",
                                "target_portion_of_balance": float(portion_match.group(1)),
                                "leverage": int(leverage_match.group(1)) if leverage_match else 1,
                                "reason": reason_match.group(1) if reason_match else "AI response parsing issue"
                            }
                            logger.info("Successfully extracted AI decision with direction and leverage manually")
                        else:
                            raise json.JSONDecodeError("Could not extract required fields", text_content, 0)
                    except Exception:
                        raise parse_err  # Re-raise original error

            # Validate that decision is a dict with required structure
            if not isinstance(decision, dict):
                logger.error(f"AI response is not a dict: {type(decision)}")
                return None

            logger.info(f"AI decision for {account.name}: {decision}")
            # 正常化leverage，未给时补1
            if "leverage" not in decision or not decision["leverage"]:
                decision["leverage"] = 1
            
            # 正常化direction，未给时补long
            if "direction" not in decision or not decision["direction"]:
                decision["direction"] = "long"
            else:
                decision["direction"] = decision["direction"].lower()
            return decision

        logger.error(f"Unexpected AI response format: {result}")
        return None

    except requests.RequestException as err:
        logger.error(f"AI API request failed: {err}")
        return None
    except json.JSONDecodeError as err:
        logger.error(f"Failed to parse AI response as JSON: {err}")
        # Try to log the content that failed to parse
        try:
            if 'text_content' in locals():
                logger.error(f"Content that failed to parse: {text_content[:500]}")
        except:
            pass
        return None
    except Exception as err:
        logger.error(f"Unexpected error calling AI: {err}", exc_info=True)
        return None


def save_ai_decision(db: Session, account: Account, decision: Dict, portfolio: Dict, executed: bool = False, order_id: Optional[int] = None) -> None:
    """Save AI decision to the decision log"""
    try:
        operation = decision.get("operation", "").lower() if decision.get("operation") else ""
        symbol_raw = decision.get("symbol")
        symbol = symbol_raw.upper() if symbol_raw else None
        target_portion = float(decision.get("target_portion_of_balance", 0)) if decision.get("target_portion_of_balance") is not None else 0.0
        reason = decision.get("reason", "No reason provided")

        # Calculate previous portion for the symbol
        prev_portion = 0.0
        if operation in ["close", "hold"] and symbol:
            positions = portfolio.get("positions", {})
            if symbol in positions:
                symbol_value = positions[symbol]["current_value"]
                total_balance = portfolio["total_assets"]
                if total_balance > 0:
                    prev_portion = symbol_value / total_balance

        # Normalize leverage from decision
        try:
            leverage_val = int(decision.get("leverage", 1))
        except Exception:
            leverage_val = 1
        if leverage_val < 1:
            leverage_val = 1

        # Create decision log entry
        decision_log = AIDecisionLog(
            account_id=account.id,
            reason=reason,
            operation=operation,
            symbol=symbol if operation != "hold" else None,
            prev_portion=Decimal(str(prev_portion)),
            target_portion=Decimal(str(target_portion)),
            total_balance=Decimal(str(portfolio["total_assets"])),
            executed="true" if executed else "false",
            order_id=order_id,
            leverage=leverage_val
        )

        db.add(decision_log)
        db.commit()

        symbol_str = symbol if symbol else "N/A"
        logger.info(f"Saved AI decision log for account {account.name}: {operation} {symbol_str} "
                   f"prev_portion={prev_portion:.4f} target_portion={target_portion:.4f} leverage={leverage_val} executed={executed}")

    except Exception as err:
        logger.error(f"Failed to save AI decision log: {err}")
        db.rollback()


def get_active_ai_accounts(db: Session) -> List[Account]:
    """Get all active AI accounts that are not using default API key"""
    accounts = db.query(Account).filter(
        Account.is_active == "true",
        Account.account_type == "AI"
    ).all()

    if not accounts:
        return []

    # Filter out default accounts
    valid_accounts = [acc for acc in accounts if not _is_default_api_key(acc.api_key)]

    if not valid_accounts:
        logger.debug("No valid AI accounts found (all using default keys)")
        return []

    return valid_accounts