# CURE Market Maker Bot

A Python-based, fully automated market-making bot designed specifically for the **BTC_CURE** trading pair on the Altquick exchange. The bot implements a modified Avellaneda-Stoikov pricing model to actively defend the ecosystem, provide healthy liquidity, and sweep profits into permanent support levels.

It includes a lightweight, unified Flask backend that serves a real-time, dark-themed web dashboard complete with a live terminal console, active order tracking, and a candlestick chart.

## ✨ Core Features

* **Loyal Avellaneda-Stoikov Skew:** Automatically shifts the pricing grid based on your target CURE inventory, buying aggressively when low on CURE and widening spreads when over-exposed.
* **1-Satoshi Floor Protection:** Mathematically prevents the bot from crashing at the absolute orderbook floor, intelligently aggregating truncated grid levels into massive 1-satoshi "Buy Walls."
* **Maker Protection Clamp:** Strictly prevents the bot from crossing the spread to ensure it always provides liquidity (Maker) rather than taking it (Taker).
* **Profit Sweeping (The Vault):** Automatically detects accumulated BTC profits and sweeps 20% into permanent, un-cancelable 2-satoshi support bids to permanently raise the token's floor.
* **State Persistence:** Survives reboots and crashes by saving order history, baseline BTC, and permanent bid IDs to a local `bot_state.json` file.
* **Live Command Center:** A unified Flask UI (`http://127.0.0.1:5000`) featuring:
  * Live BTC/CURE balance sync
  * Real-time orderbook and active bot orders
  * A live-scrolling Python terminal directly in the browser
  * Interactive candlestick charting (Chart.js)

## 📂 Project Structure

```
AltquickGUIv2/
├── market_maker_v2.py      # Main trading engine and Flask server
├── config.py               # API credentials (DO NOT COMMIT THIS FILE)
├── config example.py       # Example configuration template
├── requirements.txt        # Python dependencies
├── templates/
│   └── dashboard.html      # HTML/JS frontend command center
├── .gitignore             # Git ignore rules
└── README.md              # This file
```

## ⚙️ Installation & Setup

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/AltquickGUIv2.git
cd AltquickGUIv2
```

### 2. Install dependencies

The bot requires Python 3.x and a few basic libraries.

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install Flask==3.1.3 Requests==2.32.5
```

### 3. Configure your API Keys

You must create a configuration file to hold your Altquick exchange API credentials.

1. Copy the example configuration file:
   ```bash
   cp "config example.py" config.py
   ```

2. Edit `config.py` and add your actual API credentials:
   ```python
   # config.py
   api_key = 'YOUR_ALTQUICK_API_KEY'
   api_secret = 'YOUR_ALTQUICK_API_SECRET'
   ```

> **⚠️ Important:** Ensure `config.py` is added to your `.gitignore` file so you do not accidentally publish your API keys to GitHub.

## 🚀 Running the Bot

Start the market maker by running the main Python script:

```bash
python market_maker_v2.py
```

Once the bot initializes, it will automatically:
- Connect to the Altquick exchange API
- Load any existing bot state from `bot_state.json`
- Start the Flask web server
- Begin market making operations

Open your web browser and navigate to:
**👉 http://127.0.0.1:5000**

You can monitor the bot's mathematical decisions, active orders, and API connections directly from the "Live Bot Console" widget at the bottom of the dashboard.

## 🎛️ Dashboard Features

The web dashboard provides real-time monitoring and control:

- **Balance Overview:** Live BTC and CURE balance tracking
- **Active Orders:** Real-time display of all bot-managed orders
- **Market Data:** Current orderbook state and mid-price calculations
- **Performance Metrics:** Profit tracking and position monitoring
- **Live Console:** Python terminal output streamed directly to the browser
- **Interactive Charts:** Candlestick price charts with technical indicators

## ⚙️ Configuration Parameters

Key bot parameters can be adjusted in `market_maker_v2.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_spread` | 0.41 | Minimum spread percentage (1%) |
| `max_spread` | 0.65 | Maximum spread percentage (5%) |
| `target_cure_position` | 100,000 | Ideal CURE inventory target |
| `gamma` | 0.05 | Risk aversion parameter |
| `max_position` | 50 | Maximum position size |
| `min_order_interval` | 5 | Minimum seconds between orders |

## 🔧 Technical Details

### Avellaneda-Stoikov Model

The bot implements a modified version of the Avellaneda-Stoikov market making strategy:

- **Inventory Skew:** Adjusts bid/ask prices based on current CURE inventory relative to target
- **Risk Management:** Uses gamma parameter to control how aggressively prices shift with inventory
- **Volatility Adaptation:** Dynamically adjusts spreads based on market volatility

### Safety Features

- **Maker-Only Mode:** Prevents crossing spreads to avoid taking liquidity
- **Position Limits:** Automatic position size management and rebalancing
- **Error Handling:** Comprehensive exception handling and automatic recovery
- **State Persistence:** Saves critical data to survive restarts

## 📊 Monitoring & Logs

The bot provides extensive logging and monitoring:

- Real-time console output in the web dashboard
- Detailed order execution logs
- Balance and position tracking
- API response monitoring
- Error reporting and recovery actions

## 🛑 Risk Management

### Built-in Protections

- **Position Limits:** Automatic position size constraints
- **Spread Validation:** Prevents orders that would cross the spread
- **Balance Checks:** Validates sufficient funds before placing orders
- **API Rate Limiting:** Respects exchange API rate limits

### Best Practices

- Start with small position sizes to test the bot
- Monitor the dashboard regularly, especially during high volatility
- Keep API keys secure and never commit them to version control
- Maintain adequate BTC and CURE balances for optimal operation

## 🔍 Troubleshooting

### Common Issues

1. **API Connection Errors**
   - Verify API credentials in `config.py`
   - Check internet connection
   - Ensure API keys have trading permissions

2. **Order Placement Failures**
   - Check account balances
   - Verify minimum order sizes
   - Review exchange trading rules

3. **Dashboard Not Loading**
   - Ensure Flask server is running
   - Check for port conflicts (default: 5000)
   - Verify firewall settings

### Getting Help

- Check the live console in the dashboard for error messages
- Review the Python terminal output for detailed logs
- Ensure all dependencies are properly installed

## 📄 License

This project is provided as-is for educational and experimental purposes.

## 🛑 Disclaimer

**This software is for educational and experimental purposes only.** Market making carries inherent financial risks, especially in low-liquidity or high-volatility cryptocurrency markets. 

**⚠️ Important Warnings:**
- Always test with small amounts of capital first
- Monitor the bot continuously during operation
- Understand the risks of automated trading
- Be prepared for potential losses

**The developers assume no responsibility for any financial losses incurred while running this software.** Use at your own risk and never invest more than you can afford to lose.

---

*Built with ❤️ for the CURE ecosystem*