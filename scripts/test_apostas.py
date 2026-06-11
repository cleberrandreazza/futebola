#!/usr/bin/env python3
"""Testa a feature de apostas (Convex + helpers do bot.py)."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(ROOT, ".env"))


def _convex_client():
    url = os.getenv("CONVEX_URL", "").strip()
    if not url:
        return None
    from convex import ConvexClient

    return ConvexClient(url)


def _args(extra: dict | None = None) -> dict:
    out = dict(extra or {})
    secret = os.getenv("BOT_SHARED_SECRET", "").strip()
    if secret:
        out["secret"] = secret
    return out


def test_convex_api() -> None:
    c = _convex_client()
    if not c:
        print("SKIP Convex — CONVEX_URL não definido")
        return

    uid = f"test_script_{int(time.time())}"
    name = "Script Test"

    def step(label: str, fn):
        t0 = time.time()
        try:
            result = fn()
            print(f"OK {label} ({time.time() - t0:.2f}s)")
            return result
        except Exception as e:
            print(f"FAIL {label}: {e}")
            raise

    step("getSaldo (novo)", lambda: c.query("apostas:getSaldo", _args({"userId": uid})))
    ap = step(
        "ensureApostador",
        lambda: c.mutation(
            "apostas:ensureApostador",
            _args({"userId": uid, "displayName": name, "creditoInicial": 1000}),
        ),
    )
    assert ap["saldo"] == 1000, ap

    bet = step(
        "placeBet",
        lambda: c.mutation(
            "apostas:placeBet",
            _args(
                {
                    "userId": uid,
                    "displayName": name,
                    "eventId": "test_event_1",
                    "home": "Time A",
                    "away": "Time B",
                    "palpite": "X",
                    "valor": 25,
                    "odd": 2.0,
                    "apostaMinima": 10,
                    "creditoInicial": 1000,
                }
            ),
        ),
    )
    assert bet.get("ok"), bet
    aposta_id = bet["apostaId"]

    saldo = step("getSaldo (após aposta)", lambda: c.query("apostas:getSaldo", _args({"userId": uid})))
    assert saldo["saldo"] == 975, saldo

    step("listOpen", lambda: c.query("apostas:listOpen", _args()))
    step("listByUser", lambda: c.query("apostas:listByUser", _args({"userId": uid, "limit": 5})))

    settle = step(
        "settle (ganhou)",
        lambda: c.mutation(
            "apostas:settle",
            _args({"apostaId": aposta_id, "resultado": "ganhou"}),
        ),
    )
    assert settle.get("ok"), settle

    final = step("getSaldo (liquidado)", lambda: c.query("apostas:getSaldo", _args({"userId": uid})))
    assert final["saldo"] == 975 + 50, final  # 25 * 2 retorno

    step("getRanking", lambda: c.query("apostas:getRanking", _args({"criterio": "saldo", "limit": 10})))


def test_bot_helpers() -> None:
    import bot
    from unittest.mock import patch

    uid = f"local_{int(time.time())}"
    ap = bot._apostas_ensure(uid, "Local Test")
    assert ap["saldo"] >= bot.CREDITO_INICIAL or ap["saldo"] >= 0

    with patch.object(bot, "_validar_evento_apostavel", return_value={"ok": True, "event": {"status": "notstarted"}}):
        r = bot._apostas_place(
            uid, "Local Test", "evt_local", "Casa", "Fora", "1", bot.APOSTA_MINIMA
        )
    assert r.get("ok"), r

    bets = bot._apostas_list_user(uid, 5)
    assert bets, "deveria ter aposta aberta"

    ok = bot._apostas_settle(str(bets[0]["_id"]), "cancelada")
    assert ok, "settle cancelada"

    rows = bot._apostas_ranking("saldo", 5)
    assert isinstance(rows, list)

    embed = bot._embed_saldo(bot._apostas_ensure(uid, "Local Test"))
    assert embed.title

    print("OK bot helpers — ensure, place, list, settle, ranking, embed")


async def test_slash_saldo_handler() -> None:
    """Simula /saldo com member autorizado."""
    import bot
    import discord
    from unittest.mock import AsyncMock, MagicMock, patch

    member = MagicMock(spec=discord.Member)
    member.id = 999888777
    member.display_name = "Mock User"
    member.guild_permissions.administrator = True
    member.roles = []

    interaction = MagicMock()
    interaction.user = member
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    ap_data = {
        "userId": str(member.id),
        "saldo": 1000,
        "totalGanho": 0,
        "apostasGanhas": 0,
        "apostasPerdidas": 0,
    }
    with patch.object(bot, "_pode_apostar", return_value=True), patch.object(
        bot, "_apostas_ensure", return_value=ap_data
    ):
        await bot.slash_saldo.callback(interaction)

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    print("OK slash /saldo handler (mock)")


def test_eventos_apostaveis_escopo() -> None:
    import bot
    from unittest.mock import patch

    hoje = datetime.now(tz=bot.BRT).date()

    def fake_liga(chave: str):
        if chave == "copadomundo":
            return [{
                "fixture": {"id": "espn1", "date": "", "status": {"short": "NS", "elapsed": None}},
                "teams": {
                    "home": {"name": "South Korea", "logo": ""},
                    "away": {"name": "Czechia", "logo": ""},
                },
                "goals": {"home": None, "away": None},
                "meta": {},
            }]
        return []

    bz_ev = {
        "id": 9001,
        "status": "notstarted",
        "home_team": "South Korea",
        "away_team": "Czechia",
        "event_date": datetime.now(tz=bot.BRT).isoformat(),
        "league_id": 99,
    }

    def bz_get(path, params=None):
        if path == "events/":
            return {"results": []}
        return {"error": True}

    with patch.object(bot, "_buscar_jogos_liga_hoje", side_effect=fake_liga), patch.object(
        bot, "_bzzoiro_get", side_effect=bz_get
    ), patch.object(bot, "_find_bz_event_id", return_value=None):
        eventos = bot._bz_eventos_apostaveis()
        assert len(eventos) == 1, eventos
        assert eventos[0]["id"] == "espn:fifa.world:espn1", eventos[0]
        assert eventos[0]["home_team"] == "South Korea"

    print("OK _bz_eventos_apostaveis — fallback ESPN quando Bzzoiro ausente")


def test_evento_apostavel() -> None:
    import bot

    assert bot._evento_e_apostavel({"status": "notstarted"})
    assert not bot._evento_e_apostavel({"status": "finished"})
    assert not bot._evento_e_apostavel({"status": "inprogress"})
    assert not bot._evento_e_apostavel({"status": "cancelled"})

    with __import__("unittest.mock").mock.patch.object(
        bot, "_bzzoiro_get", return_value={"status": "finished", "home_team": "A", "away_team": "B"}
    ):
        r = bot._validar_evento_apostavel("123")
        assert not r["ok"] and "encerrada" in r["error"].lower()

    with __import__("unittest.mock").mock.patch.object(
        bot, "_bzzoiro_get", return_value={"status": "notstarted", "home_team": "A", "away_team": "B"}
    ):
        r = bot._validar_evento_apostavel("123")
        assert r["ok"]

    print("OK regra — só partidas notstarted")


def main() -> int:
    print("=== Regra notstarted ===")
    test_evento_apostavel()
    print("\n=== Escopo /hoje ===")
    test_eventos_apostaveis_escopo()
    print("\n=== Convex API ===")
    test_convex_api()
    print("\n=== Bot helpers ===")
    test_bot_helpers()
    print("\n=== Slash handler ===")
    asyncio.run(test_slash_saldo_handler())
    print("\nTodos os testes de apostas passaram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
