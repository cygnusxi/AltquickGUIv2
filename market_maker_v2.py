import os
import time
import random
import hmac
import hashlib
import requests
from urllib.parse import urlencode
from datetime import datetime, timezone
from typing import Dict, Tuple
try:
    from config import api_key, api_secret
except ImportError:
    api_key = os.environ.get('ALTQUICK_API_KEY', '')
    api_secret = os.environ.get('ALTQUICK_API_SECRET', '')
import webbrowser
import threading
from flask import Flask, render_template, jsonify
import subprocess
import platform
import json
import traceback

class MarketMaker:
    def __init__(self):
        self.base_url = 'https://altquick.com/api/v1'
        self.symbol = 'BTC_CURE'
        self.min_spread = 0.41  # 1% minimum spread
        self.max_spread = 0.65  # 5% maximum spread
        self.order_size_range = (2, 6)  # min/max order size
        self.current_inventory = 0
        # Avellaneda-Stoikov Parameters
        self.target_cure_position = 100000  # The ideal amount of CURE the bot wants to hold
        self.gamma = 0.05  # Risk aversion parameter (how aggressively it shifts prices)
        self.session_orders: Dict[str, dict] = {}  # Track only orders created in this session
        
        # Add new configuration parameters
        self.max_position = 50  # Maximum position size
        self.target_position = 0  # Target neutral position
        self.risk_factor = 0.5  # Adjusts spread based on position
        self.min_order_interval = 5  # Minimum seconds between orders
        self.last_order_time = 0
        self.position_threshold = 0.8  # 80% of max position triggers rebalancing

        # CureCoin market-focus tuning (reservation skew + grid spacing)
        self.market_support_factor = 0.08  # Bid-side support when book is ask-heavy
        self.wide_spread_threshold_pct = 3.0
        self.wide_spread_support_factor = 0.02  # Lift bids when spread is unhealthy

        # 24/7 unattended operation
        self.consecutive_api_errors = 0
        self.max_api_backoff_sec = 300
        self.last_successful_cycle = time.time()
        self.paused = False
        
        # Add logging configuration
        self.setup_logging()

        if not api_key or not api_secret:
            raise RuntimeError(
                "Missing API credentials. Copy config example.py to config.py or set "
                "ALTQUICK_API_KEY and ALTQUICK_API_SECRET environment variables."
            )

        print("Starting new market making session...")

        # Add Flask app
        self.app = Flask(__name__)
        self.setup_routes()
        self.market_data = {
            'mid_price': 0,
            'volatility': 0,
            'current_orders': [],
            'inventory': self.current_inventory,
            'last_update': None,
            # NEW UI FIELDS:
            'orderbook': {},
            'recent_trades': [],
            'klines': [],
            'ticker_24hr': {},
            'market_focus': {},
            'health': {'status': 'starting', 'consecutive_errors': 0},
        }
        
        # Start web server in a separate thread
        self.start_web_server()
        
        # Start the UI data collector thread
        self.ui_thread = threading.Thread(target=self.ui_data_collector_loop, daemon=True)
        self.ui_thread.start()

        # Add new tracking for all orders (must exist before load/save on disk)
        self.state_file = 'bot_state.json'
        self.baseline_btc = None
        self.permanent_bids = set()
        self.all_orders = self.load_orders_from_file()
        self.load_existing_orders()  # Update with fresh data from exchange

    def assess_market_conditions(self) -> dict:
        """Snapshot BTC_CURE book health for CureCoin-focused quoting."""
        orderbook = self.market_data.get('orderbook', {})
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        best_bid = float(bids[0]['price']) if bids else 0.0
        best_ask = float(asks[0]['price']) if asks else 0.0
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid else 0.0

        def depth(levels, n=10):
            total = 0.0
            for level in levels[:n]:
                total += float(level.get('quantity', level.get('amount', 0)))
            return total

        bid_depth = depth(bids)
        ask_depth = depth(asks)
        depth_total = bid_depth + ask_depth
        depth_imbalance = (bid_depth - ask_depth) / depth_total if depth_total > 0 else 0.0

        inventory_ratio = self.current_inventory / max(self.target_cure_position, 1.0)

        return {
            'mid_price': mid,
            'spread_pct': spread_pct,
            'bid_depth': bid_depth,
            'ask_depth': ask_depth,
            'depth_imbalance': depth_imbalance,
            'inventory_ratio': inventory_ratio,
        }

    def calculate_grid_increment(self, conditions: dict) -> float:
        """Tighter grid when spread is wide; slightly wider when book is already tight."""
        base_increment = 0.01
        spread_pct = conditions.get('spread_pct', 0.0)
        if spread_pct >= self.wide_spread_threshold_pct:
            return max(0.006, base_increment * 0.75)
        if spread_pct <= 1.0 and spread_pct > 0:
            return min(0.015, base_increment * 1.15)
        return base_increment

    def record_api_success(self):
        self.consecutive_api_errors = 0
        self.last_successful_cycle = time.time()
        self.market_data['health'] = {
            'status': 'ok',
            'consecutive_errors': 0,
            'last_successful_cycle': datetime.now(timezone.utc).isoformat(),
            'paused': self.paused,
        }

    def record_api_failure(self, context: str):
        self.consecutive_api_errors += 1
        delay = min(
            self.max_api_backoff_sec,
            5 * (2 ** min(self.consecutive_api_errors - 1, 6)),
        )
        status = 'degraded' if self.consecutive_api_errors < 10 else 'critical'
        self.market_data['health'] = {
            'status': status,
            'consecutive_errors': self.consecutive_api_errors,
            'last_error_context': context,
            'next_retry_delay_sec': delay,
            'paused': self.paused,
        }
        self.logger.warning(
            f"API failure ({context}). consecutive={self.consecutive_api_errors}, "
            f"backing off {delay}s"
        )
        time.sleep(delay)

    def calculate_reservation_price(self, mid_price: float, volatility: float) -> float:
        """
        CureCoin-focused Avellaneda-Stoikov reservation price.
        Centers the grid to rebuild CURE inventory, support a thin bid book, and
        avoid dumping when the ecosystem is already ask-heavy.
        """
        conditions = self.assess_market_conditions()
        self.market_data['market_focus'] = conditions

        inventory_delta = self.current_inventory - self.target_cure_position
        normalized_q = inventory_delta / max(self.target_cure_position, 1.0)

        safe_volatility = max(volatility, 0.01)

        # Inventory skew in price space (classic A-S shape, scaled to CURE holdings)
        inventory_skew = normalized_q * self.gamma * (safe_volatility ** 2) * mid_price

        # Ecosystem support: lift quotes when bids are thin vs asks (negative imbalance)
        support_skew = conditions['depth_imbalance'] * mid_price * self.market_support_factor

        if conditions['spread_pct'] >= self.wide_spread_threshold_pct:
            support_skew -= mid_price * self.wide_spread_support_factor

        # When heavily long CURE, resist pushing the floor down further
        if conditions['inventory_ratio'] > 1.25 and conditions['depth_imbalance'] < 0:
            support_skew += mid_price * 0.03

        raw_skew = inventory_skew + support_skew

        max_skew = mid_price * 0.25
        clamped_skew = max(min(raw_skew, max_skew), -max_skew)

        reservation_price = mid_price - clamped_skew
        reservation_price = max(reservation_price, 1e-8)

        self.logger.info(
            "CURE focus -> Mid: %.8f | Res: %.8f | InvSkew: %.8f | SupportSkew: %.8f | "
            "Spread: %.2f%% | DepthImb: %.3f | InvRatio: %.2f",
            mid_price,
            reservation_price,
            inventory_skew,
            support_skew,
            conditions['spread_pct'],
            conditions['depth_imbalance'],
            conditions['inventory_ratio'],
        )

        return reservation_price

    def get_recent_trades(self, limit=100):
        try:
            response = requests.get(f"{self.base_url}/trades", params={'market': self.symbol, 'limit': limit})
            if response.ok: return response.json()
        except Exception as e:
            self.logger.error(f"Error fetching trades: {e}")
        return []

    def get_klines(self, interval='1h', limit=100):
        try:
            response = requests.get(f"{self.base_url}/klines", params={'market': self.symbol, 'interval': interval, 'limit': limit})
            if response.ok: return response.json()
        except Exception as e:
            self.logger.error(f"Error fetching klines: {e}")
        return []

    def ui_data_collector_loop(self):
        """Runs in background to fetch heavy data without blocking the trading bot"""
        while True:
            try:
                self.market_data['recent_trades'] = self.get_recent_trades()
                self.market_data['klines'] = self.get_klines()
                time.sleep(10) # UI data can update a bit slower
            except Exception as e:
                self.logger.error(f"UI data loop error: {e}")
                time.sleep(10)    

    def setup_logging(self):
        """Configure enhanced logging for the market maker"""
        import logging
        import collections
        
        # --- NEW CONSOLE LOG QUEUE ---
        self.log_queue = collections.deque(maxlen=30)
        
        class MemoryHandler(logging.Handler):
            def __init__(self, queue):
                super().__init__()
                self.queue = queue
            def emit(self, record):
                self.queue.append(self.format(record))
                
        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        
        # Setup file handler
        file_handler = logging.FileHandler('market_maker.log')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        
        # Setup console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        
        # Setup memory handler for the web UI
        memory_handler = MemoryHandler(self.log_queue)
        memory_handler.setLevel(logging.INFO)
        memory_handler.setFormatter(formatter)
        
        # Setup logger
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        self.logger.addHandler(memory_handler) # Attach it here
        
        self.logger.info("Market Maker bot initialized")

    def get_market_data(self) -> Tuple[float, float]:
        """Fetch current market data and return mid price and volatility"""
        try:
            # Get order book data
            response = requests.get(f"{self.base_url}/depth", params={
                'market': self.symbol,
                'limit': 100
            })
            order_book = response.json()
            
            # SAFE GET: Prevents KeyError if the API returns an error dict
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            
            # Get 24hr ticker data
            ticker_response = requests.get(f"{self.base_url}/ticker/24hr", params={
                'market': self.symbol
            })
            ticker = ticker_response.json()
            self.market_data['orderbook'] = order_book
            self.market_data['ticker_24hr'] = ticker

            # Calculate mid price from safely extracted order book
            best_bid = float(bids[0]['price']) if bids else 0
            best_ask = float(asks[0]['price']) if asks else 0
            mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
            
            # Calculate volatility from 24hr data
            price_change_percent = float(ticker.get('priceChangePercent', 0))
            volatility = abs(price_change_percent) / 100
            
            return mid_price, volatility
        except Exception as e:
            self.logger.error(f"Error fetching market data: {e}")
            self.record_api_failure('get_market_data')
            return None, None

    def calculate_order_prices(self, mid_price: float, volatility: float) -> Tuple[float, float]:
        """Calculate bid and ask prices with dynamic spread adjustment"""
        try:
            # Adjust spread based on volatility and position
            position_impact = abs(self.current_inventory / self.max_position) * self.risk_factor
            base_spread = max(min(volatility * 2, self.max_spread), self.min_spread)
            adjusted_spread = base_spread * (1 + position_impact)
            
            # Skew prices based on inventory
            skew = self.current_inventory / self.max_position * self.risk_factor
            
            # Calculate prices with skew
            bid_price = mid_price * (1 - adjusted_spread/2 - skew)
            ask_price = mid_price * (1 + adjusted_spread/2 - skew)
            
            return round(bid_price, 8), round(ask_price, 8)
        except Exception as e:
            self.logger.error(f"Error calculating prices: {e}")
            return None, None

    def place_order(self, side: str, price: float, quantity: float = None) -> dict:
        """Place a new order with enhanced logging and error handling"""
        try:
            # If quantity is not provided, use random size from range
            if quantity is None:
                quantity = random.uniform(*self.order_size_range)
            
            params = {
                'market': self.symbol,
                'side': side,
                'type': 'limit',
                'price': str(price),
                'quantity': str(quantity),
                'timestamp': int(time.time() * 1000),
                'recvWindow': 5000
            }
            
            # Create signature
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params['signature'] = signature
            headers = {'X-MBX-APIKEY': api_key}
            
            self.logger.info(f"Attempting to place {side} order: {quantity} units at {price}")
            
            response = requests.post(
                f"{self.base_url}/order",
                params=params,
                headers=headers,
                timeout=10
            )
            
            if response.ok:
                order_data = response.json()
                order_id = order_data.get('order_id')
                if order_id:
                    order_record = {
                        'id': order_id,
                        'side': side,
                        'price': price,
                        'quantity': quantity,
                        'status': order_data.get('order_state', 'OPEN'),
                        'remaining': order_data.get('remaining', str(quantity)),
                        'created_at': order_data.get('created_at'),
                    }
                    self.session_orders[order_id] = order_record
                    self.all_orders[order_id] = order_record
                    self.save_orders_to_file()
                    self.logger.info(f"Order placed successfully: {order_id}")
                    self.update_market_data(self.market_data['mid_price'], self.market_data['volatility'])
                    return order_data
                else:
                    self.logger.error(f"Order placed but no order ID received. Response: {order_data}")
            else:
                self.logger.error(f"Order placement failed: {response.status_code} - {response.text}")
            return None
            
        except Exception as e:
            self.logger.error(f"Error placing order: {str(e)}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order with enhanced logging"""
        try:
            self.logger.info(f"Attempting to cancel order: {order_id}")
            params = {
                'market': self.symbol,
                'orderId': order_id,
                'timestamp': int(time.time() * 1000),
                'recvWindow': 5000
            }
            
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params['signature'] = signature
            headers = {'X-MBX-APIKEY': api_key}
            
            response = requests.delete(f"{self.base_url}/order", params=params, headers=headers)
            
            if response.ok:
                self.session_orders.pop(order_id, None)
                if order_id in self.all_orders:
                    self.all_orders[order_id]['status'] = 'cancelled'
                    self.save_orders_to_file()
                self.logger.info(f"Order {order_id} cancelled successfully")
                # Update market data immediately after cancelling order
                self.update_market_data(self.market_data['mid_price'], self.market_data['volatility'])
                return True
            
            self.logger.error(f"Failed to cancel order {order_id}: {response.text}")
            return False
            
        except Exception as e:
            self.logger.error(f"Error cancelling order {order_id}: {str(e)}")
            return False

    def display_status(self, mid_price: float, volatility: float):
        """Display current bot status"""
        try:
            ticker_response = requests.get(f"{self.base_url}/ticker/24hr", params={
                'market': self.symbol
            })
            ticker = ticker_response.json()
            
            os.system('clear')
            print('=' * 50)
            print('CURE Market Maker Bot Status')
            print('=' * 50)
            print(f'Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
            print(f'Mid Price: {mid_price:.8f}')
            print(f'Volatility: {volatility:.4f}')
            print(f'24h Volume: {ticker["volume"]}')
            print(f'24h Price Change: {ticker["priceChangePercent"]}%')
            print('-' * 50)
            print('Session Orders:')
            print(f'Active Orders: {len(self.session_orders)}')
            print(f'Best Bid: {ticker["bidPrice"]}')
            print(f'Best Ask: {ticker["askPrice"]}')
            
            if self.session_orders:
                print('-' * 50)
                print('Current Orders:')
                for order_id, order in self.session_orders.items():
                    print(f"Order {order_id}: {order['side']} @ {order['price']}")
            print('=' * 50)
        except Exception as e:
            print(f"Error displaying status: {e}")

    def should_rebalance(self) -> bool:
        """Determine if position needs rebalancing"""
        return abs(self.current_inventory) > (self.max_position * self.position_threshold)

    def manage_position(self):
        """Manage inventory position"""
        if self.should_rebalance():
            self.logger.info(f"Rebalancing position. Current inventory: {self.current_inventory}")
            # Cancel all existing orders
            for order_id in list(self.session_orders.keys()):
                self.cancel_order(order_id)
            
            # Place rebalancing order
            side = 'sell' if self.current_inventory > 0 else 'buy'
            mid_price, _ = self.get_market_data()
            if mid_price:
                rebalance_size = abs(self.current_inventory - self.target_position)
                self.place_order(side, mid_price, rebalance_size)

    def setup_routes(self):
        @self.app.route('/')
        def home():
            return render_template('dashboard.html')
            
        @self.app.route('/api/status')
        def status():
            return jsonify(self.market_data)

        @self.app.route('/api/health')
        def health():
            return jsonify(self.market_data.get('health', {})), 200
    
    def start_web_server(self):
        def run_flask():
            self.app.run(host='127.0.0.1', port=5000, threaded=True, use_reloader=False)
            
        self.web_thread = threading.Thread(target=run_flask, daemon=True)
        self.web_thread.start()
        
        # Print instructions instead of auto-opening browser
        print("\nWeb interface is running at: http://127.0.0.1:5000")
        print("Please open this URL in your Windows browser\n")
    
    def minimize_window(self):
        """Skip window minimization in WSL environment"""
        if platform.system() == 'Windows':  # Only try to minimize in native Windows
            try:
                import win32gui, win32con
                hwnd = win32gui.GetForegroundWindow()
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            except:
                pass

    def update_market_data(self, mid_price: float, volatility: float):
        """Update market data for web display and sync internal balances"""
        try:
            # Get current balances directly from the exchange
            balances = self.get_account_balances()
            if not balances:
                self.logger.warning(
                    "Failed to fetch balances, skipping inventory sync to prevent skew errors."
                )
                return

            # --- NEW BALANCE SYNC LOGIC ---
            btc_free = 0.0
            cure_free = 0.0
            cure_locked = 0.0
            
            for bal in balances:
                if bal.get('asset') == 'BTC':
                    btc_free = float(bal.get('free', 0))
                elif bal.get('asset') == 'CURE':
                    cure_free = float(bal.get('free', 0))
                    cure_locked = float(bal.get('locked', 0))

            if self.baseline_btc is None and btc_free > 0:
                self.baseline_btc = btc_free
            
            # True inventory is your available CURE + CURE locked in active sell orders
            self.current_inventory = cure_free + cure_locked
            
            # Get active orders directly from all_orders
            active_orders = [
                {
                    'id': order['id'],
                    'side': order['side'],
                    'price': str(order['price']),
                    'quantity': str(order['quantity']),
                    'remaining': str(order['remaining']),
                    'status': order['status'],
                    'created_at': order['created_at']
                }
                for order in self.all_orders.values()
                if order['status'] in ['booked', 'partial']
            ]
            
            # Count orders by side
            buy_orders, sell_orders = self.count_orders_by_side()
            
            self.market_data.update({
                'mid_price': mid_price or 0,
                'volatility': volatility or 0,
                'current_orders': list(self.session_orders.values()),
                'all_active_orders': active_orders,
                'inventory': self.current_inventory,  # Total CURE for the A-S Skew math
                'btc_balance': btc_free,              # Free BTC for the UI
                'cure_free': cure_free,               # Free CURE for the UI
                'balances': balances,
                'buy_orders': buy_orders,
                'sell_orders': sell_orders,
                'last_update': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'recent_logs': list(self.log_queue) if hasattr(self, 'log_queue') else []
            })
            
        except Exception as e:
            self.logger.error(f"Error updating market data: {str(e)}")
            self.logger.error(f"Traceback: {traceback.format_exc()}")

    def check_order_status(self, order_id: str) -> dict:
        """Check the status of a specific order"""
        try:
            params = {
                'market': self.symbol,
                'orderId': order_id,
                'timestamp': int(time.time() * 1000),
                'recvWindow': 5000
            }
            
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params['signature'] = signature
            headers = {'X-MBX-APIKEY': api_key}
            
            response = requests.get(f"{self.base_url}/order", params=params, headers=headers)
            
            if response.ok:
                order_data = response.json()
                self.logger.debug(f"Order status response: {order_data}")
                return order_data
            else:
                self.logger.error(f"Failed to get order status: {response.text}")
                return None
            
        except Exception as e:
            self.logger.error(f"Error checking order status: {str(e)}")
            return None

    def update_order_statuses(self):
        """Update the status of all active orders tracked locally and on the exchange."""
        try:
            active_ids = [
                oid
                for oid, order in self.all_orders.items()
                if order.get('status') in ['booked', 'partial', 'OPEN', 'open']
            ]
            for order_id in active_ids:
                order_status = self.check_order_status(order_id)
                if not order_status:
                    continue
                state = order_status.get('order_state')
                if state in ['filled', 'cancelled', 'expired']:
                    self.logger.info(f"Order {order_id} is {state}, updating local state")
                    self.session_orders.pop(order_id, None)
                    if order_id in self.all_orders:
                        self.all_orders[order_id]['status'] = state
                    if state == 'filled' and order_id in self.permanent_bids:
                        self.permanent_bids.discard(order_id)
                elif order_id in self.all_orders:
                    self.all_orders[order_id].update({
                        'status': state,
                        'remaining': order_status.get('remaining'),
                    })
                    if order_id in self.session_orders:
                        self.session_orders[order_id].update({
                            'status': state,
                            'remaining': order_status.get('remaining'),
                        })
            if active_ids:
                self.save_orders_to_file()
        except Exception as e:
            self.logger.error(f"Error updating order statuses: {str(e)}")

    def count_orders_by_side(self):
        """Count active orders by side"""
        buy_orders = 0
        sell_orders = 0
        for order in self.all_orders.values():
            if order['status'] in ['booked', 'partial']:  # Only count active orders
                if order['side'] == 'buy':
                    buy_orders += 1
                elif order['side'] == 'sell':
                    sell_orders += 1
        return buy_orders, sell_orders

    def calculate_price_levels(
        self, mid_price: float, levels: int, is_buy: bool, price_increment: float = 0.01
    ) -> list:
        """Calculate price levels for spread with fixed increments and floor protections"""
        prices = []
        min_price_move = 1e-8   # 1 Satoshi minimum tick
        min_sell_price = 10 * 1e-8  # Hardcoded 10 Satoshi minimum for sells
        
        self.logger.info(f"Calculating {'buy' if is_buy else 'sell'} price levels:")
        self.logger.info(f"Starting from mid price: {mid_price:.8f}")
        
        for i in range(levels):
            base_price = prices[-1] if prices else mid_price

            if is_buy:
                current_price = base_price * (1 - price_increment)
                if abs(current_price - base_price) < min_price_move:
                    current_price = base_price - min_price_move
                
                # Enforce absolute floor
                current_price = max(current_price, min_price_move)
                current_price = round(current_price, 8)
                
                # If we hit 1 satoshi twice in a row, we stop adding levels
                if prices and current_price == prices[-1]:
                    self.logger.info(f"Hit 1 Satoshi floor. Stopping buy grid at {len(prices)} levels.")
                    break
            else:
                current_price = base_price * (1 + price_increment)
                if abs(current_price - base_price) < min_price_move:
                    current_price = base_price + min_price_move
                
                current_price = round(current_price, 8)
                
                # Enforce the hardcoded 10 Satoshi minimum sell price
                if current_price < min_sell_price:
                    current_price = min_sell_price
                    if prices and prices[-1] >= min_sell_price:
                        current_price = prices[-1] + min_price_move

            current_price = round(current_price, 8)
            
            # Final failsafe against duplicates (mostly for the sell side)
            if prices and current_price == prices[-1]:
                current_price += min_price_move if not is_buy else -min_price_move
                current_price = round(current_price, 8)

            prices.append(current_price)
            self.logger.info(f"Level {i+1}: {current_price:.8f}")
        
        self.logger.info(f"Generated {len(prices)} {'buy' if is_buy else 'sell'} price levels.")
        return prices

    def calculate_order_sizes(self, base_size: float, levels: int, is_buy: bool) -> list:
        """Calculate graduated order sizes for spread
        
        Args:
            base_size: The minimum order size
            levels: Number of price levels
            is_buy: True for buy orders, False for sell orders
            
        Returns:
            List of order sizes, increasing from base_size
        """
        size_increment = base_size * 0.3  # Each level increases by 20%
        sizes = []
        
        for i in range(levels):
            size = base_size + (size_increment * i)
            sizes.append(round(size, 8))
        
        # For buy orders, keep original order (larger sizes at lower prices)
        # For sell orders, keep original order (larger sizes at higher prices)
        return sizes

    def should_cancel_order(self, order: dict) -> bool:
        """Determine if an order should be cancelled"""
        try:
            if order['id'] in self.permanent_bids:
                return None

            # Check order age (12 hours)
            created_time = datetime.strptime(order['created_at'], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
            order_age = (datetime.now(timezone.utc) - created_time).total_seconds() / 3600
            if order_age > 12:
                return "Order is older than 12 hours"
            
            # Check if mid price has moved significantly
            current_mid = self.market_data.get('mid_price', 0)
            if current_mid > 0:
                order_price = float(order['price'])
                side = order['side']
                
                # For buy orders, cancel if mid price drops significantly below order price
                # For sell orders, cancel if mid price rises significantly above order price
                if side == 'buy' and current_mid < (order_price * 0.5):  # Mid price 50% below buy order
                    return f"Mid price ({current_mid:.8f}) has fallen 50% below buy order price ({order_price:.8f})"
                elif side == 'sell' and current_mid > (order_price * 1.5):  # Mid price 50% above sell order
                    return f"Mid price ({current_mid:.8f}) has risen 50% above sell order price ({order_price:.8f})"
            
            # Check for duplicate prices (keep this check tight)
            side = order['side']
            price = float(order['price'])
            for other_order in self.all_orders.values():
                if (other_order['id'] != order['id'] and 
                    other_order['side'] == side and 
                    other_order['status'] in ['booked', 'partial']):
                    other_price = float(other_order['price'])
                    if abs(other_price - price) < (price * 0.0001):  # 0.01% tolerance
                        return "Duplicate order found at same price level"
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error in should_cancel_order: {e}")
            return None

    def cleanup_orders(self):
        """Cancel orders that meet cancellation criteria"""
        try:
            for order_id, order in list(self.all_orders.items()):
                if order['status'] in ['booked', 'partial']:
                    cancel_reason = self.should_cancel_order(order)
                    if cancel_reason:
                        self.logger.info(f"Cancelling order {order_id}: {cancel_reason}")
                        self.cancel_order(order_id)
        except Exception as e:
            self.logger.error(f"Error in cleanup_orders: {e}")

    def sweep_profits(self):
        """Sweep a slice of BTC profit into a permanent 2-sat bid wall."""
        try:
            balances = self.get_account_balances()
            btc_free = 0.0
            if balances:
                for bal in balances:
                    if bal.get('asset') == 'BTC':
                        btc_free = float(bal.get('free', 0))
                        break
            if self.baseline_btc is None:
                return
            btc_profit = btc_free - self.baseline_btc
            if btc_profit <= 0.00005000:
                return
            sweep_amount = btc_profit * 0.20
            cure_qty = sweep_amount / 0.00000002
            if cure_qty < 30:
                return
            result = self.place_order('buy', 0.00000002, cure_qty)
            if result:
                oid = result.get('order_id')
                if oid:
                    self.permanent_bids.add(oid)
                    self.baseline_btc = btc_free - sweep_amount
                    self.save_orders_to_file()
        except Exception as e:
            self.logger.error(f"Error in sweep_profits: {e}")

    def manage_orders(self, mid_price: float, volatility: float):
        """Create and manage spread of orders using A-S Skew"""
        try:
            if mid_price is None or mid_price == 0:
                return

            current_time = time.time()
            if current_time - self.last_order_time < self.min_order_interval:
                return

            # Clean up existing orders
            self.cleanup_orders()

            # Fixed parameters
            spread_levels = 10  # Number of orders on each side
            base_size = 30.0    # Minimum order size
            
            # --- APPLY THE AVELLANEDA-STOIKOV SKEW ---
            conditions = self.assess_market_conditions()
            grid_increment = self.calculate_grid_increment(conditions)
            reservation_price = self.calculate_reservation_price(mid_price, volatility)
            
            # --- MAKER PROTECTION CLAMP ---
            # Never let the bot cross the actual orderbook spread
            orderbook = self.market_data.get('orderbook', {})
            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])
            best_bid = float(bids[0]['price']) if bids else None
            best_ask = float(asks[0]['price']) if asks else None
            
            if best_bid is not None and best_ask is not None:
                # Force the reservation price to sit safely inside the actual spread bounds
                reservation_price = min(reservation_price, best_ask)
                reservation_price = max(reservation_price, best_bid)

            # Calculate price levels using the clamped RESERVATION price
            buy_prices = self.calculate_price_levels(
                reservation_price, spread_levels, True, grid_increment
            )
            sell_prices = self.calculate_price_levels(
                reservation_price, spread_levels, False, grid_increment
            )
            
            if not buy_prices or not sell_prices:
                self.logger.error("Failed to generate valid price levels.")
                return
            
            # -- THE 1 SATOSHI BUY WALL AGGREGATION --
            
            # Generate the FULL intended sizes for buy orders (all 10 levels)
            full_buy_sizes = self.calculate_order_sizes(base_size, spread_levels, True)
            buy_sizes = []
            
            # If the price list was truncated because it hit the floor
            if len(buy_prices) < spread_levels:
                # We calculate how many levels got "cut off"
                cutoff_count = spread_levels - len(buy_prices) + 1
                self.logger.info(f"Aggregating remaining {cutoff_count} buy levels into a 1 Satoshi Wall.")
                
                # Take normal sizes up to the second-to-last price
                buy_sizes = full_buy_sizes[:len(buy_prices)-1]
                
                # Sum the rest of the volume into the final 1 satoshi order
                remaining_volume = sum(full_buy_sizes[len(buy_prices)-1:])
                buy_sizes.append(round(remaining_volume, 8))
            else:
                # If we didn't hit the floor, just use the sizes normally
                buy_sizes = full_buy_sizes[:len(buy_prices)]
            
            # Sell sizes are generated normally based on actual length
            sell_sizes = self.calculate_order_sizes(base_size, len(sell_prices), False)
            
            # Debug logging
            self.logger.info(f"Current mid price: {mid_price:.8f}")
            self.logger.info("Buy order spread:")
            for i, (price, size) in enumerate(zip(buy_prices, buy_sizes)):
                self.logger.info(f"  Level {i+1}: {price:.8f} @ {size:.8f}")
            self.logger.info("Sell order spread:")
            for i, (price, size) in enumerate(zip(sell_prices, sell_sizes)):
                self.logger.info(f"  Level {i+1}: {price:.8f} @ {size:.8f}")
            
            orders_placed = False
            
            # Place buy orders
            for i, (price, size) in enumerate(zip(buy_prices, buy_sizes)):
                if not self.has_active_orders_at_price('buy', price):
                    if best_ask is not None and price >= best_ask:
                        self.logger.warning(
                            f"Skipping buy at {price:.8f} — would cross or touch best ask {best_ask:.8f}"
                        )
                        continue
                    self.logger.info(f"Placing buy order {i+1}/{spread_levels}: {size:.8f} units at {price:.8f}")
                    if self.place_order('buy', price, size):
                        orders_placed = True
                        time.sleep(0.5)
                else:
                    self.logger.debug(f"Skipping buy order {i+1}/{spread_levels} at {price:.8f} - already exists")
            
            # Place sell orders
            for i, (price, size) in enumerate(zip(sell_prices, sell_sizes)):
                if not self.has_active_orders_at_price('sell', price):
                    if best_bid is not None and price <= best_bid:
                        continue
                    self.logger.info(f"Placing sell order {i+1}/{spread_levels}: {size:.8f} units at {price:.8f}")
                    if self.place_order('sell', price, size):
                        orders_placed = True
                        time.sleep(0.5)
                else:
                    self.logger.debug(f"Skipping sell order {i+1}/{spread_levels} at {price:.8f} - already exists")

            if orders_placed:
                self.last_order_time = current_time

        except Exception as e:
            self.logger.error(f"Error in manage_orders: {str(e)}")
            self.logger.error(traceback.format_exc())
        finally:
            self.sweep_profits()

    def run(self):
        """Enhanced main market making loop with balanced order management"""
        self.logger.info("Starting market maker bot...")
        self.logger.info("Web interface available at: http://127.0.0.1:5000")
        
        if platform.system() == 'Windows':
            for i in range(5, 0, -1):
                print(f"Starting in {i} seconds...")
                time.sleep(1)
            self.minimize_window()
        
        while True:
            try:
                if self.paused:
                    time.sleep(5)
                    continue

                # Get market data
                mid_price, volatility = self.get_market_data()
                if mid_price is None or mid_price == 0:
                    self.logger.warning("Invalid market data received, retrying...")
                    continue

                self.record_api_success()

                # Sync fills and open orders from the exchange
                self.update_order_statuses()
                self.load_existing_orders()

                # Update web interface data
                self.update_market_data(mid_price, volatility)
                
                # Log current state
                buy_orders, sell_orders = self.count_orders_by_side()
                self.logger.info(f"Current state - Buy Orders: {buy_orders}, Sell Orders: {sell_orders}, "
                               f"Mid Price: {mid_price}, Volatility: {volatility}, "
                               f"Inventory: {self.current_inventory}")
                
                # Check if enough time has passed since last order
                current_time = time.time()
                if current_time - self.last_order_time < self.min_order_interval:
                    time.sleep(1)
                    continue

                # Manage order balance
                self.manage_orders(mid_price, volatility)

                time.sleep(5)

            except Exception as e:
                self.logger.error(f"Error in main loop: {e}")
                self.logger.error(traceback.format_exc())
                self.record_api_failure('main_loop')

    def get_account_balances(self) -> dict:
        """Fetch current account balances"""
        try:
            params = {
                'timestamp': int(time.time() * 1000),
                'recvWindow': 5000
            }
            
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params['signature'] = signature
            headers = {'X-MBX-APIKEY': api_key}
            
            response = requests.get(f"{self.base_url}/account", params=params, headers=headers)
            
            if response.ok:
                account_data = response.json()
                self.logger.debug(f"Account data received: {account_data}")
                return account_data.get('balances', [])
            else:
                self.logger.error(f"Failed to get account data: {response.text}")
                return []
            
        except Exception as e:
            self.logger.error(f"Error fetching account balances: {str(e)}")
            return []

    def load_orders_from_file(self):
        """Load bot state (orders, baseline, permanent bids) from local file"""
        try:
            with open(self.state_file, 'r') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            self.baseline_btc = data.get('baseline_btc')
            self.permanent_bids = set(data.get('permanent_bids', []))
            return data.get('orders', {})
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.logger.error(f"Error loading state file: {e}")
            return {}

    def save_orders_to_file(self):
        """Persist full bot state to local file"""
        try:
            payload = {
                'baseline_btc': self.baseline_btc,
                'permanent_bids': list(self.permanent_bids),
                'orders': self.all_orders,
            }
            with open(self.state_file, 'w') as f:
                json.dump(payload, f, indent=4)
        except Exception as e:
            self.logger.error(f"Error saving state file: {e}")

    def load_existing_orders(self):
        """Load open orders from the exchange"""
        try:
            params = {
                'market': self.symbol,
                'timestamp': int(time.time() * 1000),
                'recvWindow': 5000
            }
            
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            params['signature'] = signature
            headers = {'X-MBX-APIKEY': api_key}
            
            response = requests.get(f"{self.base_url}/openOrders", params=params, headers=headers)
            
            if response.ok:
                orders = response.json()
                # Clear existing active orders but keep history
                self.all_orders = {k: v for k, v in self.all_orders.items() 
                                 if v['status'] not in ['booked', 'partial']}
                
                for order in orders:
                    order_id = order['order_id']
                    self.all_orders[order_id] = {
                        'id': order_id,
                        'side': order['side'],
                        'price': float(order['price']),
                        'quantity': float(order['quantity']),
                        'remaining': float(order['remaining']),
                        'status': order['order_state'],
                        'created_at': order['created_at']
                    }
                self.save_orders_to_file()  # Save updated orders
                self.logger.info(f"Loaded {len(orders)} open orders from exchange")
            else:
                self.logger.error(f"Failed to load open orders: {response.text}")
                
        except Exception as e:
            self.logger.error(f"Error loading open orders: {str(e)}")
            self.logger.error(traceback.format_exc())

    def has_active_orders_at_price(self, side: str, price: float, tolerance: float = 0.0001) -> bool:
        """Check if there are active orders at the given price with tighter tolerance"""
        for order in self.all_orders.values():
            if (order['side'] == side and 
                order['status'] in ['booked', 'partial']):
                order_price = float(order['price'])
                price_diff = abs(order_price - price) / price
                if price_diff < tolerance:
                    self.logger.debug(f"Found existing {side} order at price {order_price} (checking {price})")
                    return True
        return False

if __name__ == "__main__":
    try:
        market_maker = MarketMaker()
        market_maker.run()
    except KeyboardInterrupt:
        print("\nShutdown requested (Ctrl+C). Exiting cleanly.")