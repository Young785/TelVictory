# Trading Signal Bot

Automated trading bot that listens for signals in Telegram channels and executes trades on IQ Option.

## Features

- **Signal Parsing**: Automatically extracts trading signals from Telegram messages
- **Scheduled Execution**: Places trades at exact entry times
- **Martingale Support**: Handles multiple martingale levels with configurable multipliers
- **OTC Detection**: Recognizes OTC (Over-The-Counter) trades
- **Channel Filtering**: Only processes signals from specified channels

## Project Structure

```
TelVictory/
├── trades/loren/
│   ├── __init__.py
│   ├── bot.py              # Main Telegram bot
│   ├── signal_parser.py    # Signal extraction from messages
│   ├── trade_executor.py   # Trading platform integration
│   └── config.py           # Configuration management
├── main.py                 # Entry point
├── requirements.txt        # Dependencies
├── .env.example            # Environment variables template
└── README.md               # This file
```

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Create Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Create a new bot with `/newbot`
3. Copy the bot token
4. Add the bot to your trading signal channel as an administrator

### 3. Get Channel ID

1. Add [@userinfobot](https://t.me/userinfobot) to your channel
2. Send a message in the channel
3. The bot will reply with the channel ID (starts with `-100`)

### 4. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHANNEL_ID=-1001234567890
IQ_OPTION_EMAIL=your@email.com
IQ_OPTION_PASSWORD=yourpassword
DEFAULT_AMOUNT=10.0
MAX_MARTINGALE_LEVELS=3
```

### 5. Install IQ Option API (Optional)

For real trading, install the unofficial IQ Option API:

```bash
pip install iqoption-stable-api
```

**Note**: This uses an unofficial API. Trade at your own risk.

## Usage

### Start the Bot

```bash
python main.py
```

### Bot Commands

- `/start` - Show welcome message
- `/status` - Check bot configuration
- `/test <message>` - Test signal parsing without trading

### Signal Format

The bot recognizes messages in this format:

```
🇺🇸 USD/CHF 🇨🇭 OTC 🕘 Expiration 5M
⏺ Entry at 16:25 🟥 SELL 🔽
Martingale levels
1️⃣ level at 16:30
2️⃣ level at 16:35
3️⃣ level at 16:40
💥 GET THIS SIGNAL HERE!
```

## How It Works

1. **Signal Detection**: Bot monitors the configured Telegram channel
2. **Parsing**: Extracts pair, direction, entry time, expiration, and martingale levels
3. **Scheduling**: Uses APScheduler to execute trades at precise times
4. **Execution**: Places trades via IQ Option API
5. **Martingale**: Automatically handles subsequent levels if configured

## Safety & Warnings

⚠️ **IMPORTANT**: This is automated trading software. Use at your own risk.

- Always test with `/test` command first
- Use small amounts for initial testing
- Binary options trading carries high risk
- The IQ Option API is unofficial and may break
- Never trade more than you can afford to lose

## Customization

### Adding Other Trading Platforms

Implement the `TradingPlatform` abstract class in `trade_executor.py`:

```python
class MyBroker(TradingPlatform):
    async def connect(self) -> bool:
        # Your connection logic
        pass
    
    async def place_trade(self, pair, amount, direction, expiration, is_otc):
        # Your trading logic
        pass
```

### Adjusting Martingale Strategy

Edit `MARTINGALE_MULTIPLIER` in `.env`:
- `2.0` = Double each level
- `2.5` = 2.5x each level (default)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot not responding | Check TELEGRAM_BOT_TOKEN |
| Signals not detected | Verify channel ID and bot permissions |
| Trades not executing | Check IQ Option credentials |
| Wrong entry times | Verify system timezone matches signal timezone |

## License

Use responsibly. This software is for educational purposes.
# TelVictory
