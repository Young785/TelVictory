#!/usr/bin/env python3
"""
Trading Bot Runner - Supports both Terminal and Web UI modes.

Usage:
    python run.py --mode terminal    # Run in terminal mode (default)
    python run.py --mode web         # Run Web UI only
    python run.py --mode both        # Run both terminal bot + Web UI
"""
import argparse
import sys
import threading
import time
import signal
import os

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Monkey patch logger for library compatibility
import logging
if not hasattr(logging.Logger, 'warn'):
    logging.Logger.warn = lambda self, msg, *args, **kwargs: self.warning(msg, *args, **kwargs)


def run_terminal_bot():
    """Run bot in terminal mode."""
    print("🤖 Starting Terminal Bot Mode...")
    from bot import main
    main()


def run_web_ui(port=5007):
    """Run Web UI."""
    print(f"🌐 Trading Bot UI starting on http://localhost:{port}")
    print(f"📊 Dashboard: http://localhost:{port}/")
    from web_ui import run_ui
    run_ui(port=port, debug=False)


def run_both(port=5007):
    """Run both terminal bot and Web UI."""
    print("🔥 Running BOTH Terminal Bot + Web UI")
    
    # Start Web UI in background thread
    web_thread = threading.Thread(target=run_web_ui, args=(port,))
    web_thread.daemon = True
    web_thread.start()
    
    print(f"\n📊 Web Dashboard: http://localhost:{port}")
    print("🤖 Terminal Bot: Starting...\n")
    
    # Run terminal bot in main thread
    run_terminal_bot()


def main():
    parser = argparse.ArgumentParser(description='Trading Bot - Terminal & Web UI')
    parser.add_argument('--mode', choices=['terminal', 'web', 'both'], 
                        default='terminal', help='Run mode')
    parser.add_argument('--port', type=int, default=5007, help='Web UI port')
    
    args = parser.parse_args()
    
    print("""
╔═══════════════════════════════════════════════╗
║         💰 Trading Bot Launcher               ║
║                                               ║
║   Terminal Mode: python run.py --mode terminal║
║   Web UI Mode:  python run.py --mode web     ║
║   Both Modes:   python run.py --mode both    ║
╚═══════════════════════════════════════════════╝
    """)
    
    if args.mode == 'terminal':
        run_terminal_bot()
    elif args.mode == 'web':
        run_web_ui(args.port)
    elif args.mode == 'both':
        run_both(args.port)


if __name__ == '__main__':
    main()
