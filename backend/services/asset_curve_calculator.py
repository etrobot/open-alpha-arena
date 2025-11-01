"""
Asset Curve Calculator - New Algorithm
Draws curve by accounts, creates all-time list for every account: time, cash, positions.
Gets latest 20 close prices for all symbols, then fills curve with cash + sum(symbol price * position).
"""

from sqlalchemy.orm import Session
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timezone
import logging

from database.models import Trade, Account, CryptoKline
from services.market_data import get_kline_data


def get_all_asset_curves_data_new(db: Session, timeframe: str = "1h") -> List[Dict]:
    """
    New algorithm for asset curve calculation by accounts.
    
    Args:
        db: Database session
        timeframe: Time period for the curve, options: "5m", "1h", "1d"
        
    Returns:
        List of asset curve data points with timestamp, account info, and asset values
    """
    try:
        # Step 1: Get all active accounts
        accounts = db.query(Account).filter(Account.is_active == "true").all()
        if not accounts:
            return []
        
        logging.info(f"Found {len(accounts)} active accounts")
        
        # Step 2: Get all unique symbols from all account trades
        symbols_query = db.query(Trade.symbol, Trade.market).distinct().all()
        unique_symbols = set()
        for symbol, market in symbols_query:
            unique_symbols.add((symbol, market))
        
        if not unique_symbols:
            # No trades yet, return initial capital for all accounts at current time
            now = datetime.now()
            return [{
                "timestamp": int(now.timestamp()),
                "datetime_str": now.isoformat(),
                "account_id": account.id,
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(account.initial_capital),
                "initial_capital": float(account.initial_capital),
                "profit": 0.0,
                "profit_percentage": 0.0,
                "cash": float(account.initial_capital),
                "positions_value": 0.0,
            } for account in accounts]
        
        logging.info(f"Found {len(unique_symbols)} unique symbols: {unique_symbols}")
        
        # Step 3: Get latest 20 close prices for all symbols
        symbol_klines = {}
        for symbol, market in unique_symbols:
            try:
                klines = get_kline_data(symbol, market, timeframe, 20)
                if klines:
                    symbol_klines[(symbol, market)] = klines
                    logging.info(f"Fetched {len(klines)} klines for {symbol}.{market}")
            except Exception as e:
                logging.warning(f"Failed to fetch klines for {symbol}.{market}: {e}")
        
        if not symbol_klines:
            # Fallback to current time if no market data available
            now = datetime.now()
            return [{
                "timestamp": int(now.timestamp()),
                "datetime_str": now.isoformat(),
                "account_id": account.id,
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(account.initial_capital),
                "initial_capital": float(account.initial_capital),
                "profit": 0.0,
                "profit_percentage": 0.0,
                "cash": float(account.initial_capital),
                "positions_value": 0.0,
            } for account in accounts]
        
        # Step 4: Get common timestamps from market data
        first_klines = next(iter(symbol_klines.values()))
        timestamps = [k['timestamp'] for k in first_klines]
        
        logging.info(f"Processing {len(timestamps)} timestamps")
        
        # Step 5: Calculate asset curves for each account
        result = []
        
        for account in accounts:
            account_id = account.id
            logging.info(f"Processing account {account_id}: {account.name}")
            
            # Create all-time list for this account: time, cash, positions
            account_timeline = _create_account_timeline(db, account, timestamps, symbol_klines)
            result.extend(account_timeline)
        
        # Sort result by timestamp and account_id for consistent ordering
        result.sort(key=lambda x: (x['timestamp'], x['account_id']))
        
        logging.info(f"Generated {len(result)} data points for asset curves")
        return result
        
    except Exception as e:
        logging.error(f"Failed to calculate asset curves: {e}")
        return []


def _create_account_timeline(
    db: Session, 
    account: Account, 
    timestamps: List[int], 
    symbol_klines: Dict[Tuple[str, str], List[Dict]]
) -> List[Dict]:
    """
    Create all-time list for an account: time, cash, positions.
    Calculate cash + sum(symbol price * position) for each timestamp.
    
    Args:
        db: Database session
        account: Account object
        timestamps: List of timestamps to calculate for
        symbol_klines: Dictionary of symbol klines data
        
    Returns:
        List of timeline data points for the account
    """
    account_id = account.id
    
    # Get all trades for this account, ordered by time
    trades = db.query(Trade).filter(
        Trade.account_id == account_id
    ).order_by(Trade.trade_time.asc()).all()
    
    if not trades:
        # No trades, return initial capital at all timestamps
        first_klines = next(iter(symbol_klines.values()))
        return [{
            "timestamp": ts,
            "datetime_str": first_klines[i]['datetime_str'],
            "account_id": account.id,
            "user_id": account.user_id,
            "username": account.name,
            "total_assets": float(account.initial_capital),
            "initial_capital": float(account.initial_capital),
            "profit": 0.0,
            "profit_percentage": 0.0,
            "cash": float(account.initial_capital),
            "positions_value": 0.0,
        } for i, ts in enumerate(timestamps)]
    
    # Calculate holdings and cash at each timestamp
    timeline = []
    first_klines = next(iter(symbol_klines.values()))
    
    # Check if we should use actual account.current_cash for the last timestamp
    # This handles cases where cash was adjusted outside of trade history
    use_actual_cash_for_last = len(timestamps) > 0
    
    for i, ts in enumerate(timestamps):
        ts_datetime = datetime.fromtimestamp(ts, tz=timezone.utc)
        is_last_timestamp = (i == len(timestamps) - 1)
        
        # Calculate cash and positions up to this timestamp
        cash_change = 0.0
        position_quantities = {}
        
        for trade in trades:
            trade_time = trade.trade_time
            if not trade_time.tzinfo:
                trade_time = trade_time.replace(tzinfo=timezone.utc)
            
            if trade_time <= ts_datetime:
                # Update cash based on trade (include commission and interest)
                trade_amount = float(trade.price) * float(trade.quantity) + float(trade.commission) + float(trade.interest_charged)
                if trade.side == "BUY" or trade.side == "LONG":
                    cash_change -= trade_amount
                else:  # SELL or SHORT
                    cash_change += trade_amount
                
                # Update position quantity
                key = (trade.symbol, trade.market)
                if key not in position_quantities:
                    position_quantities[key] = 0.0
                
                if trade.side == "BUY" or trade.side == "LONG":
                    position_quantities[key] += float(trade.quantity)
                else:  # SELL or SHORT
                    position_quantities[key] -= float(trade.quantity)
        
        # For the last timestamp, use actual current_cash to account for any realized P&L or adjustments
        # For historical points, reconstruct from initial capital + cash changes
        if is_last_timestamp and use_actual_cash_for_last:
            current_cash = float(account.current_cash)
        else:
            current_cash = float(account.initial_capital) + cash_change
        
        # Calculate positions MARKET VALUE using prices at this timestamp
        # Market value = quantity * price (NOT * leverage!)
        # Leverage only affects margin requirement, not the position's equity value
        positions_value = 0.0
        
        # For the last timestamp, use actual Position table data to get accurate current positions
        if is_last_timestamp and use_actual_cash_for_last:
            from database.models import Position
            from services.market_data import get_last_price
            from decimal import Decimal
            positions = db.query(Position).filter(Position.account_id == account.id).all()
            for pos in positions:
                if pos.quantity > 0:
                    try:
                        price = get_last_price(pos.symbol, pos.market)
                        if price and price > 0:
                            price_dec = Decimal(str(price))
                            quantity_dec = Decimal(str(pos.quantity))
                            avg_cost_dec = Decimal(str(pos.avg_cost))
                            leverage_dec = Decimal(str(pos.leverage)) if pos.leverage and pos.leverage > 0 else Decimal("1")
                            
                            # Market value of position
                            market_value = quantity_dec * price_dec
                            
                            # For leveraged positions, only count margin + unrealized P&L
                            if leverage_dec > 1:
                                # Initial margin used
                                initial_margin = market_value / leverage_dec
                                # Unrealized P&L
                                unrealized_pnl = quantity_dec * (price_dec - avg_cost_dec)
                                # Position equity = margin + P&L
                                position_equity = initial_margin + unrealized_pnl
                            else:
                                # Non-leveraged position: equity = market value
                                position_equity = market_value
                            
                            positions_value += float(position_equity)
                    except Exception as e:
                        logging.warning(f"Could not get price for {pos.symbol}.{pos.market}: {e}")
        else:
            # For historical points, reconstruct from trades
            for (symbol, market), quantity in position_quantities.items():
                if quantity > 0 and (symbol, market) in symbol_klines:
                    klines = symbol_klines[(symbol, market)]
                    if i < len(klines) and klines[i]['close']:
                        price = float(klines[i]['close'])
                        # Market value (equity) = price * quantity
                        positions_value += price * quantity
        
        total_assets = current_cash + positions_value
        # Calculate profit: total_assets - initial_capital
        profit = total_assets - float(account.initial_capital)
        # Calculate profit percentage
        profit_percentage = (profit / float(account.initial_capital)) * 100 if float(account.initial_capital) > 0 else 0
        
        timeline.append({
            "timestamp": ts,
            "datetime_str": first_klines[i]['datetime_str'],
            "account_id": account.id,
            "user_id": account.user_id,
            "username": account.name,
            "total_assets": total_assets,
            "initial_capital": float(account.initial_capital),
            "profit": profit,
            "profit_percentage": profit_percentage,
            "cash": current_cash,
            "positions_value": positions_value,
        })
    
    return timeline


def get_account_asset_curve(db: Session, account_id: int, timeframe: str = "1h") -> List[Dict]:
    """
    Get asset curve data for a specific account.
    
    Args:
        db: Database session
        account_id: ID of the account to get curve for
        timeframe: Time period for the curve
        
    Returns:
        List of asset curve data points for the account
    """
    try:
        # Get the specific account
        account = db.query(Account).filter(
            Account.id == account_id,
            Account.is_active == "true"
        ).first()
        
        if not account:
            return []
        
        # Get all unique symbols from this account's trades
        symbols_query = db.query(Trade.symbol, Trade.market).filter(
            Trade.account_id == account_id
        ).distinct().all()
        
        unique_symbols = set()
        for symbol, market in symbols_query:
            unique_symbols.add((symbol, market))
        
        if not unique_symbols:
            # No trades yet, return initial capital
            now = datetime.now()
            return [{
                "timestamp": int(now.timestamp()),
                "datetime_str": now.isoformat(),
                "account_id": account.id,
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(account.initial_capital),
                "initial_capital": float(account.initial_capital),
                "profit": 0.0,
                "profit_percentage": 0.0,
                "cash": float(account.initial_capital),
                "positions_value": 0.0,
            }]
        
        # Get latest 20 close prices for account's symbols
        symbol_klines = {}
        for symbol, market in unique_symbols:
            try:
                klines = get_kline_data(symbol, market, timeframe, 20)
                if klines:
                    symbol_klines[(symbol, market)] = klines
            except Exception as e:
                logging.warning(f"Failed to fetch klines for {symbol}.{market}: {e}")
        
        if not symbol_klines:
            # Fallback to current time
            now = datetime.now()
            return [{
                "timestamp": int(now.timestamp()),
                "datetime_str": now.isoformat(),
                "account_id": account.id,
                "user_id": account.user_id,
                "username": account.name,
                "total_assets": float(account.initial_capital),
                "initial_capital": float(account.initial_capital),
                "profit": 0.0,
                "profit_percentage": 0.0,
                "cash": float(account.initial_capital),
                "positions_value": 0.0,
            }]
        
        # Get timestamps
        first_klines = next(iter(symbol_klines.values()))
        timestamps = [k['timestamp'] for k in first_klines]
        
        # Create timeline for this account
        timeline = _create_account_timeline(db, account, timestamps, symbol_klines)
        
        return timeline
        
    except Exception as e:
        logging.error(f"Failed to get account asset curve for account {account_id}: {e}")
        return []