from decimal import Decimal
from sqlalchemy.orm import Session
from database.models import Position
from .market_data import get_last_price


def calc_positions_market_value(db: Session, account_id: int) -> float:
    """
    Calculate total equity in positions (for leveraged positions: margin + unrealized P&L).
    
    For leveraged positions, the equity is NOT the full market value (quantity * price),
    but rather the margin used plus unrealized profit/loss.
    
    Equity = Initial Margin + Unrealized P&L
           = (market_value / leverage) + (quantity * (current_price - avg_cost))

    Args:
        db: Database session
        account_id: Account ID

    Returns:
        Total equity in positions, returns 0 if price cannot be obtained
    """
    positions = db.query(Position).filter(Position.account_id == account_id).all()
    total = Decimal("0")
    
    for p in positions:
        try:
            price = Decimal(str(get_last_price(p.symbol, p.market)))
            quantity = Decimal(str(p.quantity))
            avg_cost = Decimal(str(p.avg_cost))
            leverage = Decimal(str(p.leverage)) if p.leverage and p.leverage > 0 else Decimal("1")
            
            # Market value of position
            market_value = quantity * price
            
            # For leveraged positions, only count margin + unrealized P&L, not full market value
            if leverage > 1:
                # Initial margin used
                initial_margin = market_value / leverage
                # Unrealized P&L
                unrealized_pnl = quantity * (price - avg_cost)
                # Position equity = margin + P&L
                position_equity = initial_margin + unrealized_pnl
            else:
                # Non-leveraged position: equity = market value
                position_equity = market_value
            
            total += position_equity
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Cannot get price for {p.symbol}.{p.market}, skipping position value calculation: {e}")
            continue
    
    return float(total)


def calc_positions_value(db: Session, account_id: int) -> float:
    """
    计算所有仓位的名义总价值 (sum(quantity * price * leverage))。
    
    WARNING: This returns NOTIONAL value (exposure), not equity!
    For calculating account assets/profit, use calc_positions_market_value() instead.

    Args:
        db: Database session
        account_id: Account ID

    Returns:
        Total notional value of positions, returns 0 if price cannot be obtained
    """
    positions = db.query(Position).filter(Position.account_id == account_id).all()
    total = Decimal("0")
    
    for p in positions:
        try:
            price = Decimal(str(get_last_price(p.symbol, p.market)))
            total += price * Decimal(str(p.quantity)) * Decimal(str(p.leverage))
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Cannot get price for {p.symbol}.{p.market}, skipping position value calculation: {e}")
            continue
    
    return float(total)
