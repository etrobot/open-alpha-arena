import uuid
import datetime
from decimal import Decimal
from sqlalchemy.orm import Session
from database.models import (
    Order, Position, Trade, Account,
    CRYPTO_TAKER_FEE_RATE, CRYPTO_INTEREST_RATE_HOURLY, CRYPTO_MAX_LEVERAGE,
    CRYPTO_MIN_ORDER_QUANTITY, CRYPTO_LOT_SIZE, CRYPTO_MAINTENANCE_MARGIN_RATIO
)
from .market_data import get_last_price


def _calc_crypto_fee(notional: Decimal, leverage: int = 1) -> Decimal:
    """Calculate taker fee for CRYPTO market"""
    return notional * Decimal(str(CRYPTO_TAKER_FEE_RATE))


def _calculate_position_interest(position: Position) -> Decimal:
    """Calculate accumulated interest since last calculation"""
    if not position.last_interest_time or position.leverage <= 1:
        return Decimal(0)
    
    now = datetime.datetime.now(datetime.timezone.utc)
    # Handle both timezone-aware and naive datetimes
    last_time = position.last_interest_time
    if last_time.tzinfo is None:
        last_time = last_time.replace(tzinfo=datetime.timezone.utc)
    hours_elapsed = (now - last_time).total_seconds() / 3600
    
    # Interest only applies to borrowed amount (leveraged portion)
    borrowed_notional = Decimal(str(position.quantity)) * Decimal(str(position.avg_cost)) * (Decimal(position.leverage) - 1) / Decimal(position.leverage)
    interest = borrowed_notional * Decimal(str(CRYPTO_INTEREST_RATE_HOURLY)) * Decimal(str(hours_elapsed))
    
    return interest


def place_and_execute_crypto(
    db: Session,
    account: Account,
    symbol: str,
    name: str,
    side: str,
    order_type: str,
    price: float | None,
    quantity: float,
    leverage: int = 1
) -> Order:
    """
    Place and execute a CRYPTO order with leverage support.
    
    Args:
        account: Trading account
        symbol: Trading pair (e.g., 'BTC/USDT')
        side: 'LONG' (open long) / 'SHORT' (open short) / 'BUY' (close short) / 'SELL' (close long)
        leverage: Leverage multiplier (1 = spot, 2-50 = leveraged)
        quantity: Amount in base currency (e.g., BTC amount for BTC/USDT)
    
    Returns:
        Executed Order
    """
    if leverage < 1 or leverage > CRYPTO_MAX_LEVERAGE:
        raise ValueError(f"Leverage must be between 1 and {CRYPTO_MAX_LEVERAGE}")
    
    # Validate quantity
    if quantity < CRYPTO_MIN_ORDER_QUANTITY:
        raise ValueError(f"Quantity must be >= {CRYPTO_MIN_ORDER_QUANTITY}")
    
    # Get execution price
    exec_price = Decimal(str(price if (order_type == "LIMIT" and price) else get_last_price(symbol, "CRYPTO")))
    notional = exec_price * Decimal(str(quantity))
    
    # Calculate fees
    taker_fee = _calc_crypto_fee(notional, leverage)
    
    # Create order
    order = Order(
        version="v1",
        account_id=account.id,
        order_no=uuid.uuid4().hex[:16],
        symbol=symbol,
        name=name,
        market="CRYPTO",
        side=side.upper(),
        order_type=order_type,
        price=float(exec_price),
        quantity=quantity,
        leverage=leverage,
        filled_quantity=0,
        status="PENDING",
    )
    db.add(order)
    db.flush()
    
    # Get existing position
    pos = (
        db.query(Position)
        .filter(
            Position.account_id == account.id,
            Position.symbol == symbol,
            Position.market == "CRYPTO"
        )
        .first()
    )
    
    interest_charged = Decimal(0)
    
    # Handle different order sides
    if side.upper() in ("LONG", "SHORT"):
        # Opening a leveraged position
        is_long = side.upper() == "LONG"
        
        # Calculate margin required
        initial_margin = notional / Decimal(leverage)
        total_cost = initial_margin + taker_fee
        
        # Check if enough cash
        available_cash = Decimal(str(account.current_cash))
        if available_cash < total_cost:
            raise ValueError(f"Insufficient cash. Need {total_cost}, have {available_cash}")
        
        # Deduct margin and fee from cash
        account.current_cash = float(available_cash - total_cost)
        
        # Only track margin for leveraged positions (leverage > 1)
        if leverage > 1:
            account.margin_used = float(Decimal(str(account.margin_used)) + initial_margin)
        
        if pos and pos.leverage > 1 and pos.side:
            # Has existing leveraged position - calculate interest before modifying
            interest_charged = _calculate_position_interest(pos)
            if interest_charged > 0:
                pos.accumulated_interest = float(Decimal(str(pos.accumulated_interest)) + interest_charged)
                # Deduct interest from cash
                if Decimal(str(account.current_cash)) < interest_charged:
                    raise ValueError(f"Insufficient cash for interest payment: {interest_charged}")
                account.current_cash = float(Decimal(str(account.current_cash)) - interest_charged)
            
            # Check if adding to same side
            if pos.side == side.upper():
                # Adding to existing position - calculate new weighted average
                old_notional = Decimal(str(pos.quantity)) * Decimal(str(pos.avg_cost))
                new_qty = Decimal(str(pos.quantity)) + Decimal(str(quantity))
                new_cost = (old_notional + notional) / new_qty
                pos.quantity = float(new_qty)
                pos.avg_cost = float(new_cost)
                # Weighted average leverage
                pos.leverage = int((old_notional * pos.leverage + notional * leverage) / (old_notional + notional))
            else:
                raise ValueError(f"Cannot open {side} position while holding {pos.side} position. Close existing position first.")
        else:
            # Create new position or convert spot to leveraged
            if not pos:
                pos = Position(
                    version="v1",
                    account_id=account.id,
                    symbol=symbol,
                    name=name,
                    market="CRYPTO",
                    quantity=0,
                    available_quantity=0,
                    avg_cost=0,
                    leverage=1,
                )
                db.add(pos)
                db.flush()
            
            # Set leveraged position
            pos.quantity = quantity
            pos.available_quantity = quantity
            pos.avg_cost = float(exec_price)
            pos.leverage = leverage
            pos.side = side.upper()
        
        # Update interest timestamp
        pos.last_interest_time = datetime.datetime.now(datetime.timezone.utc)
    
    elif side.upper() in ("BUY", "SELL"):
        # Closing a position (partial or full)
        if not pos or pos.quantity == 0:
            raise ValueError("No position to close")
        
        # Calculate interest before closing
        interest_charged = _calculate_position_interest(pos)
        if interest_charged > 0:
            pos.accumulated_interest = float(Decimal(str(pos.accumulated_interest)) + interest_charged)
            if Decimal(str(account.current_cash)) < interest_charged:
                raise ValueError(f"Insufficient cash for interest payment: {interest_charged}")
            account.current_cash = float(Decimal(str(account.current_cash)) - interest_charged)
        
        if pos.leverage > 1:
            # Closing leveraged position
            # BUY closes SHORT, SELL closes LONG
            if (side.upper() == "SELL" and pos.side != "LONG") or (side.upper() == "BUY" and pos.side != "SHORT"):
                raise ValueError(f"Cannot {side} to close a {pos.side} position")
            
            if Decimal(str(quantity)) > Decimal(str(pos.quantity)):
                raise ValueError(f"Cannot close more than position size. Position: {pos.quantity}, Trying to close: {quantity}")
            
            # Calculate PnL
            entry_notional = Decimal(str(pos.avg_cost)) * Decimal(str(quantity))
            exit_notional = notional
            
            if pos.side == "LONG":
                pnl = exit_notional - entry_notional
            else:  # SHORT
                pnl = entry_notional - exit_notional
            
            # Release margin proportionally (only if leverage > 1)
            margin_released = entry_notional / Decimal(pos.leverage)
            
            # Net cash change = PnL + margin released - closing fee
            net_cash_change = pnl + margin_released - taker_fee
            account.current_cash = float(Decimal(str(account.current_cash)) + net_cash_change)
            
            # Only update margin_used for leveraged positions
            if pos.leverage > 1:
                account.margin_used = float(Decimal(str(account.margin_used)) - margin_released)
            
            # Update position
            pos.quantity = float(Decimal(str(pos.quantity)) - Decimal(str(quantity)))
            pos.available_quantity = float(Decimal(str(pos.available_quantity)) - Decimal(str(quantity)))
            
            if pos.quantity == 0:
                pos.side = None
                pos.leverage = 1
                pos.last_interest_time = None
            else:
                pos.last_interest_time = datetime.datetime.now(datetime.timezone.utc)
        else:
            # Closing spot position (simple sell)
            if side.upper() != "SELL":
                raise ValueError("Can only SELL spot positions")
            
            if Decimal(str(quantity)) > Decimal(str(pos.available_quantity)):
                raise ValueError(f"Insufficient position. Have: {pos.available_quantity}, Trying to sell: {quantity}")
            
            # Simple spot sell
            cash_gain = notional - taker_fee
            account.current_cash = float(Decimal(str(account.current_cash)) + cash_gain)
            
            pos.quantity = float(Decimal(str(pos.quantity)) - Decimal(str(quantity)))
            pos.available_quantity = float(Decimal(str(pos.available_quantity)) - Decimal(str(quantity)))
    
    else:
        raise ValueError(f"Invalid side: {side}. Must be LONG/SHORT (open) or BUY/SELL (close)")
    
    # Create trade record
    trade = Trade(
        order_id=order.id,
        account_id=account.id,
        symbol=symbol,
        name=name,
        market="CRYPTO",
        side=side.upper(),
        price=float(exec_price),
        quantity=quantity,
        commission=float(taker_fee),
        taker_fee=float(taker_fee),
        interest_charged=float(interest_charged),
    )
    db.add(trade)
    
    # Mark order as filled
    order.filled_quantity = quantity
    order.status = "FILLED"
    
    db.commit()
    db.refresh(order)
    db.refresh(account)
    if pos:
        db.refresh(pos)
    
    return order
