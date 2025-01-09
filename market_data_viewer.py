import requests
import time
from flask import Flask, render_template, jsonify
from datetime import datetime
import threading

class MarketDataViewer:
    def __init__(self):
        self.base_url = 'https://altquick.com/api/v1'
        self.symbol = 'BTC_CURE'
        self.market_data = {
            'orderbook': {},
            'recent_trades': [],
            'klines': [],
            'ticker_24hr': {},
            'price_ticker': {},
            'book_ticker': {}
        }
        
        # Initialize Flask app
        self.app = Flask(__name__)
        self.setup_routes()
        
        # Start data collection thread
        self.running = True
        self.collector_thread = threading.Thread(target=self.data_collector_loop)
        self.collector_thread.daemon = True
        self.collector_thread.start()

    def setup_routes(self):
        @self.app.route('/')
        def home():
            return render_template('market_data.html')

        @self.app.route('/api/market-data')
        def get_market_data():
            return jsonify(self.market_data)

    def get_orderbook(self, limit=100):
        """Fetch order book data"""
        try:
            response = requests.get(f"{self.base_url}/depth", params={
                'market': self.symbol,
                'limit': limit
            })
            if response.ok:
                return response.json()
        except Exception as e:
            print(f"Error fetching orderbook: {e}")
        return None

    def get_recent_trades(self, limit=500):
        """Fetch recent trades"""
        try:
            response = requests.get(f"{self.base_url}/trades", params={
                'market': self.symbol,
                'limit': limit
            })
            if response.ok:
                return response.json()
        except Exception as e:
            print(f"Error fetching recent trades: {e}")
        return None

    def get_klines(self, interval='1h', limit=500):
        """Fetch kline/candlestick data"""
        try:
            response = requests.get(f"{self.base_url}/klines", params={
                'market': self.symbol,
                'interval': interval,
                'limit': limit
            })
            if response.ok:
                return response.json()
        except Exception as e:
            print(f"Error fetching klines: {e}")
        return None

    def get_24hr_ticker(self):
        """Fetch 24hr ticker statistics"""
        try:
            response = requests.get(f"{self.base_url}/ticker/24hr", params={
                'market': self.symbol
            })
            if response.ok:
                return response.json()
        except Exception as e:
            print(f"Error fetching 24hr ticker: {e}")
        return None

    def get_price_ticker(self):
        """Fetch current price ticker"""
        try:
            response = requests.get(f"{self.base_url}/ticker/price", params={
                'market': self.symbol
            })
            if response.ok:
                return response.json()
        except Exception as e:
            print(f"Error fetching price ticker: {e}")
        return None

    def get_book_ticker(self):
        """Fetch order book ticker"""
        try:
            response = requests.get(f"{self.base_url}/ticker/bookTicker", params={
                'market': self.symbol
            })
            if response.ok:
                return response.json()
        except Exception as e:
            print(f"Error fetching book ticker: {e}")
        return None

    def data_collector_loop(self):
        """Main loop to collect market data"""
        while self.running:
            try:
                # Update all market data
                self.market_data.update({
                    'orderbook': self.get_orderbook(),
                    'recent_trades': self.get_recent_trades(),
                    'klines': self.get_klines(),
                    'ticker_24hr': self.get_24hr_ticker(),
                    'price_ticker': self.get_price_ticker(),
                    'book_ticker': self.get_book_ticker(),
                    'last_update': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                time.sleep(5)  # Update every 5 seconds
            except Exception as e:
                print(f"Error in data collector loop: {e}")
                time.sleep(5)

    def run(self, host='127.0.0.1', port=5001):
        """Start the Flask server"""
        print(f"\nMarket Data Viewer is running at: http://{host}:{port}")
        self.app.run(host=host, port=port)

if __name__ == "__main__":
    viewer = MarketDataViewer()
    viewer.run() 