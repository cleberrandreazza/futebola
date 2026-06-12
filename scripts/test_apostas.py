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
    if os.getenv("CONVEX_INTEGRATION", "").strip() != "1":
        print("SKIP Convex — defina CONVEX_INTEGRATION=1 para testar contra o deployment")
        return

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

    try:
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
                        "matchKey": "2026-06-11|time a|time b",
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

        saldo = step(
            "getSaldo (após aposta)",
            lambda: c.query("apostas:getSaldo", _args({"userId": uid})),
        )
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

        final = step(
            "getSaldo (liquidado)",
            lambda: c.query("apostas:getSaldo", _args({"userId": uid})),
        )
        assert final["saldo"] == 975 + 50, final

        ranking = step(
            "getRanking",
            lambda: c.query("apostas:getRanking", _args({"criterio": "vitorias", "limit": 10})),
        )
        assert all(
            str(r.get("userId", "")).isdigit() for r in ranking
        ), "ranking não deve incluir IDs de teste"
    finally:
        purge = c.mutation("apostas:purgeTestApostadores", _args())
        print(
            f"OK purgeTestApostadores — {purge.get('apostadoresRemovidos', 0)} apostadores, "
            f"{purge.get('apostasRemovidas', 0)} apostas"
        )


def test_bot_helpers() -> None:
    import bot
    from unittest.mock import patch

    uid = f"local_{int(time.time())}"

    with patch.object(bot, "_convex_client", None):
        ap = bot._apostas_ensure(uid, "Local Test")
        assert ap["saldo"] >= bot.CREDITO_INICIAL or ap["saldo"] >= 0

        with patch.object(
            bot,
            "_validar_evento_apostavel",
            return_value={"ok": True, "event": {"status": "notstarted"}},
        ):
            r = bot._apostas_place(
                uid, "Local Test", "evt_local", "Casa", "Fora", "1", bot.APOSTA_MINIMA
            )
        assert r.get("ok"), r

        bets = bot._apostas_list_user(uid, 5)
        assert bets, "deveria ter aposta aberta"

        ok = bot._apostas_settle(str(bets[0]["_id"]), "cancelada")
        assert ok, "settle cancelada"

        rows = bot._apostas_ranking("vitorias", 5)
        assert isinstance(rows, list)
        assert not any(r.get("userId") == uid for r in rows), "ranking local ignora IDs não-Discord"

        embed = bot._embed_saldo(bot._apostas_ensure(uid, "Local Test"))
        assert embed.title

    print("OK bot helpers — ensure, place, list, settle, ranking, embed (local only)")


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


def test_duplicata_match_key() -> None:
    import bot
    from unittest.mock import patch

    uid = f"dup_{int(time.time())}"
    ed = "2026-06-11T23:00:00+00:00"
    mk = bot._aposta_match_key("South Korea", "Czechia", ed)
    mk_alias = bot._aposta_match_key("Korea Republic", "Czechia", ed)
    assert mk == mk_alias, (mk, mk_alias)

    ok_ev = {"ok": True, "event": {"status": "notstarted"}}
    with patch.object(bot, "_convex_client", None), patch.object(
        bot, "_validar_evento_apostavel", return_value=ok_ev
    ):
        r1 = bot._apostas_place(
            uid, "Dup Test", "espn:fifa.world:760414",
            "South Korea", "Czechia", "1", bot.APOSTA_MINIMA, ed, mk,
        )
        assert r1.get("ok"), r1
        r2 = bot._apostas_place(
            uid, "Dup Test", "760414",
            "Korea Republic", "Czechia", "X", bot.APOSTA_MINIMA, ed, mk_alias,
        )
        assert not r2.get("ok"), r2
        assert "aposta aberta" in r2.get("error", "").lower()

    print("OK duplicata — matchKey bloqueia IDs diferentes na mesma partida")


def test_ranking_sort_vitorias() -> None:
    import bot

    rows = [
        {"userId": "1", "displayName": "A", "saldo": 950, "totalGanho": 0,
         "apostasGanhas": 0, "apostasPerdidas": 0},
        {"userId": "2", "displayName": "B", "saldo": 500, "totalGanho": -100,
         "apostasGanhas": 0, "apostasPerdidas": 1},
        {"userId": "3", "displayName": "C", "saldo": 500, "totalGanho": 0,
         "apostasGanhas": 0, "apostasPerdidas": 0},
        {"userId": "4", "displayName": "D", "saldo": 200, "totalGanho": 50,
         "apostasGanhas": 2, "apostasPerdidas": 1},
    ]
    rows.sort(key=lambda r: bot._ranking_sort_key(r, "vitorias"))
    assert [r["displayName"] for r in rows] == ["D", "A", "C", "B"]
    print("OK ranking — vitórias, saldo e menos derrotas")


def test_espn_liquidacao_summary() -> None:
    import bot
    from unittest.mock import patch

    sumario_ft = {
        "header": {
            "competitions": [{
                "status": {"type": {"state": "post", "name": "STATUS_FULL_TIME"}},
                "competitors": [
                    {"homeAway": "home", "score": "2"},
                    {"homeAway": "away", "score": "1"},
                ],
            }],
        },
    }
    with patch.object(bot, "buscar_partida_espn", return_value=sumario_ft):
        assert bot._espn_resultado_1x2_summary("fifa.world", "760414") == "1"

    with patch.object(bot, "buscar_partida_espn", return_value=sumario_ft), patch.object(
        bot, "_espn_fixture_por_id", return_value=None
    ):
        assert bot._resultado_aposta_1x2("espn:fifa.world:760414") == "1"

    sumario_pre = {
        "header": {
            "competitions": [{
                "status": {"type": {"state": "pre", "name": "STATUS_SCHEDULED"}},
                "competitors": [
                    {"homeAway": "home", "score": "0"},
                    {"homeAway": "away", "score": "0"},
                ],
            }],
        },
    }
    with patch.object(bot, "buscar_partida_espn", return_value=sumario_pre), patch.object(
        bot, "_espn_fixture_por_id", return_value=None
    ):
        assert bot._resultado_aposta_1x2("espn:fifa.world:999") is None

    print("OK liquidação ESPN — summary fora do placar do dia")


async def test_publicar_ranking_pos_partida() -> None:
    import bot
    from unittest.mock import AsyncMock, MagicMock, patch

    canal = MagicMock()
    canal.send = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_channel.return_value = canal

    rows = [{"userId": "1", "displayName": "Test", "saldo": 100, "totalGanho": 0,
             "apostasGanhas": 0, "apostasPerdidas": 0}]
    with patch.object(bot, "bot", mock_bot), patch.object(
        bot, "_apostas_ranking", return_value=rows
    ), patch.object(bot, "CANAL_COMANDOS_ID", 999):
        await bot._publicar_ranking_pos_partida("Casa", "Fora", "1")

    canal.send.assert_awaited_once()
    args, kwargs = canal.send.await_args
    assert "Casa" in kwargs["content"] and "Fora" in kwargs["content"]
    assert kwargs["embed"].title.startswith("🏆 Ranking")

    print("OK publicar ranking — embed de /rank-apostas após liquidação")


def main() -> int:
    print("=== Regra notstarted ===")
    test_evento_apostavel()
    print("\n=== Duplicata matchKey ===")
    test_duplicata_match_key()
    print("\n=== Ranking vitórias ===")
    test_ranking_sort_vitorias()
    print("\n=== Liquidação ESPN summary ===")
    test_espn_liquidacao_summary()
    print("\n=== Publicar ranking pós-partida ===")
    asyncio.run(test_publicar_ranking_pos_partida())
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
