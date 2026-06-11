#!/usr/bin/env python3
"""Verifica configuração do app Discord (precisa de TOKEN_DISCORD no .env).

Uso: python scripts/check_discord_app.py
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))

TOKEN = os.getenv("TOKEN_DISCORD", "").strip()
if not TOKEN:
    print("ERRO: TOKEN_DISCORD não encontrado. Crie um .env com TOKEN_DISCORD=...")
    sys.exit(1)


async def main() -> None:
    import discord

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        app = await client.application_info()
        print(f"App: {app.name} (id {app.id})")
        endpoint = app.interactions_endpoint_url
        if endpoint:
            print()
            print("=" * 70)
            print("PROBLEMA ENCONTRADO — Interactions Endpoint URL está configurado:")
            print(f"  {endpoint}")
            print()
            print("Isso impede slash commands de chegarem ao bot discord.py.")
            print("Remova em: Discord Developer Portal → General → Interactions Endpoint URL")
            print("=" * 70)
        else:
            print("OK — Interactions Endpoint URL vazio (gateway recebe comandos)")
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
