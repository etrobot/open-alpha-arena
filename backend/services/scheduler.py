"""
Scheduled task scheduler service
Used to manage WebSocket snapshot updates and other scheduled tasks
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Dict, Set, Callable, Optional, List
import logging
from datetime import date, datetime

from database.connection import SessionLocal
from database.models import Position, CryptoPrice, Account, Order
from decimal import Decimal

logger = logging.getLogger(__name__)


class TaskScheduler:
    """Unified task scheduler"""
    
    def __init__(self):
        self.scheduler: Optional[BackgroundScheduler] = None
        self._started = False
        self._account_connections: Dict[int, Set] = {}  # track account connections
        
    def start(self):
        """Start the scheduler"""
        if not self._started:
            self.scheduler = BackgroundScheduler()
            self.scheduler.start()
            self._started = True
            logger.info("Scheduler started")
    
    def shutdown(self):
        """Shutdown the scheduler"""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
            self._started = False
            logger.info("Scheduler shutdown")
    
    def is_running(self) -> bool:
        """Check if scheduler is running"""
        return self._started and self.scheduler and self.scheduler.running
    
    def add_account_snapshot_task(self, account_id: int, interval_seconds: int = 10):
        """
        Add snapshot update task for account

        Args:
            account_id: Account ID
            interval_seconds: Update interval (seconds), default 10 seconds
        """
        if not self.is_running():
            self.start()
            
        job_id = f"snapshot_account_{account_id}"
        
        # Check if task already exists
        if self.scheduler.get_job(job_id):
            logger.debug(f"Snapshot task for account {account_id} already exists")
            return
        
        self.scheduler.add_job(
            func=self._execute_account_snapshot,
            trigger=IntervalTrigger(seconds=interval_seconds),
            args=[account_id],
            id=job_id,
            replace_existing=True,
            max_instances=1,  # Avoid duplicate execution
            coalesce=True,    # Combine missed executions into one
            misfire_grace_time=5  # Allow 5 seconds grace time for late execution
        )
        
        logger.info(f"Added snapshot task for account {account_id}, interval {interval_seconds} seconds")
    
    def add_margin_monitor_task(self, interval_seconds: int = 5):
        """
        Add margin monitoring task to watch for liquidation conditions
        Checks all leveraged positions and force closes if margin is insufficient
        
        Args:
            interval_seconds: Check interval (seconds), default 5 seconds
        """
        if not self.is_running():
            self.start()
        
        job_id = "margin_monitor"
        
        # Check if task already exists
        if self.scheduler.get_job(job_id):
            logger.debug("Margin monitor task already exists")
            return
        
        self.scheduler.add_job(
            func=self._check_margin_levels,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=2
        )
        
        logger.info(f"Added margin monitor task, checking every {interval_seconds} seconds")
    
    def remove_account_snapshot_task(self, account_id: int):
        """
        Remove snapshot update task for account

        Args:
            account_id: Account ID
        """
        if not self.scheduler:
            return
            
        job_id = f"snapshot_account_{account_id}"
        
        try:
            self.scheduler.remove_job(job_id)
            logger.info(f"Removed snapshot task for account {account_id}")
        except Exception as e:
            logger.debug(f"Failed to remove snapshot task for account {account_id}: {e}")
    
    
    def add_interval_task(self, task_func: Callable, interval_seconds: int, task_id: str, *args, **kwargs):
        """
        Add interval execution task

        Args:
            task_func: Function to execute
            interval_seconds: Execution interval (seconds)
            task_id: Task unique identifier
            *args, **kwargs: Parameters passed to task_func
        """
        if not self.is_running():
            self.start()
            
        self.scheduler.add_job(
            func=task_func,
            trigger=IntervalTrigger(seconds=interval_seconds),
            args=args,
            kwargs=kwargs,
            id=task_id,
            replace_existing=True
        )
        
        logger.info(f"Added interval task {task_id}: Execute every {interval_seconds} seconds")
    
    def remove_task(self, task_id: str):
        """
        Remove specified task

        Args:
            task_id: Task ID
        """
        if not self.scheduler:
            return
            
        try:
            self.scheduler.remove_job(task_id)
            logger.info(f"Removed task: {task_id}")
        except Exception as e:
            logger.debug(f"Failed to remove task {task_id}: {e}")

    def get_job_info(self) -> list:
        """Get all task information"""
        if not self.scheduler:
            return []

        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'next_run_time': job.next_run_time,
                'func_name': job.func.__name__ if hasattr(job.func, '__name__') else str(job.func)
            })
        return jobs

    async def _execute_account_snapshot(self, account_id: int):
        """
        Internal method to execute account snapshot update

        Args:
            account_id: Account ID
        """
        start_time = datetime.now()
        try:
            # Dynamic import to avoid circular dependency
            from api.ws import manager, _send_snapshot_optimized

            # Check if account still has active connections
            if account_id not in manager.active_connections:
                # Account disconnected, remove task
                self.remove_account_snapshot_task(account_id)
                return

            # Execute optimized snapshot update
            db: Session = SessionLocal()
            try:
                # Send optimized snapshot update (reduced frequency for expensive data)
                # Note: For now, skip the async WebSocket update in sync scheduler context
                # This can be enhanced later to properly handle async operations
                logger.debug(f"Skipping WebSocket snapshot update for account {account_id} in sync context")

                # Save latest prices for account's positions (less frequently)
                if start_time.second % 30 == 0:  # Only every 30 seconds
                    self._save_position_prices(db, account_id)

            finally:
                db.close()

        except Exception as e:
            logger.error(f"Account {account_id} snapshot update failed: {e}")
        finally:
            execution_time = (datetime.now() - start_time).total_seconds()
            if execution_time > 5:  # Log if execution takes longer than 5 seconds
                logger.warning(f"Slow snapshot execution for account {account_id}: {execution_time:.2f}s")
    
    def _save_position_prices(self, db: Session, account_id: int):
        """
        Save latest prices for account's positions on the current date

        Args:
            db: Database session
            account_id: Account ID
        """
        try:
            # Get all account's positions
            positions = db.query(Position).filter(
                Position.account_id == account_id,
                Position.quantity > 0
            ).all()

            if not positions:
                logger.debug(f"Account {account_id} has no positions, skip price saving")
                return

            today = date.today()

            for position in positions:
                try:
                    # Check if crypto price already saved today
                    existing_price = db.query(CryptoPrice).filter(
                        CryptoPrice.symbol == position.symbol,
                        CryptoPrice.market == position.market,
                        CryptoPrice.price_date == today
                    ).first()

                    if existing_price:
                        logger.debug(f"crypto {position.symbol} price already exists for today, skip")
                        continue

                    # Get latest price
                    from services.market_data import get_last_price
                    current_price = get_last_price(position.symbol, position.market)

                    # Save price record
                    crypto_price = CryptoPrice(
                        symbol=position.symbol,
                        market=position.market,
                        price=current_price,
                        price_date=today
                    )

                    db.add(crypto_price)
                    db.commit()

                    logger.info(f"Saved crypto price: {position.symbol} {today} {current_price}")

                except Exception as e:
                    logger.error(f"Failed to save crypto {position.symbol} price: {e}")
                    db.rollback()
                    continue

        except Exception as e:
            logger.error(f"Failed to save account {account_id} position prices: {e}")
            db.rollback()
    
    def _check_margin_levels(self):
        """
        Check margin levels for all accounts with leveraged positions
        Force close positions if margin falls below maintenance level
        """
        db = SessionLocal()
        try:
            # Get all accounts with leveraged positions
            accounts_with_positions = (
                db.query(Account)
                .join(Position, Position.account_id == Account.id)
                .filter(
                    Position.quantity > 0,
                    Position.leverage > 1
                )
                .distinct()
                .all()
            )
            
            if not accounts_with_positions:
                return
            
            for account in accounts_with_positions:
                try:
                    self._check_account_margin(db, account)
                except Exception as e:
                    logger.error(f"Error checking margin for account {account.name} (ID: {account.id}): {e}")
            
            db.commit()
            
        except Exception as e:
            logger.error(f"Error in margin monitoring: {e}")
            db.rollback()
        finally:
            db.close()
    
    def _check_account_margin(self, db: Session, account: Account):
        """
        Check margin level for a specific account and liquidate if necessary
        
        Args:
            db: Database session
            account: Account to check
        """
        from services.market_data import get_last_price
        
        # Get all leveraged positions
        positions = db.query(Position).filter(
            Position.account_id == account.id,
            Position.quantity > 0,
            Position.leverage > 1
        ).all()
        
        if not positions:
            return
        
        # Calculate current equity (account value)
        total_position_value = Decimal(0)
        total_pnl = Decimal(0)
        
        for position in positions:
            try:
                # Get current market price
                current_price = get_last_price(position.symbol, position.market)
                if not current_price or current_price <= 0:
                    logger.warning(f"Invalid price for {position.symbol}, skipping margin check")
                    continue
                
                current_price = Decimal(str(current_price))
                quantity = Decimal(str(position.quantity))
                avg_cost = Decimal(str(position.avg_cost))
                
                # Calculate position value
                position_value = quantity * current_price
                
                # Calculate PnL based on position side
                if position.side == "LONG":
                    # Long: profit when price goes up
                    pnl = quantity * (current_price - avg_cost)
                elif position.side == "SHORT":
                    # Short: profit when price goes down
                    pnl = quantity * (avg_cost - current_price)
                else:
                    # Default to LONG if side not specified
                    pnl = quantity * (current_price - avg_cost)
                
                total_position_value += position_value
                total_pnl += pnl
                
            except Exception as e:
                logger.error(f"Error calculating PnL for {position.symbol}: {e}")
                continue
        
        # Calculate equity: cash + unrealized PnL
        equity = Decimal(str(account.current_cash)) + total_pnl
        margin_used = Decimal(str(account.margin_used))
        
        if margin_used <= 0:
            # No margin used, nothing to check
            return
        
        # Calculate margin level (equity / margin_used)
        margin_level = equity / margin_used
        maintenance_margin_ratio = Decimal(str(account.maintenance_margin_ratio))
        
        logger.debug(
            f"Account {account.name}: equity=${equity:.2f}, margin_used=${margin_used:.2f}, "
            f"margin_level={margin_level:.2%}, maintenance_required={maintenance_margin_ratio:.2%}"
        )
        
        # Check if margin level is below maintenance requirement
        if margin_level < maintenance_margin_ratio:
            logger.warning(
                f"⚠️ MARGIN CALL! Account {account.name} margin level {margin_level:.2%} "
                f"below maintenance {maintenance_margin_ratio:.2%}. Liquidating positions..."
            )
            
            # Force close all leveraged positions
            self._liquidate_positions(db, account, positions, reason="Insufficient margin")
    
    def _liquidate_positions(self, db: Session, account: Account, positions: List[Position], reason: str):
        """
        Force close (liquidate) all positions for an account
        
        Args:
            db: Database session
            account: Account being liquidated
            positions: List of positions to liquidate
            reason: Reason for liquidation
        """
        from services.order_matching import check_and_execute_order
        import uuid
        
        for position in positions:
            try:
                if float(position.quantity) <= 0:
                    continue
                
                # Determine close side: SELL closes LONG, BUY closes SHORT
                close_side = "SELL" if position.side == "LONG" else "BUY"
                
                # Create liquidation order
                order_no = f"LIQ-{uuid.uuid4().hex[:16].upper()}"
                
                order = Order(
                    account_id=account.id,
                    order_no=order_no,
                    symbol=position.symbol,
                    name=position.name,
                    market=position.market,
                    side=close_side,
                    order_type="MARKET",
                    price=None,  # Market order
                    quantity=float(position.quantity),
                    leverage=1,  # Closing orders don't use leverage
                    filled_quantity=0,
                    status="PENDING"
                )
                
                db.add(order)
                db.flush()
                
                # Execute immediately
                executed = check_and_execute_order(db, order)
                
                if executed:
                    logger.warning(
                        f"🔴 LIQUIDATED: {account.name} {close_side} {position.quantity} {position.symbol} "
                        f"at market price. Reason: {reason}"
                    )
                else:
                    logger.error(f"Failed to execute liquidation order {order_no} for {position.symbol}")
                    
            except Exception as e:
                logger.error(f"Error liquidating position {position.symbol} for {account.name}: {e}")


# Global scheduler instance
task_scheduler = TaskScheduler()


# Convenience functions
def start_scheduler():
    """Start global scheduler"""
    task_scheduler.start()


def stop_scheduler():
    """Stop global scheduler"""
    task_scheduler.shutdown()


def add_account_snapshot_job(account_id: int, interval_seconds: int = 10):
    """Convenience function to add snapshot task for account"""
    task_scheduler.add_account_snapshot_task(account_id, interval_seconds)


def remove_account_snapshot_job(account_id: int):
    """Convenience function to remove account snapshot task"""
    task_scheduler.remove_account_snapshot_task(account_id)


def start_margin_monitor(interval_seconds: int = 5):
    """Start margin monitoring task"""
    task_scheduler.add_margin_monitor_task(interval_seconds)
    logger.info(f"Margin monitor started - checking every {interval_seconds} seconds")


# Legacy compatibility functions
def add_user_snapshot_job(user_id: int, interval_seconds: int = 10):
    """Legacy function - now redirects to account-based function"""
    # For backward compatibility, assume this is account_id
    add_account_snapshot_job(user_id, interval_seconds)


def remove_user_snapshot_job(user_id: int):
    """Legacy function - now redirects to account-based function"""
    # For backward compatibility, assume this is account_id
    remove_account_snapshot_job(user_id)


def setup_market_tasks():
    """Set up crypto market-related scheduled tasks"""
    # Crypto markets run 24/7, no specific market open/close times needed
    logger.info("Crypto markets run 24/7 - no market hours tasks needed")


def _ensure_market_data_ready() -> None:
    """Prefetch required market data before enabling trading tasks"""
    try:
        from services.trading_commands import AI_TRADING_SYMBOLS
        from services.market_data import get_last_price

        missing_symbols: List[str] = []

        for symbol in AI_TRADING_SYMBOLS:
            try:
                price = get_last_price(symbol, "CRYPTO")
                if price is None or price <= 0:
                    missing_symbols.append(symbol)
                    logger.warning(f"Prefetch returned invalid price for {symbol}: {price}")
                else:
                    logger.debug(f"Prefetched market data for {symbol}: {price}")
            except Exception as fetch_err:
                missing_symbols.append(symbol)
                logger.warning(f"Failed to prefetch price for {symbol}: {fetch_err}")

        if missing_symbols:
            raise RuntimeError(
                "Market data not ready for symbols: " + ", ".join(sorted(set(missing_symbols)))
            )

    except Exception as err:
        logger.error(f"Market data readiness check failed: {err}")
        raise


def reset_auto_trading_job():
    """Reset the auto trading job after account configuration changes"""
    try:
        # Import constants from auto_trader module
        from services.auto_trader import AI_TRADE_JOB_ID
        from services.trading_commands import place_ai_driven_crypto_order
        
        # Define interval (5 minutes)
        AI_TRADE_INTERVAL_SECONDS = 300
        
        # Ensure market data is ready before scheduling trading tasks
        _ensure_market_data_ready()

        # Ensure scheduler is started
        if not task_scheduler.is_running():
            task_scheduler.start()
            logger.info("Started scheduler for auto trading job reset")

        # Remove existing auto trading job if it exists
        if task_scheduler.scheduler and task_scheduler.scheduler.get_job(AI_TRADE_JOB_ID):
            task_scheduler.remove_task(AI_TRADE_JOB_ID)
            logger.info(f"Removed existing auto trading job: {AI_TRADE_JOB_ID}")
        
        # Re-add the auto trading job with updated configuration
        task_scheduler.add_interval_task(
            task_func=lambda: place_ai_driven_crypto_order(max_ratio=0.2),
            interval_seconds=AI_TRADE_INTERVAL_SECONDS,
            task_id=AI_TRADE_JOB_ID
        )
        
        # Trigger one immediate execution in background so API calls don't block
        import threading
        def _run_once():
            try:
                logger.info("Triggering immediate AI trade after account save/update")
                place_ai_driven_crypto_order(max_ratio=0.2)
            except Exception as run_err:
                logger.error(f"Immediate AI trade failed: {run_err}")
        threading.Thread(target=_run_once, name="ai_trade_run_once", daemon=True).start()

        # Log current jobs for verification
        jobs = task_scheduler.get_job_info()
        logger.info(f"Auto trading job reset successfully - interval: {AI_TRADE_INTERVAL_SECONDS}s; Jobs: {jobs}")
        
    except Exception as e:
        logger.error(f"Failed to reset auto trading job: {e}")
        raise