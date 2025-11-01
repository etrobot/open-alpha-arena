"""
Account and Asset Curve API Routes (Cleaned)
"""

from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging

from database.connection import SessionLocal
from database.models import Account, Position, Trade, CryptoPrice

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/list")
async def list_all_accounts(db: Session = Depends(get_db)):
    """Get all active accounts (for paper trading demo)"""
    try:
        from database.models import User
        accounts = db.query(Account).filter(Account.is_active == "true").all()
        
        result = []
        for account in accounts:
            user = db.query(User).filter(User.id == account.user_id).first()
            result.append({
                "id": account.id,
                "user_id": account.user_id,
                "username": user.username if user else "unknown",
                "name": account.name,
                "account_type": account.account_type,
                "initial_capital": float(account.initial_capital),
                "current_cash": float(account.current_cash),
                "frozen_cash": float(account.frozen_cash),
                "model": account.model,
                "base_url": account.base_url,
                "api_key": account.api_key,
                "is_active": account.is_active == "true"
            })
        
        return result
    except Exception as e:
        logger.error(f"Failed to list accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list accounts: {str(e)}")


@router.get("/{account_id}/overview")
async def get_specific_account_overview(account_id: int, db: Session = Depends(get_db)):
    """Get overview for a specific account"""
    try:
        # Get the specific account
        account = db.query(Account).filter(
            Account.id == account_id,
            Account.is_active == "true"
        ).first()
        
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        # Calculate positions value for this specific account
        from services.asset_calculator import calc_positions_value
        positions_value = float(calc_positions_value(db, account.id) or 0.0)
        
        # Count positions and pending orders for this account
        positions_count = db.query(Position).filter(
            Position.account_id == account.id,
            Position.quantity > 0
        ).count()
        
        from database.models import Order
        pending_orders = db.query(Order).filter(
            Order.account_id == account.id,
            Order.status == "PENDING"
        ).count()
        
        return {
            "account": {
                "id": account.id,
                "name": account.name,
                "account_type": account.account_type,
                "current_cash": float(account.current_cash),
                "frozen_cash": float(account.frozen_cash),
            },
            "total_assets": positions_value + float(account.current_cash),
            "positions_value": positions_value,
            "positions_count": positions_count,
            "pending_orders": pending_orders,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get account {account_id} overview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get account overview: {str(e)}")


@router.get("/overview")
async def get_account_overview(db: Session = Depends(get_db)):
    """Get overview for the default account (for paper trading demo)"""
    try:
        # Get the first active account (default account)
        account = db.query(Account).filter(Account.is_active == "true").first()
        
        if not account:
            raise HTTPException(status_code=404, detail="No active account found")
        
        # Calculate positions value
        from services.asset_calculator import calc_positions_value
        positions_value = float(calc_positions_value(db, account.id) or 0.0)
        
        # Count positions and pending orders
        positions_count = db.query(Position).filter(
            Position.account_id == account.id,
            Position.quantity > 0
        ).count()
        
        from database.models import Order
        pending_orders = db.query(Order).filter(
            Order.account_id == account.id,
            Order.status == "PENDING"
        ).count()
        
        return {
            "account": {
                "id": account.id,
                "name": account.name,
                "account_type": account.account_type,
                "current_cash": float(account.current_cash),
                "frozen_cash": float(account.frozen_cash),
            },
            "portfolio": {
                "total_assets": positions_value + float(account.current_cash),
                "positions_value": positions_value,
                "positions_count": positions_count,
                "pending_orders": pending_orders,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get overview: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get overview: {str(e)}")


@router.post("/")
async def create_new_account(payload: dict, db: Session = Depends(get_db)):
    """Create a new account for the default user (for paper trading demo)"""
    try:
        from database.models import User
        
        # Get the default user (or first user)
        user = db.query(User).filter(User.username == "default").first()
        if not user:
            user = db.query(User).first()
        
        if not user:
            raise HTTPException(status_code=404, detail="No user found")
        
        # Validate required fields
        if "name" not in payload or not payload["name"]:
            raise HTTPException(status_code=400, detail="Account name is required")
        
        # Create new account
        new_account = Account(
            user_id=user.id,
            version="v1",
            name=payload["name"],
            account_type=payload.get("account_type", "AI"),
            model=payload.get("model", "gpt-4-turbo"),
            base_url=payload.get("base_url", "https://api.openai.com/v1"),
            api_key=payload.get("api_key", ""),
            initial_capital=float(payload.get("initial_capital", 10000.0)),
            current_cash=float(payload.get("initial_capital", 10000.0)),
            frozen_cash=0.0,
            is_active="true"
        )
        
        db.add(new_account)
        db.commit()
        db.refresh(new_account)
        
        # Reset auto trading job after creating new account
        try:
            from services.scheduler import reset_auto_trading_job
            reset_auto_trading_job()
            logger.info("Auto trading job reset successfully after account creation")
        except Exception as e:
            logger.warning(f"Failed to reset auto trading job: {e}")
        
        return {
            "id": new_account.id,
            "user_id": new_account.user_id,
            "username": user.username,
            "name": new_account.name,
            "account_type": new_account.account_type,
            "initial_capital": float(new_account.initial_capital),
            "current_cash": float(new_account.current_cash),
            "frozen_cash": float(new_account.frozen_cash),
            "model": new_account.model,
            "base_url": new_account.base_url,
            "api_key": new_account.api_key,
            "is_active": new_account.is_active == "true"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create account: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create account: {str(e)}")


@router.put("/{account_id}")
async def update_account_settings(account_id: int, payload: dict, db: Session = Depends(get_db)):
    """Update account settings (for paper trading demo)"""
    try:
        logger.info(f"Updating account {account_id} with payload: {payload}")
        
        account = db.query(Account).filter(
            Account.id == account_id,
            Account.is_active == "true"
        ).first()
        
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        
        # Update fields if provided (allow empty strings for api_key and base_url)
        if "name" in payload:
            if payload["name"]:
                account.name = payload["name"]
                logger.info(f"Updated name to: {payload['name']}")
            else:
                raise HTTPException(status_code=400, detail="Account name cannot be empty")
        
        if "model" in payload:
            account.model = payload["model"] if payload["model"] else None
            logger.info(f"Updated model to: {account.model}")
        
        if "base_url" in payload:
            account.base_url = payload["base_url"]
            logger.info(f"Updated base_url to: {account.base_url}")
        
        if "api_key" in payload:
            account.api_key = payload["api_key"]
            logger.info(f"Updated api_key (length: {len(payload['api_key']) if payload['api_key'] else 0})")
        
        db.commit()
        db.refresh(account)
        logger.info(f"Account {account_id} updated successfully")
        
        # Reset auto trading job after account update
        try:
            from services.scheduler import reset_auto_trading_job
            reset_auto_trading_job()
            logger.info("Auto trading job reset successfully after account update")
        except Exception as e:
            logger.warning(f"Failed to reset auto trading job: {e}")
        
        from database.models import User
        user = db.query(User).filter(User.id == account.user_id).first()
        
        return {
            "id": account.id,
            "user_id": account.user_id,
            "username": user.username if user else "unknown",
            "name": account.name,
            "account_type": account.account_type,
            "initial_capital": float(account.initial_capital),
            "current_cash": float(account.current_cash),
            "frozen_cash": float(account.frozen_cash),
            "model": account.model,
            "base_url": account.base_url,
            "api_key": account.api_key,
            "is_active": account.is_active == "true"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update account: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update account: {str(e)}")


@router.get("/asset-curve/timeframe")
async def get_asset_curve_by_timeframe(
    timeframe: str = "1d",
    db: Session = Depends(get_db)
):
    """Get asset curve data for all accounts within a specified timeframe (20 data points)
    
    Args:
        timeframe: Time period, options: 5m, 1h, 1d
    """
    try:
        # Validate timeframe
        valid_timeframes = ["5m", "1h", "1d"]
        if timeframe not in valid_timeframes:
            raise HTTPException(status_code=400, detail=f"Invalid timeframe. Must be one of: {', '.join(valid_timeframes)}")
        
        # Map timeframe to period for kline data
        timeframe_map = {
            "5m": "5m",
            "1h": "1h",
            "1d": "1d"
        }
        period = timeframe_map[timeframe]
        
        # Get all active accounts
        accounts = db.query(Account).filter(Account.is_active == "true").all()
        if not accounts:
            return []
        
        # Get all unique symbols from all account positions and trades
        symbols_query = db.query(Trade.symbol, Trade.market).distinct().all()
        unique_symbols = set()
        for symbol, market in symbols_query:
            unique_symbols.add((symbol, market))
        
        if not unique_symbols:
            # No trades yet, return initial capital for all accounts
            now = datetime.now()
            return [{
                "timestamp": int(now.timestamp()),
                "datetime_str": now.isoformat(),
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(account.initial_capital),
                "initial_capital": float(account.initial_capital),
                "profit": 0.0,
                "profit_percentage": 0.0,
                "cash": float(account.current_cash),
                "positions_value": 0.0,
            } for account in accounts]
        
        # Fetch kline data for all symbols (20 points)
        from services.market_data import get_kline_data
        
        symbol_klines = {}
        for symbol, market in unique_symbols:
            try:
                klines = get_kline_data(symbol, market, period, 20)
                if klines:
                    symbol_klines[(symbol, market)] = klines
                    logger.info(f"Fetched {len(klines)} klines for {symbol}.{market}")
            except Exception as e:
                logger.warning(f"Failed to fetch klines for {symbol}.{market}: {e}")
        
        if not symbol_klines:
            raise HTTPException(status_code=500, detail="Failed to fetch market data")
        
        # Get timestamps from the first symbol's klines
        first_klines = next(iter(symbol_klines.values()))
        timestamps = [k['timestamp'] for k in first_klines]
        
        # Calculate asset value for each account at each timestamp
        result = []
        for account in accounts:
            account_id = account.id
            
            # Get all trades for this account
            trades = db.query(Trade).filter(
                Trade.account_id == account_id
            ).order_by(Trade.trade_time.asc()).all()
            
            if not trades:
                # No trades, return initial capital at all timestamps
                for i, ts in enumerate(timestamps):
                    result.append({
                        "timestamp": ts,
                        "datetime_str": first_klines[i]['datetime_str'],
                        "user_id": account.user_id,
                        "username": account.name,
                        "total_assets": float(account.initial_capital),
                        "initial_capital": float(account.initial_capital),
                        "profit": 0.0,
                        "profit_percentage": 0.0,
                        "cash": float(account.initial_capital),
                        "positions_value": 0.0,
                    })
                continue
            
            # Calculate holdings and cash at each timestamp
            for i, ts in enumerate(timestamps):
                ts_datetime = datetime.fromtimestamp(ts, tz=timezone.utc)
                is_last_timestamp = (i == len(timestamps) - 1)
                
                # Calculate cash changes up to this timestamp
                cash_change = 0.0
                position_quantities = {}
                
                for trade in trades:
                    trade_time = trade.trade_time
                    if not trade_time.tzinfo:
                        trade_time = trade_time.replace(tzinfo=timezone.utc)
                    
                    if trade_time <= ts_datetime:
                        # Update cash (include commission and interest)
                        trade_amount = float(trade.price) * float(trade.quantity) + float(trade.commission) + float(trade.interest_charged)
                        if trade.side == "BUY" or trade.side == "LONG":
                            cash_change -= trade_amount
                        else:  # SELL or SHORT
                            cash_change += trade_amount
                        
                        # Update position
                        key = (trade.symbol, trade.market)
                        if key not in position_quantities:
                            position_quantities[key] = 0.0
                        
                        if trade.side == "BUY" or trade.side == "LONG":
                            position_quantities[key] += float(trade.quantity)
                        else:  # SELL or SHORT
                            position_quantities[key] -= float(trade.quantity)
                
                # For the last timestamp, use actual current_cash
                if is_last_timestamp:
                    current_cash = float(account.current_cash)
                else:
                    current_cash = float(account.initial_capital) + cash_change
                
                # Calculate positions value using prices at this timestamp
                positions_value = 0.0
                
                # For the last timestamp, use actual Position table data
                if is_last_timestamp:
                    from services.asset_calculator import calc_positions_market_value
                    positions_value = calc_positions_market_value(db, account.id)
                else:
                    # For historical points, reconstruct from trades
                    for (symbol, market), quantity in position_quantities.items():
                        if quantity > 0 and (symbol, market) in symbol_klines:
                            klines = symbol_klines[(symbol, market)]
                            if i < len(klines):
                                price = klines[i]['close']
                                if price:
                                    positions_value += float(price) * quantity
                
                total_assets = current_cash + positions_value
                
                # Calculate profit: total_assets - initial_capital
                initial_capital = float(account.initial_capital)
                profit = total_assets - initial_capital
                profit_percentage = (profit / initial_capital) * 100 if initial_capital > 0 else 0.0
                
                result.append({
                    "timestamp": ts,
                    "datetime_str": first_klines[i]['datetime_str'],
                    "user_id": account.user_id,
                    "username": account.name,
                    "total_assets": total_assets,
                    "initial_capital": initial_capital,
                    "profit": profit,
                    "profit_percentage": profit_percentage,
                    "cash": current_cash,
                    "positions_value": positions_value,
                })
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get asset curve for timeframe: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get asset curve for timeframe: {str(e)}")


@router.post("/test-llm")
async def test_llm_connection(payload: dict):
    """Test LLM connection with provided credentials"""
    try:
        import requests
        import json
        
        model = payload.get("model", "gpt-3.5-turbo")
        base_url = payload.get("base_url", "https://api.openai.com/v1")
        api_key = payload.get("api_key", "")
        
        if not api_key:
            return {"success": False, "message": "API key is required"}
        
        if not base_url:
            return {"success": False, "message": "Base URL is required"}
        
        # Clean up base_url - ensure it doesn't end with slash
        if base_url.endswith('/'):
            base_url = base_url.rstrip('/')
        
        # Test the connection with a simple completion request
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            
            # Use OpenAI-compatible chat completions format
            payload_data = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Say 'Connection test successful' if you can read this."}
                ],
                "max_tokens": 50,
                "temperature": 0
            }
            
            # Construct API endpoint URL
            api_endpoint = f"{base_url}/chat/completions"
            
            # Make the request
            response = requests.post(
                api_endpoint,
                headers=headers,
                json=payload_data,
                timeout=10.0,
                verify=False  # Disable SSL verification for custom AI endpoints
            )
            
            # Check response status
            if response.status_code == 200:
                result = response.json()
                
                # Extract text from OpenAI-compatible response format
                if "choices" in result and len(result["choices"]) > 0:
                    message = result["choices"][0].get("message", {})
                    content = message.get("content", "")
                    
                    if content:
                        logger.info(f"LLM test successful for model {model} at {base_url}")
                        return {
                            "success": True, 
                            "message": f"Connection successful! Model {model} responded correctly.",
                            "response": content
                        }
                    else:
                        return {"success": False, "message": "LLM responded but with empty content"}
                else:
                    return {"success": False, "message": "Unexpected response format from LLM"}
                    
            elif response.status_code == 401:
                return {"success": False, "message": "Authentication failed. Please check your API key."}
            elif response.status_code == 403:
                return {"success": False, "message": "Permission denied. Your API key may not have access to this model."}
            elif response.status_code == 429:
                return {"success": False, "message": "Rate limit exceeded. Please try again later."}
            elif response.status_code == 404:
                return {"success": False, "message": f"Model '{model}' not found or endpoint not available."}
            else:
                return {"success": False, "message": f"API returned status {response.status_code}: {response.text}"}
                
        except requests.ConnectionError:
            return {"success": False, "message": f"Failed to connect to {base_url}. Please check the base URL."}
        except requests.Timeout:
            return {"success": False, "message": "Request timed out. The LLM service may be unavailable."}
        except json.JSONDecodeError:
            return {"success": False, "message": "Invalid JSON response from LLM service."}
        except requests.RequestException as e:
            logger.error(f"LLM test request failed: {e}", exc_info=True)
            return {"success": False, "message": f"Connection test failed: {str(e)}"}
        except Exception as e:
            logger.error(f"LLM test failed: {e}", exc_info=True)
            return {"success": False, "message": f"Connection test failed: {str(e)}"}
            
    except Exception as e:
        logger.error(f"Failed to test LLM connection: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to test LLM connection: {str(e)}"}
