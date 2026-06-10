SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "PAXGUSDT"]
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]
HISTORY_LIMIT = 1000       # Fetch last 1000 candles on bootstrap
MAX_CACHE_SIZE = 1000      # Keep last 1000 candles in RAM
INDICATOR_WINDOW = 260     # Compute indicators on last 260 rows (SMA200 needs 200+)
