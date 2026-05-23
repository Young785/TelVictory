"""File-based database for trading bot using JSON."""
import json
import os
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from pathlib import Path

DB_DIR = Path(__file__).parent / "data"
DB_DIR.mkdir(exist_ok=True)

TRADES_FILE = DB_DIR / "trades.json"
SIGNALS_FILE = DB_DIR / "signals.json"
LOGS_FILE = DB_DIR / "logs.json"
SETTINGS_FILE = DB_DIR / "settings.json"
CHANNELS_FILE = DB_DIR / "channels.json"


class JSONDB:
    """Simple JSON file-based database."""
    
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self._ensure_file()
    
    def _ensure_file(self):
        if not self.file_path.exists():
            self.file_path.write_text(json.dumps([]))
    
    def read(self) -> List[Dict]:
        try:
            return json.loads(self.file_path.read_text())
        except:
            return []
    
    def write(self, data: List[Dict]):
        self.file_path.write_text(json.dumps(data, indent=2, default=str))
    
    def insert(self, record: Dict):
        data = self.read()
        record['id'] = len(data) + 1
        record['created_at'] = datetime.now().isoformat()
        data.append(record)
        self.write(data)
        return record
    
    def update(self, id: int, updates: Dict):
        data = self.read()
        for item in data:
            if item.get('id') == id:
                item.update(updates)
                item['updated_at'] = datetime.now().isoformat()
                self.write(data)
                return item
        return None
    
    def get(self, id: int) -> Optional[Dict]:
        data = self.read()
        for item in data:
            if item.get('id') == id:
                return item
        return None
    
    def get_all(self) -> List[Dict]:
        return self.read()
    
    def delete(self, id: int):
        data = self.read()
        data = [item for item in data if item.get('id') != id]
        self.write(data)


# Database instances
trades_db = JSONDB(TRADES_FILE)
signals_db = JSONDB(SIGNALS_FILE)
logs_db = JSONDB(LOGS_FILE)
settings_db = JSONDB(SETTINGS_FILE)
channels_db = JSONDB(CHANNELS_FILE)


class SettingsDB(JSONDB):
    """Settings database with key-based updates."""

    def set(self, key: str, value):
        """Set a value by key."""
        data = self.read()
        # Convert list to dict if needed
        if isinstance(data, list):
            data = {}
        data[key] = value
        self.write(data)

    def get(self, key: str, default=None):
        """Get a value by key."""
        data = self.read()
        if isinstance(data, dict):
            return data.get(key, default)
        return default


# Replace settings_db with SettingsDB class
settings_db = SettingsDB(SETTINGS_FILE)


def log_event(level: str, message: str, source: str = "system"):
    """Log an event to the database."""
    logs_db.insert({
        'level': level,
        'message': message,
        'source': source
    })


def get_stats() -> Dict:
    """Get trading statistics."""
    trades = trades_db.get_all()
    signals = signals_db.get_all()
    
    total_trades = len(trades)
    total_signals = len(signals)
    
    won = len([t for t in trades if t.get('result') == 'win'])
    lost = len([t for t in trades if t.get('result') == 'loss'])
    pending = len([t for t in trades if t.get('result') == 'pending'])
    
    total_profit = sum([float(t.get('profit', 0) or 0) for t in trades])
    
    return {
        'total_trades': total_trades,
        'total_signals': total_signals,
        'won': won,
        'lost': lost,
        'pending': pending,
        'total_profit': round(total_profit, 2),
        'win_rate': round(won / (won + lost) * 100, 2) if (won + lost) > 0 else 0
    }
