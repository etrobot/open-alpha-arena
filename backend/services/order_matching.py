"""
Order matching service
Implements conditional execution logic for limit orders
"""

import uuid
from decimal import Decimal
from typing import Optional, Tuple
from sqlalchemy.orm import Session
import logging

from database.models import Order, Position, Trade, Account, User, CRYPTO_MIN_COMMISSION, CRYPTO_COMMISSION_RATE, CRYPTO_MIN_ORDER_QUANTITY, CRYPTO_LOT_SIZE
from .market_data import get_last_price

logger = logging.getLogger(__name__)


def _calc_commission(notional: Decimal) -> Decimal:
    """Calculate commission"""
    pct_fee = notional * Decimal(str(CRYPTO_COMMISSION_RATE))
    min_fee = Decimal(str(CRYPTO_MIN_COMMISSION))
    return max(pct_fee, min_fee)


def create_order(db: Session, account: Account, symbol: str, name: str,
                side: str, order_type: str, price: Optional[float], quantity: float, leverage: int = 1) -> Order:
    """
    Create limit order

    Args:
        db: Database session
        account: Account object
        symbol: crypto Symbol
        name: crypto name
        side: Buy/side direction (BUY/SELL)
        order_type: Order type (MARKET/LIMIT)
        price: Order price (required for limit orders)
        quantity: Order quantity

    Returns:
        Created order object

    Raises:
        ValueError: Parameter validation failed or insufficient funds/positions
    """
    # Basic parameter validation (crypto-only)
    
    # For crypto, we support fractional quantities, so no lot size validation needed
    # if quantity % CRYPTO_LOT_SIZE != 0:
    #     raise ValueError(f"Order quantity must be integer multiple of {CRYPTO_LOT_SIZE}")

    # For crypto, allow very small quantities (minimum $1 worth)
    if quantity <= 0:
        raise ValueError(f"Order quantity must be > 0")

    if order_type == "LIMIT" and (price is None or price <= 0):
        raise ValueError("Limit order must specify valid order price")
    
    # Get current market price for fund validation (only when cookie is configured)
    current_market_price = None
    if order_type == "MARKET":
        # Market order: get current price for fund validation
        try:
            current_market_price = get_last_price(symbol)
        except Exception as err:
            raise ValueError(f"Unable to get market price for market order: {err}")
        check_price = Decimal(str(current_market_price))
    else:
        # Limit order: use order price for fund validation
        check_price = Decimal(str(price))

    # Pre-check funds and positions
    if side == "BUY":
        # Buy: check if sufficient cash available
        notional = check_price * Decimal(str(quantity))
        commission = _calc_commission(notional)

        if leverage > 1:
            initial_margin = notional / Decimal(str(leverage))
            cash_needed = initial_margin + commission
        else: # 现货
            cash_needed = notional + commission

        if Decimal(str(account.current_cash)) < cash_needed:
            raise ValueError(f"Insufficient cash. Need ${cash_needed:.2f}, current cash ${account.current_cash:.2f}")

    else:  # SELL
        # Sell: check if sufficient positions available
        position = (
            db.query(Position)
            .filter(Position.account_id == account.id, Position.symbol == symbol, Position.market == "CRYPTO")
            .first()
        )

        if not position or Decimal(str(position.available_quantity)) < Decimal(str(quantity)):
            available_qty = float(position.available_quantity) if position else 0
            raise ValueError(f"Insufficient positions. Need {quantity} {symbol}, available {available_qty} {symbol}")
    
    # Create order
    order = Order(
        version="v1",
        account_id=account.id,
        order_no=uuid.uuid4().hex[:16],
        symbol=symbol,
        name=name,
        market="CRYPTO",
        side=side,
        order_type=order_type,
        price=price,
        quantity=quantity,
        leverage=leverage,
        filled_quantity=0,
        status="PENDING",
    )

    db.add(order)
    db.flush()

    logger.info(f"Created limit order: {order.order_no}, {side} {quantity} {symbol} @ {price if price else 'MARKET'}")

    return order


def check_and_execute_order(db: Session, order: Order) -> bool:
    """
    Check and execute limit order

    Execution conditions:
    - Buy: order price >= current market price and sufficient funds
    - Sell: order price <= current market price and sufficient positions

    Args:
        db: Database session
        order: Order to check

    Returns:
        Whether order was executed
    """
    if order.status != "PENDING":
        return False
    
    # Check if cookie is configured, skip order checking if not
    try:
        # Get current market price
        current_price = get_last_price(order.symbol, order.market)
        current_price_decimal = Decimal(str(current_price))

        # Get user information
        account = db.query(Account).filter(Account.id == order.account_id).first()
        if not account:
            logger.error(f"Account corresponding to order {order.order_no} does not exist")
            return False

        # Check execution conditions
        should_execute = False
        execution_price = current_price_decimal

        if order.order_type == "MARKET":
            # Market order executes immediately
            should_execute = True
            execution_price = current_price_decimal

        elif order.order_type == "LIMIT":
            # Limit order conditional execution
            limit_price = Decimal(str(order.price))

            if order.side == "BUY":
                # Buy: order price >= current market price
                if limit_price >= current_price_decimal:
                    should_execute = True
                    execution_price = current_price_decimal  # Execute at market price

            else:  # SELL
                # Sell: order price <= current market price
                if limit_price <= current_price_decimal:
                    should_execute = True
                    execution_price = current_price_decimal  # Execute at market price

        if not should_execute:
            logger.debug(f"Order {order.order_no} does not meet execution condition: {order.side} {order.price} vs market {current_price}")
            return False

        # Execute order
        return _execute_order(db, order, account, execution_price)

    except Exception as e:
        logger.error(f"Error checking order {order.order_no}: {e}")
        return False


def _release_frozen_on_fill(account: Account, order: Order, execution_price: Decimal, commission: Decimal):
    """Release frozen cash on fill (for BUY only)"""
    if order.side == "BUY":
        # Estimated frozen amount may differ from actual execution, release based on actual execution amount
        notional = execution_price * Decimal(order.quantity)
        frozen_to_release = notional + commission
        account.frozen_cash = float(max(Decimal(str(account.frozen_cash)) - frozen_to_release, Decimal('0')))


def _execute_order(db: Session, order: Order, account: Account, execution_price: Decimal) -> bool:
    """
    Execute order fill

    Args:
        db: Database session
        order: Order object
        account: Account object
        execution_price: Execution price

    Returns:
        Whether execution was successful
    """
    try:
        quantity = Decimal(str(order.quantity))
        notional = execution_price * quantity
        commission = _calc_commission(notional)
        leverage = Decimal(str(order.leverage))

        # Re-check funds and positions (prevent concurrency issues)
        if order.side == "BUY":
            if leverage > 1:
                initial_margin = notional / leverage
                cash_needed = initial_margin + commission
            else:
                cash_needed = notional + commission
            
            if Decimal(str(account.current_cash)) < cash_needed:
                logger.warning(f"Insufficient cash when executing order {order.order_no}")
                return False
                
            # Deduct cash
            account.current_cash = float(Decimal(str(account.current_cash)) - cash_needed)
            
            # Update position
            position = (
                db.query(Position)
                .filter(Position.account_id == account.id, Position.symbol == order.symbol, Position.market == order.market)
                .first()
            )
            
            if not position:
                position = Position(
                    version="v1",
                    account_id=account.id,
                    symbol=order.symbol,
                    name=order.name,
                    market=order.market,
                    quantity=0,
                    available_quantity=0,
                    avg_cost=0,
                    leverage=1,
                )
                db.add(position)
                db.flush()
            
            # Calculate new average cost and leverage (use Decimal for precision)
            old_qty = Decimal(str(position.quantity))
            old_cost = Decimal(str(position.avg_cost))
            old_leverage = Decimal(str(position.leverage))

            new_qty = old_qty + quantity
            
            if old_qty == 0:
                new_avg_cost = execution_price
                new_leverage = leverage
            else:
                old_notional = old_cost * old_qty
                new_notional = notional + old_notional
                new_avg_cost = new_notional / new_qty
                # Update leverage (weighted average)
                new_leverage = (old_notional * old_leverage + notional * leverage) / new_notional

            position.quantity = float(new_qty)
            position.available_quantity = float(Decimal(str(position.available_quantity)) + quantity)
            position.avg_cost = float(new_avg_cost)
            position.leverage = int(new_leverage)

        else:  # SELL
            # Check position
            position = (
                db.query(Position)
                .filter(Position.account_id == account.id, Position.symbol == order.symbol, Position.market == order.market)
                .first()
            )

            if not position or Decimal(str(position.available_quantity)) < quantity:
                logger.warning(f"Insufficient position when executing order {order.order_no}")
                return False

            # Reduce position (use Decimal for precision)
            position.quantity = float(Decimal(str(position.quantity)) - quantity)
            position.available_quantity = float(Decimal(str(position.available_quantity)) - quantity)
            
            # PnL and cash gain calculation for leveraged positions
            sell_notional = notional
            commission = _calc_commission(sell_notional)
            position_leverage = Decimal(str(position.leverage))
            
            if position_leverage > 1:
                # 杠杆仓位卖出，需要计算 PnL
                entry_price = Decimal(str(position.avg_cost))
                pnl = (execution_price - entry_price) * quantity
                
                # 释放的保证金
                initial_margin_part = (entry_price * quantity) / position_leverage
                
                cash_gain = initial_margin_part + pnl - commission
            else:
                # 现货卖出
                cash_gain = sell_notional - commission

            account.current_cash = float(Decimal(str(account.current_cash)) + cash_gain)
        
        # Create trade record
        trade = Trade(
            order_id=order.id,
            account_id=account.id,
            symbol=order.symbol,
            name=order.name,
            market=order.market,
            side=order.side,
            price=float(execution_price),
            quantity=float(quantity),
            commission=float(commission),
        )
        db.add(trade)

        # Release frozen (BUY)
        _release_frozen_on_fill(account, order, execution_price, commission)
        
        # Update order status
        order.filled_quantity = float(quantity)
        order.status = "FILLED"
        
        db.commit()
        
        logger.info(f"Order {order.order_no} executed: {order.side} {quantity} {order.symbol} @ ${execution_price}")
        return True
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error executing order {order.order_no}: {e}")
        return False


def get_pending_orders(db: Session, account_id: Optional[int] = None) -> list[Order]:
    """
    Get pending orders

    Args:
        db: Database session
        account_id: Account ID, when None get all accounts' pending orders

    Returns:
        List of pending orders
    """
    query = db.query(Order).filter(Order.status == "PENDING")
    
    if account_id is not None:
        query = query.filter(Order.account_id == account_id)
    
    return query.order_by(Order.created_at).all()


def _release_frozen_on_cancel(account: Account, order: Order):
    """Release frozen on order cancel (BUY only)"""
    if order.side == "BUY":
        # Conservative release: estimate frozen amount based on order price, avoid getting market price
        ref_price = float(order.price or 0.0)
        if ref_price <= 0:
            # If no order price (theoretically shouldn't happen), use conservative estimate
            logger.warning(f"Order {order.order_no} has no order price, unable to accurately release frozen funds")
            ref_price = 100.0  # Use default value

        notional = Decimal(str(ref_price)) * Decimal(order.quantity)
        commission = _calc_commission(notional)
        release_amt = notional + commission
        account.frozen_cash = float(max(Decimal(str(account.frozen_cash)) - release_amt, Decimal('0')))


def cancel_order(db: Session, order: Order, reason: str = "User cancelled") -> bool:
    """
    Cancel order

    Args:
        db: Database session
        order: Order object
        reason: Cancel reason

    Returns:
        Whether cancellation was successful
    """
    if order.status != "PENDING":
        return False
    
    try:
        order.status = "CANCELLED"
        # Release frozen
        account = db.query(Account).filter(Account.id == order.account_id).first()
        if account:
            _release_frozen_on_cancel(account, order)
        db.commit()
        
        logger.info(f"Order {order.order_no} cancelled: {reason}")
        return True
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error cancelling order {order.order_no}: {e}")
        return False


def process_all_pending_orders(db: Session) -> Tuple[int, int]:
    """
    Process all pending orders

    Args:
        db: Database session

    Returns:
        (Executed orders count, Total checked orders)
    """
    pending_orders = get_pending_orders(db)
    executed_count = 0
    
    for order in pending_orders:
        if check_and_execute_order(db, order):
            executed_count += 1
    
    logger.info(f"Processing pending orders: checked {len(pending_orders)} orders, executed {executed_count} orders")
    return executed_count, len(pending_orders)