#!/usr/bin/env python3
"""Verifica (e opcionalmente corrige) Interactions Endpoint URL no Discord.

Uso:
  python scripts/check_discord_app.py
  python scripts/check_discord_app.py --fix
"""
from __future__ import annotations

import argparse
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
    print("ERRO: TOKEN_DISCORD não encontrado no .env")
    sys.exit(1)


async def main(fix: bool) -> None:
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
            print("PROBLEMA — Interactions Endpoint URL configurado:")
            print(f"  {endpoint}")
            print("Slash commands NÃO chegam ao bot discord.py enquanto isso existir.")
            if fix:
                try:
                    await app.edit(interactions_endpoint_url=None)
                    app2 = await client.application_info()
                    print("OK — removido. Novo valor:", app2.interactions_endpoint_url or "(vazio)")
                except Exception as e:
                    print(f"Falha ao remover via API: {e}")
                    print("Remova manualmente no Developer Portal → General")
            else:
                print("Rode com --fix para tentar remover via API, ou apague no Developer Portal.")
        else:
            print("OK — Interactions Endpoint URL vazio (gateway recebe comandos)")
        await client.close()

    await client.start(TOKEN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true", help="Remove Interactions Endpoint URL via API")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.fix))
    except KeyboardInterrupt:
        pass
