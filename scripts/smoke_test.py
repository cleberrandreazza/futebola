#!/usr/bin/env python3
"""Testes locais antes do push — não precisa de TOKEN_DISCORD.

Uso: python scripts/smoke_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Evita SystemExit ao importar bot.py (sem token real)
os.environ.setdefault("TOKEN_DISCORD", "test-token-local-smoke")


def test_import_and_commands() -> None:
    import bot  # noqa: WPS433

    cmds = bot.bot.tree.get_commands()
    names = {c.name for c in cmds}
    assert len(cmds) >= 18, f"Esperava >=18 slash commands, got {len(cmds)}"
    for required in ("hoje", "ajuda", "calendario", "menu"):
        assert required in names, f"Comando /{required} não registrado"
    print(f"OK import — {len(cmds)} slash commands registrados")


async def test_salvar_async_nao_bloqueia_loop() -> None:
    import bot  # noqa: WPS433

    bloqueou = {"valor": False}

    def _salvar_lento(data: dict) -> None:
        time.sleep(2.0)

    bot._salvar_seguindo = _salvar_lento  # type: ignore[method-assign]

    async def ping() -> None:
        await asyncio.sleep(0.05)
        bloqueou["valor"] = True

    await asyncio.gather(
        bot._salvar_seguindo_async({}),
        ping(),
    )
    assert bloqueou["valor"], "Event loop ficou bloqueado durante _salvar_seguindo_async"
    print("OK event loop — Convex simulado lento não bloqueou outras coroutines")


async def test_slash_hoje_defer_imediato() -> None:
    import bot  # noqa: WPS433
    from unittest.mock import AsyncMock, MagicMock, patch

    interaction = MagicMock()
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    with patch.object(bot, "_buscar_jogos_resumo_paralelo", new=AsyncMock(return_value={})):
        await bot.slash_hoje.callback(interaction)

    interaction.response.defer.assert_awaited()
    interaction.followup.send.assert_awaited()
    print("OK /hoje — defer + followup quando não há jogos")


async def test_verificar_noticias_nao_salva_sempre() -> None:
    import bot  # noqa: WPS433
    from unittest.mock import AsyncMock, patch

    bot._SEGUINDO.clear()
    bot._SEGUINDO["1"] = {
        "times": ["Flamengo"],
        "noticias_vistas": {},
        "prefs": {"noticias": True, "jogos": True, "lembrete": True},
    }

    salvar_calls = 0

    async def _fake_salvar(_data: dict) -> None:
        nonlocal salvar_calls
        salvar_calls += 1

    with patch.object(bot, "_buscar_noticias_time", new=AsyncMock(return_value=[])), patch.object(
        bot, "_salvar_seguindo_async", new=_fake_salvar
    ):
        await bot.verificar_noticias_times.coro()

    assert salvar_calls == 0, "verificar_noticias_times não deve salvar sem novidades"
    print("OK verificar_noticias — não grava no Convex sem novidades")


def main() -> None:
    test_import_and_commands()
    asyncio.run(test_salvar_async_nao_bloqueia_loop())
    asyncio.run(test_slash_hoje_defer_imediato())
    asyncio.run(test_verificar_noticias_nao_salva_sempre())
    print("\nTodos os smoke tests passaram.")


if __name__ == "__main__":
    main()
