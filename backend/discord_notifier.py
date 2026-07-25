import os
import aiohttp
import asyncio
import logging

async def send_discord_embed(embed_payload: dict):
    """
    Asynchronously sends a Discord Webhook notification containing the embed_payload.
    Fails silently on timeouts or errors to prevent blocking the main trading engine.
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
    enabled = os.getenv("DISCORD_ENABLED", "True").lower() in ("true", "1", "yes")

    if not enabled or not webhook_url or webhook_url.startswith("your_"):
        return

    payload = {
        "embeds": [embed_payload]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(webhook_url, json=payload, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                if response.status not in (200, 204):
                    logging.warning(f"Discord Webhook returned status {response.status}")
    except Exception as e:
        # Fail silently as requested to prevent loop disruption
        logging.error(f"Discord notification failed: {e}")
