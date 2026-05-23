#!/usr/bin/env python3
"""
Create a personal Telegram user session (not bot) to join channels.
"""
import asyncio
import sys
sys.path.insert(0, '/Users/ariyoayomikun/Downloads/TelVictory/trades/loren')

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from config import Config

async def create_user_session():
    config = Config()
    
    # Create new client - will prompt for phone number
    client = TelegramClient(
        'loren_user_session',  # New session file
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH
    )
    
    print("Creating user session (not bot account)...")
    print("This will allow you to join private channels automatically.\n")
    
    await client.start()
    
    me = await client.get_me()
    print(f"\n✅ Success! Connected as: {me.first_name} (@{me.username})")
    print(f"User ID: {me.id}")
    print(f"Session file: loren_user_session.session")
    
    # Now try to join the channel
    print("\n🔄 Attempting to join the trading channel...")
    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        result = await client(ImportChatInviteRequest('ysLwQtzaUb42MWMx'))
        print("✅ Successfully joined the channel!")
    except Exception as e:
        if 'already' in str(e).lower():
            print("✅ Already a member of the channel")
        else:
            print(f"⚠️ Could not join: {e}")
            print("You may need to join manually via Telegram app first.")
    
    await client.disconnect()
    
    print("\n📝 Next steps:")
    print("1. Update your .env file:")
    print('   TELEGRAM_SESSION_NAME=loren_user_session')
    print("2. Restart the bot and it will monitor the joined channel!")

if __name__ == "__main__":
    asyncio.run(create_user_session())
