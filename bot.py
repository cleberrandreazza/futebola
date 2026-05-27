import os
import re
import base64
import asyncio
import secrets
import discord
from discord.ext import commands, tasks
import requests
import aiohttp
from datetime import datetime, timezone, timedelta
from urllib.parse import quote as url_quote
from playwright.async_api import async_playwright
from aiohttp import web as aiohttp_web
from dotenv import load_dotenv

load_dotenv()

TOKEN_DO_DISCORD  = os.getenv("TOKEN_DISCORD")
CANAL_RESUMO_ID   = int(os.getenv("CANAL_JOGOS_DO_DIA", "0"))
# Railway injeta PORT automaticamente; localmente usa SERVER_PORT ou 8080
SERVER_PORT       = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8080")))
SERVER_URL        = os.getenv("SERVER_URL", f"http://localhost:{SERVER_PORT}")
_hora_env         = os.getenv("HORA_RESUMO_DIARIO", "09:00").split(":")
BRT               = timezone(timedelta(hours=-3))
HORARIO_RESUMO    = __import__("datetime").time(
    int(_hora_env[0]), int(_hora_env[1]), tzinfo=BRT
)

# ==========================================
# ESPN API — sem chave, dados 2026 atuais
# ==========================================
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports/soccer"
ESPN_V1 = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# API-Football — usado apenas para Copa do Brasil (ESPN não cobre)
# Free tier: seasons 2022-2024 apenas
API_KEY_FUTEBOL = os.getenv("API_KEY_FUTEBOL")
APIFOOTBALL_BASE = "https://v3.football.api-sports.io"
APIFOOTBALL_HEADERS = {"x-apisports-key": API_KEY_FUTEBOL}

# Competições de mata-mata: !tabela mostra rodada atual, não tabela de pontos
LIGAS_COPA = {"copadobrasil"}

# Slugs das ligas na ESPN (copadobrasil não é suportada pela ESPN)
LIGAS = {
    "brasileirao":   "BRA.1",
    "premierleague": "eng.1",
    "champions":     "UEFA.CHAMPIONS",
    "sulamericana":  "CONMEBOL.SUDAMERICANA",
    "libertadores":  "CONMEBOL.LIBERTADORES",
    "laliga":        "ESP.1",
    "seriea":        "ITA.1",
    "bundesliga":    "GER.1",
    "ligue1":        "FRA.1",
    # Copa do Brasil: roteado via API-Football, não ESPN
    "copadobrasil":  None,
}

# Ligas exibidas no !hoje e no resumo diário automático
LIGAS_RESUMO = ["brasileirao", "premierleague", "sulamericana", "libertadores"]

PASTA_LOGOS = "logos"
os.makedirs(PASTA_LOGOS, exist_ok=True)

_cache_logos: dict = {}
# event_id (str) -> {"canal_id", "slug", "eventos": set(), "encerrado": bool}
JOGOS_MONITORADOS: dict = {}

# ==========================================
# IPTV — Xtream Codes
# ==========================================
IPTV_URL  = os.getenv("IPTV_URL", "").rstrip("/")
IPTV_USER = os.getenv("IPTV_USER", "")
IPTV_PASS = os.getenv("IPTV_PASS", "")

# guild_id -> {"message": discord.Message, "event_id": str, "slug": str, "canal_iptv": str}
PLAYERS_ATIVOS: dict[int, dict] = {}
_cache_canais_iptv: list = []
_ts_cache_iptv: float = 0.0

# token -> {"stream_url": str, "title": str, "event_id": str, "slug": str}
_player_sessions: dict[str, dict] = {}


def _criar_sessao(stream_url: str, title: str, event_id: str, slug: str) -> str:
    token = secrets.token_urlsafe(24)
    _player_sessions[token] = {
        "stream_url": stream_url,
        "title":      title,
        "event_id":   event_id,
        "slug":       slug,
    }
    print(f"[Session] criada token={token[:8]}… event={event_id}")
    return token


def _revogar_sessoes(event_id: str) -> int:
    tokens = [t for t, s in _player_sessions.items() if s["event_id"] == event_id]
    for t in tokens:
        del _player_sessions[t]
    if tokens:
        print(f"[Session] {len(tokens)} sessão(ões) revogada(s) para event={event_id}")
    return len(tokens)

# Metadados das ligas para exibição no resumo diário
LIGAS_META = {
    "brasileirao":   {"nome": "Brasileirão Série A", "emoji": "🇧🇷"},
    "premierleague": {"nome": "Premier League",      "emoji": "🏴󠁧󠁢󠁥󠁮󠁧󠁿"},
    "champions":     {"nome": "Champions League",    "emoji": "⭐"},
    "copadobrasil":  {"nome": "Copa do Brasil",      "emoji": "🏆"},
    "sulamericana":  {"nome": "Sul-Americana",       "emoji": "🌎"},
    "libertadores":  {"nome": "Libertadores",        "emoji": "🌎"},
    "laliga":        {"nome": "La Liga",             "emoji": "🇪🇸"},
    "seriea":        {"nome": "Serie A",             "emoji": "🇮🇹"},
    "bundesliga":    {"nome": "Bundesliga",          "emoji": "🇩🇪"},
    "ligue1":        {"nome": "Ligue 1",             "emoji": "🇫🇷"},
}


# ==========================================
# PLAYER EMBED
# ==========================================

async def _montar_embed_player(event_id: str, slug: str, canal_iptv: str) -> tuple:
    """Retorna (discord.Embed, encerrado: bool) com dados ao vivo da partida."""
    loop = asyncio.get_event_loop()
    sumario = await loop.run_in_executor(None, buscar_partida_espn, slug, event_id)
    if not sumario:
        return discord.Embed(title="❌ Jogo não encontrado", color=0x6B7280), True

    header      = sumario.get("header", {})
    comp        = (header.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})

    nome_casa = (home.get("team") or {}).get("displayName", "?")
    nome_fora = (away.get("team") or {}).get("displayName", "?")
    g_casa    = _safe_score(home)
    g_fora    = _safe_score(away)

    status_obj  = comp.get("status") or {}
    status_type = status_obj.get("type") or {}
    state       = status_type.get("state") or "pre"
    detail      = status_obj.get("displayClock") or ""
    period      = status_type.get("shortDetail") or ""
    completed   = status_type.get("completed") or False

    meta       = _extrair_meta(comp)
    venue      = meta.get("venue", "")
    broadcasts = meta.get("broadcasts", [])

    if completed or state == "post":
        cor        = 0x6B7280
        status_str = "🏁 ENCERRADO"
    elif state == "in":
        cor        = 0xEF4444
        status_str = f"🔴 AO VIVO · {detail or period}"
    else:
        cor        = 0x3B82F6
        status_str = f"📅 EM BREVE · {period}"

    embed = discord.Embed(color=cor)
    embed.set_author(name=f"📺 {canal_iptv}  ·  {status_str}")

    if state == "in" or completed or state == "post":
        embed.title = f"{nome_casa}  {g_casa} – {g_fora}  {nome_fora}"
    else:
        embed.title = f"{nome_casa}  ×  {nome_fora}"
        embed.description = f"⏰ {period}"

    if venue:
        embed.add_field(name="🏟️ Estádio", value=venue, inline=True)
    if broadcasts:
        embed.add_field(name="📺 Onde assistir", value=", ".join(broadcasts), inline=True)

    embed.set_footer(text=f"Atualizado às {datetime.now(tz=BRT).strftime('%H:%M')} BRT")
    return embed, (completed or state == "post")


# ==========================================
# IPTV — parse M3U + busca de canal
# ==========================================
import time as _time

def _iptv_canais() -> list:
    """Baixa e parseia playlist M3U. Cache de 1h."""
    global _cache_canais_iptv, _ts_cache_iptv
    agora = _time.time()
    if _cache_canais_iptv and agora - _ts_cache_iptv < 3600:
        return _cache_canais_iptv
    try:
        r = requests.get(
            f"{IPTV_URL}/get.php",
            params={"username": IPTV_USER, "password": IPTV_PASS, "type": "m3u_plus", "output": "ts"},
            timeout=60,
        )
        canais = []
        lines  = r.text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("#EXTINF"):
                nome_m  = re.search(r'tvg-name="([^"]*)"', line)
                grupo_m = re.search(r'group-title="([^"]*)"', line)
                nome  = nome_m.group(1).strip()  if nome_m  else ""
                grupo = grupo_m.group(1).strip() if grupo_m else ""
                if i + 1 < len(lines):
                    url = lines[i + 1].strip()
                    sid = url.rstrip("/").split("/")[-1]
                    if nome and url.startswith("http"):
                        canais.append({"name": nome, "group": grupo, "stream_id": sid, "url": url})
                i += 2
            else:
                i += 1
        _cache_canais_iptv = canais
        _ts_cache_iptv = agora
        print(f"[IPTV] {len(canais)} canais carregados do M3U")
        return canais
    except Exception as e:
        print(f"[IPTV] Erro ao carregar M3U: {e}")
        return []


def _qualidade(nome: str) -> int:
    """Preferência de qualidade: FHD=3, HD=2, sem sufixo=1, SD=0."""
    n = nome.upper()
    if "FHD" in n or "4K" in n: return 3
    if " HD"  in n:              return 2
    if " SD"  in n:              return 0
    return 1


def _iptv_buscar_canal(nome: str) -> dict | None:
    """Localiza canal IPTV pelo nome com preferência por qualidade FHD > HD > SD."""
    canais = _iptv_canais()
    busca  = nome.lower().strip()

    candidatos = []

    # 1) nome exato
    for c in canais:
        if c["name"].lower() == busca:
            candidatos.append(c)
    # 2) busca contém o nome ou vice-versa
    if not candidatos:
        for c in canais:
            n = c["name"].lower()
            if busca in n or n in busca:
                candidatos.append(c)
    # 3) score por palavras
    if not candidatos:
        palavras = [p for p in busca.split() if len(p) > 2]
        melhor, score = None, 0
        for c in canais:
            n = c["name"].lower()
            s = sum(1 for p in palavras if p in n)
            if s > score:
                score, melhor = s, c
        return melhor if score > 0 else None

    # Ordena candidatos: prefer FHD, depois HD, depois sem sufixo, depois SD
    candidatos.sort(key=lambda c: _qualidade(c["name"]), reverse=True)
    return candidatos[0]


# ==========================================
# CACHE DE ESCUDOS (base64 embed — sem DNS no Playwright)
# ==========================================

def _baixar_logo_base64(url: str) -> str:
    if not url:
        return ""
    if url in _cache_logos:
        return _cache_logos[url]

    nome_cache = base64.urlsafe_b64encode(url.encode()).decode()[:80] + ".png"
    caminho_cache = os.path.join(PASTA_LOGOS, nome_cache)

    if os.path.exists(caminho_cache):
        with open(caminho_cache, "rb") as f:
            dados = f.read()
    else:
        try:
            resp = requests.get(url, timeout=7, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200 and resp.content:
                dados = resp.content
                with open(caminho_cache, "wb") as f:
                    f.write(dados)
            else:
                return ""
        except Exception:
            return ""

    resultado = f"data:image/png;base64,{base64.b64encode(dados).decode()}"
    _cache_logos[url] = resultado
    return resultado


async def _baixar_logos_paralelo(urls: list) -> dict:
    loop = asyncio.get_running_loop()
    resultados = await asyncio.gather(
        *[loop.run_in_executor(None, _baixar_logo_base64, url) for url in urls]
    )
    return dict(zip(urls, resultados))


def _img_tag(b64: str, nome: str, tamanho: int = 24) -> str:
    if b64:
        return f'<img src="{b64}" width="{tamanho}" height="{tamanho}" style="object-fit:contain;flex-shrink:0;">'
    return f'<span class="sigla">{nome[:2].upper()}</span>'


# ==========================================
# CAMADA ESPN
# ==========================================

def _espn_get(url: str, params: dict = None) -> dict | None:
    try:
        r = requests.get(url, params=params, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.json()
        print(f"[ESPN] {url} -> HTTP {r.status_code}")
    except Exception as e:
        print(f"[ESPN] Erro: {e}")
    return None


# ==========================================
# API-FOOTBALL — Copa do Brasil
# ==========================================

def _apifootball_get(endpoint: str, params: dict) -> dict | None:
    try:
        r = requests.get(
            f"{APIFOOTBALL_BASE}/{endpoint}",
            headers=APIFOOTBALL_HEADERS,
            params=params,
            timeout=10,
        )
        if r.status_code == 200:
            body = r.json()
            if body.get("errors"):
                print(f"[APIFoot] {endpoint} erros: {body['errors']}")
                return None
            return body
    except Exception as e:
        print(f"[APIFoot] Erro: {e}")
    return None


def buscar_rodada_copa_brasil() -> tuple[str, list]:
    """Retorna (nome_rodada, fixtures) da rodada atual/mais recente da Copa do Brasil 2024."""
    # Tenta rodada atual; se vazia, pega a última rodada da temporada
    for params in (
        {"league": 73, "season": 2024, "current": "true"},
        {"league": 73, "season": 2024},
    ):
        data = _apifootball_get("fixtures/rounds", params)
        if not data or not data.get("response"):
            continue
        rounds = data["response"]
        round_name = rounds[0] if params.get("current") else rounds[-1]
        fixtures_data = _apifootball_get(
            "fixtures", {"league": 73, "season": 2024, "round": round_name}
        )
        if fixtures_data and fixtures_data.get("response"):
            return (round_name, fixtures_data["response"])
    return ("", [])


def buscar_jogos_copa_hoje() -> list:
    """Fixtures de hoje na Copa do Brasil 2024 via API-Football."""
    data_hoje = datetime.now().strftime("%Y-%m-%d")
    data = _apifootball_get("fixtures", {"league": 73, "season": 2024, "date": data_hoje})
    return data.get("response", []) if data else []


def _stat(stats: list, name: str) -> int:
    for s in stats:
        if s.get("name") == name:
            try:
                return int(float(s.get("value", 0)))
            except (ValueError, TypeError):
                return 0
    return 0


def _safe_score(competitor: dict) -> int:
    try:
        return int(competitor.get("score", "0") or "0")
    except (ValueError, TypeError):
        return 0


def buscar_tabela(slug: str) -> list | None:
    data = _espn_get(f"{ESPN_V2}/{slug}/standings")
    if not data:
        return None
    try:
        # Estrutura real: data.children[0].standings.entries
        entries = []
        children = data.get("children", [])
        if children:
            entries = (children[0].get("standings") or {}).get("entries", [])

        # Fallback: data.standings.entries (algumas ligas menores)
        if not entries:
            entries = (data.get("standings") or {}).get("entries", [])

        if not entries:
            print(f"[ESPN] standings vazio para {slug}")
            return None

        resultado = []
        for i, entry in enumerate(entries, 1):
            team = entry.get("team", {})
            logos = team.get("logos", [])
            stats = entry.get("stats", [])
            resultado.append({
                "rank": i,
                "team": {
                    "name": team.get("displayName", ""),
                    "logo": logos[0]["href"] if logos else "",
                },
                "all": {
                    "played": _stat(stats, "gamesPlayed"),
                    "win":    _stat(stats, "wins"),
                    "draw":   _stat(stats, "ties"),
                    "lose":   _stat(stats, "losses"),
                },
                "goalsDiff": _stat(stats, "pointDifferential"),
                "points":    _stat(stats, "points"),
            })
        return resultado or None
    except Exception as e:
        print(f"[ESPN] Erro ao parsear standings {slug}: {e}")
        return None


def _extrair_meta(comp: dict) -> dict:
    """Extrai transmissão, estádio e odds de um objeto competition da ESPN."""
    broadcasts = []
    for b in (comp.get("geoBroadcasts") or []):
        media = b.get("media") or {}
        nome  = media.get("shortName", "") or media.get("callLetters", "")
        if nome and nome not in broadcasts:
            broadcasts.append(nome)
    if not broadcasts and comp.get("broadcast"):
        broadcasts = [comp["broadcast"]]

    v       = comp.get("venue") or {}
    addr    = v.get("address") or {}
    cidade  = addr.get("city", "")
    estadio = v.get("fullName", "")
    venue   = f"{estadio}, {cidade}".strip(", ") if estadio else ""

    odds_raw = ((comp.get("odds") or [{}])[0]) or {}
    odds_str = odds_raw.get("details", "")

    return {"broadcasts": broadcasts, "venue": venue, "odds": odds_str}


# ==========================================
# TV — mapeamento por competição (ESPN slug -> canais BR)
# ==========================================

_TV_POR_LIGA: dict[str, list[str]] = {
    "BRA.1":                    ["Globo", "SporTV", "SporTV 2", "SporTV 3", "Premiere", "Cazé TV"],
    "CONMEBOL.LIBERTADORES":    ["ESPN", "ESPN 2", "ESPN 3", "Disney+", "SBT"],
    "CONMEBOL.SUDAMERICANA":    ["ESPN", "ESPN 2", "ESPN 3", "Disney+"],
    "UEFA.CHAMPIONS":           ["TNT", "MAX", "SBT"],
    "UEFA.EUROPA":              ["TNT", "MAX"],
    "UEFA.EUROPA.CONFERENCE":   ["TNT", "MAX"],
    "eng.1":                    ["ESPN", "ESPN 2", "ESPN 3", "Disney+", "Star+"],
    "ESP.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+"],
    "ITA.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+"],
    "GER.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+", "RedeTV"],
    "FRA.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+"],
}


def _canais_tv(jogo: dict, slug: str = "") -> list[str]:
    """Retorna lista de canais ESPN (da API) + mapeamento fixo por liga."""
    espn = list((jogo.get("meta") or {}).get("broadcasts", []))
    fixos = _TV_POR_LIGA.get(slug, [])
    for c in fixos:
        if c not in espn:
            espn.append(c)
    return espn



def buscar_artilheiros(slug: str) -> list:
    """Retorna top artilheiros de uma liga via ESPN leaders endpoint."""
    data = _espn_get(f"{ESPN_V1}/{slug}/leaders")
    if not data:
        return []
    try:
        for categoria in data.get("leaders", []):
            nome_cat = categoria.get("name", "").lower()
            if "goal" in nome_cat or "score" in nome_cat or "gol" in nome_cat:
                return categoria.get("leaders", [])
        # Fallback: primeira categoria disponível
        cats = data.get("leaders", [])
        return cats[0].get("leaders", []) if cats else []
    except Exception as e:
        print(f"[ESPN] Erro leaders {slug}: {e}")
        return []


def buscar_time_id(slug: str, nome_time: str) -> tuple | None:
    """Busca team_id e nome oficial pelo nome parcial."""
    data = _espn_get(f"{ESPN_V1}/{slug}/teams", {"limit": 100})
    if not data:
        return None
    busca = nome_time.lower()
    melhor = None
    melhor_score = 0
    for t in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
        team = t.get("team", {})
        nomes = [
            team.get("displayName", "").lower(),
            team.get("shortDisplayName", "").lower(),
            team.get("abbreviation", "").lower(),
            team.get("nickname", "").lower(),
        ]
        for n in nomes:
            if busca in n or n in busca:
                score = len(set(busca) & set(n))
                if score > melhor_score:
                    melhor_score = score
                    melhor = (str(team.get("id", "")), team.get("displayName", nome_time))
    return melhor


def buscar_proximos_jogos(slug: str, team_id: str) -> list:
    """Retorna próximos fixtures de um time."""
    data = _espn_get(f"{ESPN_V1}/{slug}/teams/{team_id}/schedule")
    if not data:
        return []
    try:
        hoje = datetime.now(tz=BRT).date()
        proximos = []
        for ev in data.get("events", []):
            date_str = ev.get("date", "")
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(BRT)
                if dt.date() < hoje:
                    continue
            except Exception:
                continue
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            proximos.append({
                "fixture": {
                    "id": ev.get("id", "0"),
                    "date": date_str,
                    "status": {"short": "NS", "elapsed": None},
                },
                "teams": {
                    "home": {"name": (home.get("team") or {}).get("displayName", ""), "logo": (home.get("team") or {}).get("logo", "")},
                    "away": {"name": (away.get("team") or {}).get("displayName", ""), "logo": (away.get("team") or {}).get("logo", "")},
                },
                "goals": {"home": None, "away": None},
                "meta": _extrair_meta(comp),
            })
            if len(proximos) == 5:
                break
        return proximos
    except Exception as e:
        print(f"[ESPN] Erro schedule {team_id}: {e}")
        return []


def _e_hoje(date_str: str) -> bool:
    """Retorna True se a data do jogo (UTC) cair no dia de hoje em BRT."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(BRT).date() == datetime.now(tz=BRT).date()
    except Exception:
        return True   # em caso de erro, inclui o jogo


def buscar_jogos_do_dia(slug: str) -> list:
    data = _espn_get(f"{ESPN_V1}/{slug}/scoreboard")
    if not data:
        return []
    try:
        jogos = []
        for ev in data.get("events", []):
            # Filtra jogos que não são de hoje (ESPN às vezes devolve próximo jogo agendado)
            if not _e_hoje(ev.get("date", "")):
                continue
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})

            status      = ev.get("status") or {}
            status_type = status.get("type") or {}
            state       = status_type.get("state") or "pre"
            type_name   = status_type.get("name") or ""

            if state == "pre":
                short = "NS"
            elif state == "post":
                short = "FT"
            elif type_name == "STATUS_HALFTIME":
                short = "HT"
            else:
                short = "1H"

            elapsed = None
            if state == "in" and short != "HT":
                clock = status.get("displayClock") or ""
                try:
                    elapsed = int(clock.split(":")[0])
                except Exception:
                    pass

            home_team = (home.get("team") or {})
            away_team = (away.get("team") or {})

            jogos.append({
                "fixture": {
                    "id": ev.get("id", "0"),
                    "date": ev.get("date", ""),
                    "status": {"short": short, "elapsed": elapsed},
                },
                "teams": {
                    "home": {
                        "name": home_team.get("displayName", ""),
                        "logo": home_team.get("logo", ""),
                    },
                    "away": {
                        "name": away_team.get("displayName", ""),
                        "logo": away_team.get("logo", ""),
                    },
                },
                "goals": {
                    "home": _safe_score(home),
                    "away": _safe_score(away),
                },
                "meta": _extrair_meta(comp),
            })
        return jogos
    except Exception as e:
        print(f"[ESPN] Erro scoreboard {slug}: {e}")
        return []


def buscar_partida_espn(slug: str, event_id: str) -> dict | None:
    return _espn_get(f"{ESPN_V1}/{slug}/summary", {"event": event_id})


# ==========================================
# RENDERIZAÇÃO HTML -> PNG
# ==========================================

async def _html_para_png(html: str, nome_arquivo: str, width: int = 700) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_viewport_size({"width": width, "height": 1600})
        await page.set_content(html, wait_until="domcontentloaded")
        await page.wait_for_timeout(150)
        corpo = await page.query_selector("body")
        await corpo.screenshot(path=nome_arquivo)
        await browser.close()
    return nome_arquivo


async def gerar_mata_mata_png(fixtures: list, titulo: str, rodada: str = "") -> str:
    """Gera imagem estilo mata-mata para competições de copa (sem tabela de pontos)."""
    urls_unicas = list({
        url
        for f in fixtures
        for url in (f["teams"]["home"].get("logo", ""), f["teams"]["away"].get("logo", ""))
        if url
    })
    logos = await _baixar_logos_paralelo(urls_unicas) if urls_unicas else {}

    cards = ""
    for f in fixtures:
        status = f["fixture"]["status"]["short"]
        elapsed = f["fixture"]["status"].get("elapsed") or ""

        try:
            dt = datetime.fromisoformat(f["fixture"]["date"].replace("Z", "+00:00"))
            horario = dt.astimezone(timezone(timedelta(hours=-3))).strftime("%d/%m %H:%M")
        except Exception:
            horario = "--/-- --:--"

        nome_casa = f["teams"]["home"]["name"]
        nome_fora = f["teams"]["away"]["name"]
        b64_casa = logos.get(f["teams"]["home"].get("logo", ""), "")
        b64_fora = logos.get(f["teams"]["away"].get("logo", ""), "")
        g_casa = f["goals"]["home"]
        g_fora = f["goals"]["away"]

        if status in ("NS", "TBD"):
            placar_html = f'<div class="hora">{horario}</div>'
            cls_card = ""
        elif status in ("FT", "AET", "PEN"):
            label = {"FT": "Enc.", "AET": "Prorrg.", "PEN": "Pên."}.get(status, status)
            placar_html = (
                f'<div class="placar">{g_casa} <span class="sep">×</span> {g_fora}</div>'
                f'<div class="label-enc">{label}</div>'
            )
            cls_card = " enc"
        elif status == "HT":
            placar_html = (
                f'<div class="placar live">{g_casa} <span class="sep">×</span> {g_fora}</div>'
                f'<div class="label-live">Intervalo</div>'
            )
            cls_card = " live"
        else:
            placar_html = (
                f'<div class="placar live">{g_casa} <span class="sep">×</span> {g_fora}</div>'
                f'<div class="label-live">{elapsed}\' 🔴</div>'
            )
            cls_card = " live"

        cards += (
            f'<div class="card{cls_card}">'
            f'  <div class="lado casa">'
            f'    <span class="nome">{nome_casa}</span>'
            f'    {_img_tag(b64_casa, nome_casa, 36)}'
            f'  </div>'
            f'  <div class="centro">{placar_html}</div>'
            f'  <div class="lado fora">'
            f'    {_img_tag(b64_fora, nome_fora, 36)}'
            f'    <span class="nome">{nome_fora}</span>'
            f'  </div>'
            f'</div>'
        )

    rodada_html = f'<div class="rodada">{rodada}</div>' if rodada else ""

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;width:620px;padding:22px}
.titulo{font-size:20px;font-weight:700;color:#fff;margin-bottom:4px}
.rodada{font-size:13px;color:#8892a4;margin-bottom:16px;text-transform:uppercase;letter-spacing:.8px}
.card{display:flex;align-items:center;background:#1a2a5e;margin-bottom:10px;
      padding:16px 14px;border-radius:12px;gap:8px}
.card.enc{background:#1a2540}
.card.live{background:#2a1a3e;border:1px solid #6d28d9}
.lado{display:flex;align-items:center;gap:10px;flex:1}
.lado.casa{justify-content:flex-end;text-align:right;flex-direction:row}
.lado.fora{justify-content:flex-start;text-align:left}
.nome{font-size:13px;font-weight:600;line-height:1.3}
.centro{width:130px;text-align:center;flex-shrink:0}
.hora{color:#93c5fd;font-size:15px;font-weight:600}
.placar{font-size:26px;font-weight:800;color:#fff;letter-spacing:2px}
.placar.live{color:#f87171}
.sep{color:#8892a4;font-weight:400;font-size:20px}
.label-enc{font-size:10px;color:#8892a4;margin-top:3px;text-transform:uppercase;letter-spacing:.7px}
.label-live{font-size:11px;color:#f87171;margin-top:3px;font-weight:600}
.sigla{background:#0f3460;color:#ccc;padding:3px 7px;border-radius:4px;font-size:11px;font-weight:700}
.vazio{color:#8892a4;text-align:center;padding:40px;font-size:14px}
"""

    conteudo = cards if cards else '<div class="vazio">Nenhum confronto encontrado.</div>'
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
        f'<div class="titulo">🏆 {titulo}</div>'
        f'{rodada_html}'
        f'{conteudo}'
        f'</body></html>'
    )

    return await _html_para_png(html, "mata_mata_temp.png", width=660)


# Zonas por liga: (limite_superior, classe_css, cor_hex, legenda)
# limite pode ser int (top-N) ou "bN" (bottom-N)
_ZONAS_LIGA: dict[str, list] = {
    "BRA.1": [
        (4,    "za", "#3b82f6", "Libertadores"),
        (6,    "zb", "#22c55e", "Sul-americana"),
        ("b4", "zc", "#ef4444", "Rebaixamento"),
    ],
    "eng.1": [
        (4,    "za", "#3b82f6", "Champions League"),
        (6,    "zb", "#22c55e", "Europa League"),
        (7,    "zd", "#a78bfa", "Conference League"),
        ("b3", "zc", "#ef4444", "Rebaixamento"),
    ],
    "ESP.1": [
        (4,    "za", "#3b82f6", "Champions League"),
        (6,    "zb", "#22c55e", "Europa League"),
        (7,    "zd", "#a78bfa", "Conference League"),
        ("b3", "zc", "#ef4444", "Rebaixamento"),
    ],
    "ITA.1": [
        (4,    "za", "#3b82f6", "Champions League"),
        (6,    "zb", "#22c55e", "Europa League"),
        (7,    "zd", "#a78bfa", "Conference League"),
        ("b3", "zc", "#ef4444", "Rebaixamento"),
    ],
    "GER.1": [
        (4,    "za", "#3b82f6", "Champions League"),
        (6,    "zb", "#22c55e", "Europa League"),
        (7,    "zd", "#a78bfa", "Conference League"),
        ("b2", "zc", "#ef4444", "Rebaixamento"),
    ],
    "FRA.1": [
        (3,    "za", "#3b82f6", "Champions League"),
        (5,    "zb", "#22c55e", "Europa League"),
        (6,    "zd", "#a78bfa", "Conference League"),
        ("b3", "zc", "#ef4444", "Rebaixamento"),
    ],
    "UEFA.CHAMPIONS": [
        (8,    "za", "#3b82f6", "Oitavas diretas"),
        (24,   "zb", "#f59e0b", "Playoffs"),
        ("b12","zc", "#ef4444", "Eliminado"),
    ],
    "UEFA.EUROPA": [
        (8,    "za", "#3b82f6", "16 avos diretos"),
        (24,   "zb", "#f59e0b", "Playoffs"),
        ("b8", "zc", "#ef4444", "Eliminado"),
    ],
    "CONMEBOL.LIBERTADORES": [
        (8,    "za", "#3b82f6", "Oitavas diretas"),
        (24,   "zb", "#f59e0b", "Playoffs"),
        ("b12","zc", "#ef4444", "Eliminado"),
    ],
    "CONMEBOL.SUDAMERICANA": [
        (8,    "za", "#3b82f6", "16 avos diretos"),
        (24,   "zb", "#f59e0b", "Playoffs"),
        ("b8", "zc", "#ef4444", "Eliminado"),
    ],
}


def _zona_rank(rank: int, total: int, zonas: list) -> str:
    for limite, cls, *_ in zonas:
        if isinstance(limite, str):
            n = int(limite[1:])
            if rank > total - n:
                return cls
        elif rank <= limite:
            return cls
    return ""


async def gerar_tabela_png(dados: list, nome_liga: str, slug: str = "") -> str:
    urls_logos = [t["team"].get("logo", "") for t in dados]
    logos = await _baixar_logos_paralelo(urls_logos)

    total  = len(dados)
    zonas  = _ZONAS_LIGA.get(slug, _ZONAS_LIGA["BRA.1"])
    linhas = ""

    # CSS dinâmico para as cores das zonas
    css_zonas = ""
    for limite, cls, cor, _ in zonas:
        css_zonas += f"tr.{cls} td:first-child{{border-left:3px solid {cor}}}\n"

    for time in dados:
        rank  = time["rank"]
        nome  = time["team"]["name"]
        b64   = logos.get(time["team"].get("logo", ""), "")
        zona  = _zona_rank(rank, total, zonas)
        sg    = time["goalsDiff"]
        sg_str = f"+{sg}" if sg > 0 else str(sg)

        linhas += (
            f'<tr class="{zona}">'
            f'<td class="pos">{rank}</td>'
            f'<td class="time-col">{_img_tag(b64, nome, 22)}<span>{nome}</span></td>'
            f'<td>{time["all"]["played"]}</td>'
            f'<td>{time["all"]["win"]}</td>'
            f'<td>{time["all"]["draw"]}</td>'
            f'<td>{time["all"]["lose"]}</td>'
            f'<td>{sg_str}</td>'
            f'<td class="pts">{time["points"]}</td>'
            f'</tr>'
        )

    legenda_html = "".join(
        f'<div class="leg"><div class="dot" style="background:{cor}"></div>{label}</div>'
        for _, _, cor, label in zonas
    )

    css = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;padding:22px;width:660px}}
.titulo{{font-size:20px;font-weight:700;color:#fff;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{color:#8892a4;font-weight:500;padding:7px 8px;border-bottom:2px solid #0f3460;
   text-align:center;font-size:11px;text-transform:uppercase;letter-spacing:.7px}}
th:nth-child(2){{text-align:left;padding-left:10px}}
td{{padding:9px 8px;border-bottom:1px solid #1e2f5e;text-align:center;vertical-align:middle}}
td.time-col{{text-align:left;display:flex;align-items:center;gap:9px;padding-left:10px}}
td.pos{{color:#8892a4;font-size:12px;width:30px}}
td.pts{{font-weight:700;color:#fff;font-size:14px}}
tr:hover td{{background:#1a2a5e}}
.sigla{{background:#0f3460;color:#ccc;padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}}
{css_zonas}
.legenda{{display:flex;gap:18px;margin-top:13px;font-size:11px;color:#8892a4;flex-wrap:wrap}}
.leg{{display:flex;align-items:center;gap:5px}}
.dot{{width:10px;height:10px;border-radius:2px}}
"""

    ano = datetime.now().year
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
        f'<div class="titulo">🏆 {nome_liga} — Classificação {ano}</div>'
        f'<table><thead><tr>'
        f'<th>#</th><th style="text-align:left;padding-left:10px">Clube</th>'
        f'<th>PJ</th><th>V</th><th>E</th><th>D</th><th>SG</th><th>PTS</th>'
        f'</tr></thead><tbody>{linhas}</tbody></table>'
        f'<div class="legenda">{legenda_html}</div>'
        f'</body></html>'
    )

    nome_arquivo = f"tabela_{nome_liga.lower().replace(' ', '')}.png"
    return await _html_para_png(html, nome_arquivo, width=700)


async def gerar_jogos_png(jogos: list, titulo: str) -> str:
    urls_unicas = list({
        url
        for j in jogos
        for url in (j["teams"]["home"].get("logo", ""), j["teams"]["away"].get("logo", ""))
        if url
    })
    logos = await _baixar_logos_paralelo(urls_unicas) if urls_unicas else {}

    cards = ""
    for j in jogos:
        status = j["fixture"]["status"]["short"]
        elapsed = j["fixture"]["status"].get("elapsed") or ""
        event_id = j["fixture"]["id"]

        try:
            dt = datetime.fromisoformat(j["fixture"]["date"].replace("Z", "+00:00"))
            horario = dt.astimezone(timezone(timedelta(hours=-3))).strftime("%d/%m %H:%M")
        except Exception:
            horario = "--/-- --:--"

        nome_casa = j["teams"]["home"]["name"]
        nome_fora = j["teams"]["away"]["name"]
        b64_casa = logos.get(j["teams"]["home"].get("logo", ""), "")
        b64_fora = logos.get(j["teams"]["away"].get("logo", ""), "")
        g_casa = j["goals"]["home"]
        g_fora = j["goals"]["away"]

        if status in ("NS", "TBD"):
            mid = f'<div class="hora">{horario}</div>'
        elif status in ("FT", "AET", "PEN"):
            label = {"FT": "Encerrado", "AET": "Prorrogacao", "PEN": "Penaltis"}.get(status, status)
            mid = f'<div class="placar enc">{g_casa} — {g_fora}</div><div class="label">{label}</div>'
        elif status == "HT":
            mid = f'<div class="placar vivo">{g_casa} — {g_fora}</div><div class="label inter">Intervalo</div>'
        else:
            min_str = f"{elapsed}'" if elapsed else "AO VIVO"
            mid = f'<div class="placar vivo">{g_casa} — {g_fora}</div><div class="label live">{min_str}</div>'

        cards += (
            f'<div class="card">'
            f'<div class="time home">'
            f'<span class="nome">{nome_casa}</span>'
            f'{_img_tag(b64_casa, nome_casa, 30)}</div>'
            f'<div class="mid">{mid}</div>'
            f'<div class="time away">'
            f'{_img_tag(b64_fora, nome_fora, 30)}'
            f'<span class="nome">{nome_fora}</span></div>'
            f'<div class="event-id">ID: {event_id}</div>'
            f'</div>'
        )

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;width:600px;padding:20px}
.titulo{font-size:18px;font-weight:700;color:#fff;border-bottom:2px solid #0f3460;
        padding-bottom:10px;margin-bottom:14px}
.card{display:flex;align-items:center;background:#1a2a5e;margin-bottom:8px;
      padding:13px 14px;border-radius:10px;gap:6px;position:relative}
.time{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.home{justify-content:flex-end;text-align:right}
.away{justify-content:flex-start;text-align:left}
.nome{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mid{width:120px;flex-shrink:0;display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.3}
.hora{color:#93c5fd;font-size:18px;font-weight:700}
.placar{font-size:20px;font-weight:800}
.placar.enc{color:#e0e0e0}
.placar.vivo{color:#f87171}
.label{font-size:10px;color:#8892a4;margin-top:2px;text-transform:uppercase;letter-spacing:.7px}
.label.live{color:#f87171;font-weight:600}
.label.inter{color:#fbbf24}
.sigla{background:#0f3460;color:#ccc;padding:3px 6px;border-radius:4px;font-size:11px;font-weight:700}
.event-id{position:absolute;bottom:3px;right:8px;font-size:9px;color:#4a5a8a}
.vazio{color:#8892a4;text-align:center;padding:30px;font-size:14px}
"""

    conteudo = cards if cards else '<div class="vazio">Nenhum jogo agendado para hoje.</div>'
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
        f'<div class="titulo">📅 {titulo}</div>'
        f'{conteudo}'
        f'</body></html>'
    )

    return await _html_para_png(html, "jogos_temp.png", width=640)


async def gerar_artilheiro_png(artilheiros: list, nome_liga: str) -> str:
    linhas = ""
    for i, art in enumerate(artilheiros[:15], 1):
        atleta = art.get("athlete", {})
        nome   = atleta.get("displayName", "?")
        time   = (art.get("team") or {}).get("displayName", "")
        gols   = int(float(art.get("value", 0)))
        logo_url = (art.get("team") or {}).get("logo", "") or (atleta.get("headshot") or {}).get("href", "")
        b64 = _baixar_logo_base64(logo_url)
        img  = f'<img src="{b64}" width="22" height="22" style="object-fit:contain">' if b64 else f'<span class="sigla">{time[:2].upper()}</span>'
        destaque = ' class="top"' if i == 1 else ""
        linhas += (
            f'<tr{destaque}>'
            f'<td class="pos">{i}</td>'
            f'<td class="atleta">{img}<span>{nome}</span></td>'
            f'<td class="clube">{time}</td>'
            f'<td class="gols">{gols}</td>'
            f'</tr>'
        )

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;padding:22px;width:560px}
.titulo{font-size:20px;font-weight:700;color:#fff;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:#8892a4;font-weight:500;padding:7px 8px;border-bottom:2px solid #0f3460;
   font-size:11px;text-transform:uppercase;letter-spacing:.7px;text-align:center}
th:nth-child(2),th:nth-child(3){text-align:left}
td{padding:9px 8px;border-bottom:1px solid #1e2f5e;text-align:center;vertical-align:middle}
td.atleta{display:flex;align-items:center;gap:9px;text-align:left;font-weight:500}
td.clube{color:#8892a4;font-size:12px;text-align:left}
td.pos{color:#8892a4;font-size:12px;width:28px}
td.gols{font-size:16px;font-weight:800;color:#f59e0b}
tr.top td{background:#1a2a5e}
tr.top td.gols{color:#fbbf24;font-size:18px}
.sigla{background:#0f3460;color:#ccc;padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}
"""
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{css}</style></head><body>'
        f'<div class="titulo">🥇 Artilheiros — {nome_liga}</div>'
        f'<table><thead><tr><th>#</th><th>Atleta</th><th>Clube</th><th>⚽</th></tr></thead>'
        f'<tbody>{linhas}</tbody></table></body></html>'
    )
    return await _html_para_png(html, "artilheiro_temp.png", width=600)


async def gerar_proximos_png(jogos: list, nome_time: str, nome_liga: str) -> str:
    urls = list({
        url for j in jogos
        for url in (j["teams"]["home"].get("logo",""), j["teams"]["away"].get("logo","")) if url
    })
    logos = await _baixar_logos_paralelo(urls) if urls else {}

    cards = ""
    for j in jogos:
        try:
            dt = datetime.fromisoformat(j["fixture"]["date"].replace("Z","+00:00")).astimezone(BRT)
            data_hora = dt.strftime("%d/%m %H:%M")
        except Exception:
            data_hora = "--/-- --:--"

        nome_casa = j["teams"]["home"]["name"]
        nome_fora = j["teams"]["away"]["name"]
        b64_casa  = logos.get(j["teams"]["home"].get("logo",""),"")
        b64_fora  = logos.get(j["teams"]["away"].get("logo",""),"")
        destaque  = nome_time.lower() in nome_casa.lower() or nome_time.lower() in nome_fora.lower()

        cards += (
            f'<div class="card{"destaque" if destaque else ""}">'
            f'<div class="lado home"><span class="tnome">{nome_casa}</span>'
            f'{_img_tag(b64_casa, nome_casa, 26)}</div>'
            f'<div class="centro"><span class="hora">{data_hora}</span></div>'
            f'<div class="lado away">{_img_tag(b64_fora, nome_fora, 26)}'
            f'<span class="tnome">{nome_fora}</span></div>'
            f'</div>'
        )

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;width:560px;padding:20px}
.titulo{font-size:18px;font-weight:700;color:#fff;margin-bottom:4px}
.sub{font-size:12px;color:#8892a4;margin-bottom:14px}
.card{display:flex;align-items:center;background:#1a2a5e;margin-bottom:7px;
      padding:11px 14px;border-radius:9px;gap:6px}
.carddestaque{border-left:3px solid #3b82f6;background:#1e3270}
.lado{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.home{justify-content:flex-end;text-align:right}
.away{justify-content:flex-start;text-align:left}
.tnome{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}
.centro{width:110px;text-align:center;flex-shrink:0}
.hora{color:#93c5fd;font-size:14px;font-weight:700}
.sigla{background:#0f3460;color:#ccc;padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}
"""
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{css}</style></head><body>'
        f'<div class="titulo">📅 Próximos jogos — {nome_time}</div>'
        f'<div class="sub">{nome_liga}</div>'
        f'{cards}</body></html>'
    )
    return await _html_para_png(html, "proximos_temp.png", width=600)


# ==========================================
# RESUMO DIÁRIO — imagem consolidada de todas as ligas
# ==========================================

async def gerar_resumo_diario_png(jogos_por_liga: dict) -> str:
    """
    jogos_por_liga: {"brasileirao": [...], "premierleague": [...], ...}
    Gera uma única imagem com todos os jogos do dia agrupados por liga.
    """
    # Coleta todas as URLs de logos de uma vez e baixa em paralelo
    todas_urls = list({
        url
        for jogos in jogos_por_liga.values()
        for j in jogos
        for url in (j["teams"]["home"].get("logo", ""), j["teams"]["away"].get("logo", ""))
        if url
    })
    logos = await _baixar_logos_paralelo(todas_urls) if todas_urls else {}

    secoes_html = ""
    total_jogos = 0

    for chave, jogos in jogos_por_liga.items():
        if not jogos:
            continue
        total_jogos += len(jogos)
        meta = LIGAS_META.get(chave, {"nome": chave.title(), "emoji": "🏟️"})

        cards = ""
        for j in jogos:
            status = j["fixture"]["status"]["short"]
            elapsed = j["fixture"]["status"].get("elapsed") or ""

            try:
                dt = datetime.fromisoformat(j["fixture"]["date"].replace("Z", "+00:00"))
                horario = dt.astimezone(BRT).strftime("%H:%M")
            except Exception:
                horario = "--:--"

            nome_casa = j["teams"]["home"]["name"]
            nome_fora = j["teams"]["away"]["name"]
            b64_casa  = logos.get(j["teams"]["home"].get("logo", ""), "")
            b64_fora  = logos.get(j["teams"]["away"].get("logo", ""), "")
            g_casa = j["goals"]["home"]
            g_fora = j["goals"]["away"]

            if status in ("NS", "TBD"):
                mid = f'<span class="hora">{horario}</span>'
            elif status in ("FT", "AET", "PEN"):
                label = {"FT": "FIM", "AET": "PRORRG", "PEN": "PEN"}.get(status, status)
                mid = f'<span class="placar enc">{g_casa}–{g_fora}</span><br><span class="lbl">{label}</span>'
            elif status == "HT":
                mid = f'<span class="placar vivo">{g_casa}–{g_fora}</span><br><span class="lbl inter">INTERV.</span>'
            else:
                min_str = f"{elapsed}'" if elapsed else "AO VIVO"
                mid = f'<span class="placar vivo">{g_casa}–{g_fora}</span><br><span class="lbl live">{min_str}</span>'

            cards += (
                f'<div class="jogo">'
                f'  <div class="lado home">'
                f'    <span class="tnome">{nome_casa}</span>'
                f'    {_img_tag(b64_casa, nome_casa, 22)}'
                f'  </div>'
                f'  <div class="mid">{mid}</div>'
                f'  <div class="lado away">'
                f'    {_img_tag(b64_fora, nome_fora, 22)}'
                f'    <span class="tnome">{nome_fora}</span>'
                f'  </div>'
                f'</div>'
            )

        secoes_html += (
            f'<div class="secao">'
            f'  <div class="liga-titulo">{meta["emoji"]} {meta["nome"]}</div>'
            f'  {cards}'
            f'</div>'
        )

    if not secoes_html:
        secoes_html = '<div class="vazio">Nenhum jogo encontrado para hoje em nenhuma liga.</div>'

    data_hoje = datetime.now(tz=BRT).strftime("%A, %d de %B de %Y").capitalize()

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;width:680px;padding:22px}
.cabecalho{text-align:center;margin-bottom:20px;padding-bottom:14px;border-bottom:2px solid #0f3460}
.cabecalho .titulo{font-size:22px;font-weight:800;color:#fff}
.cabecalho .data{font-size:13px;color:#8892a4;margin-top:4px}
.secao{margin-bottom:18px}
.liga-titulo{font-size:14px;font-weight:700;color:#93c5fd;text-transform:uppercase;
             letter-spacing:.9px;margin-bottom:7px;padding-left:4px;
             border-left:3px solid #3b82f6;padding-left:8px}
.jogo{display:flex;align-items:center;background:#1a2a5e;margin-bottom:5px;
      padding:9px 12px;border-radius:8px;gap:6px}
.lado{display:flex;align-items:center;gap:7px;flex:1;min-width:0}
.lado.home{justify-content:flex-end;text-align:right}
.lado.away{justify-content:flex-start;text-align:left}
.tnome{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
.mid{width:100px;text-align:center;flex-shrink:0;line-height:1.3}
.hora{color:#93c5fd;font-size:15px;font-weight:700}
.placar{font-size:17px;font-weight:800}
.placar.enc{color:#e0e0e0}
.placar.vivo{color:#f87171}
.lbl{font-size:9px;color:#8892a4;text-transform:uppercase;letter-spacing:.6px}
.lbl.live{color:#f87171}
.lbl.inter{color:#fbbf24}
.sigla{background:#0f3460;color:#ccc;padding:2px 5px;border-radius:3px;font-size:10px;font-weight:700}
.vazio{color:#8892a4;text-align:center;padding:40px;font-size:14px}
"""

    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
        f'<div class="cabecalho">'
        f'  <div class="titulo">⚽ Jogos do Dia</div>'
        f'  <div class="data">{data_hoje} · {total_jogos} jogos em {len(jogos_por_liga)} ligas</div>'
        f'</div>'
        f'{secoes_html}'
        f'</body></html>'
    )

    return await _html_para_png(html, "resumo_diario.png", width=720)


# ==========================================
# SERVIDOR WEB — player HLS embutido
# ==========================================

_PLAYER_HTML = """\
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@latest/dist/mpegts.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;
      min-height:100vh;display:flex;flex-direction:column;align-items:center;
      justify-content:center;gap:14px;padding:16px}}
h2{{font-size:18px;color:#93c5fd;text-align:center;max-width:800px}}
video{{width:100%;max-width:1280px;background:#000;border-radius:10px;outline:none}}
#status{{font-size:13px;color:#8892a4;text-align:center}}
</style>
</head>
<body>
<h2>⚽ {title}</h2>
<video id="v" controls autoplay playsinline></video>
<div id="status">Conectando...</div>
<script>
const TOKEN    = location.pathname.split("/").pop();
const proxyUrl = "{server_url}/proxy/" + TOKEN;
const isHLS    = "{stream_url}".includes(".m3u8");
const v        = document.getElementById("v");
const st       = document.getElementById("status");

function setStatus(msg) {{ st.textContent = msg; }}

if (isHLS && Hls.isSupported()) {{
    const hls = new Hls({{ enableWorker: true, lowLatencyMode: true }});
    hls.loadSource(proxyUrl);
    hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {{ v.play(); setStatus("🔴 Ao vivo"); }});
    hls.on(Hls.Events.ERROR, (_, d) => {{
        if (d.fatal) setStatus("❌ " + d.details);
        console.error("[HLS]", d);
    }});
}} else if (mpegts.isSupported()) {{
    const player = mpegts.createPlayer({{
        type: "mpegts", isLive: true, url: proxyUrl,
    }}, {{ enableWorker: true, lazyLoadMaxDuration: 180, seekType: "range" }});
    player.attachMediaElement(v);
    player.load();
    player.play();
    player.on(mpegts.Events.MEDIA_INFO,  ()     => setStatus("🔴 Ao vivo"));
    player.on(mpegts.Events.ERROR,       (t, d) => {{
        setStatus("❌ " + t + ": " + JSON.stringify(d));
        console.error("[MPEGTS]", t, d);
    }});
}} else {{
    setStatus("❌ Navegador sem suporte a streams. Use Chrome.");
}}
</script>
</body>
</html>"""

_CORS_HEADERS = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Cache-Control":                "no-cache, no-store",
}


async def _web_player_handler(request: aiohttp_web.Request) -> aiohttp_web.Response:
    token = request.match_info.get("token", "")
    sessao = _player_sessions.get(token)
    if not sessao:
        return aiohttp_web.Response(
            text="<h2>⛔ Link expirado ou inválido.</h2>",
            content_type="text/html", status=410,
        )
    html = _PLAYER_HTML.format(
        title=sessao["title"],
        stream_url=sessao["stream_url"],
        server_url=SERVER_URL,
    )
    return aiohttp_web.Response(text=html, content_type="text/html", charset="utf-8")


async def _proxy_handler(request: aiohttp_web.Request) -> aiohttp_web.Response:
    """Proxy streaming — resolve CORS, pipe de chunks para TS ao vivo."""
    token = request.match_info.get("token", "")
    # Suporta segmentos TS repassados com ?url= (reescritos do M3U8)
    target = request.query.get("url", "")

    if token and not target:
        sessao = _player_sessions.get(token)
        if not sessao:
            return aiohttp_web.Response(text="Sessão expirada.", status=410, headers=_CORS_HEADERS)
        target = sessao["stream_url"]

    if not target:
        return aiohttp_web.Response(text="url ausente", status=400)

    print(f"[Proxy] → {target}")
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        # sem total timeout — stream vive enquanto o usuário assiste
        timeout = aiohttp.ClientTimeout(connect=10, sock_connect=10, sock_read=60)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(target) as resp:
                ct = resp.content_type or "application/octet-stream"
                print(f"[Proxy] ← HTTP {resp.status} | {ct} | {target}")

                # M3U8: bufferiza e reescreve URLs dos segmentos
                if "mpegurl" in ct or target.split("?")[0].endswith(".m3u8"):
                    raw  = await resp.read()
                    base = target.rsplit("/", 1)[0] + "/"
                    lines = []
                    for line in raw.decode("utf-8", errors="replace").splitlines():
                        s = line.strip()
                        if s and not s.startswith("#"):
                            seg = s if s.startswith("http") else base + s
                            lines.append(f"{SERVER_URL}/proxy?url={url_quote(seg, safe='')}")
                        else:
                            lines.append(line)
                    return aiohttp_web.Response(
                        body="\n".join(lines).encode(),
                        content_type="application/vnd.apple.mpegurl",
                        headers=_CORS_HEADERS,
                    )

                # TS / stream contínuo: pipe em chunks (nunca bufferiza tudo)
                stream_resp = aiohttp_web.StreamResponse(headers={
                    **_CORS_HEADERS,
                    "Content-Type": ct,
                    "Transfer-Encoding": "chunked",
                })
                await stream_resp.prepare(request)
                async for chunk in resp.content.iter_chunked(32768):
                    await stream_resp.write(chunk)
                return stream_resp

    except Exception as e:
        print(f"[Proxy] Erro {type(e).__name__}: {e} | url={target}")
        return aiohttp_web.Response(
            text=f"{type(e).__name__}: {e}",
            status=502,
            headers=_CORS_HEADERS,
        )


async def _iniciar_servidor_web():
    app = aiohttp_web.Application()
    app.router.add_get("/player/{token}", _web_player_handler)
    app.router.add_get("/proxy/{token}",  _proxy_handler)
    app.router.add_get("/proxy",          _proxy_handler)   # segmentos reescritos do M3U8
    app.router.add_options("/proxy/{token}", lambda r: aiohttp_web.Response(headers=_CORS_HEADERS))
    app.router.add_options("/proxy",         lambda r: aiohttp_web.Response(headers=_CORS_HEADERS))
    runner = aiohttp_web.AppRunner(app)
    await runner.setup()
    site = aiohttp_web.TCPSite(runner, "0.0.0.0", SERVER_PORT)
    await site.start()
    print(f"[Player] Servidor web em {SERVER_URL}/player/<token>")


# ==========================================
# BOT DISCORD
# ==========================================

class FootballBot(commands.Bot):
    async def setup_hook(self):
        await _iniciar_servidor_web()
        checar_jogos_ao_vivo.start()
        atualizar_players.start()
        if CANAL_RESUMO_ID:
            resumo_diario.start()
        else:
            print("[Resumo] CANAL_JOGOS_DO_DIA não configurado — resumo diário desativado.")


intents = discord.Intents.default()
intents.message_content = True
bot = FootballBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")


# ==========================================
# MONITORAMENTO AO VIVO (ESPN summary)
# ==========================================

@tasks.loop(seconds=30)
async def checar_jogos_ao_vivo():
    for event_id, dados in list(JOGOS_MONITORADOS.items()):
        if dados.get("encerrado"):
            del JOGOS_MONITORADOS[event_id]
            continue

        canal = bot.get_channel(dados["canal_id"])
        if not canal:
            continue

        try:
            slug = dados["slug"]
            sumario = buscar_partida_espn(slug, event_id)
            if not sumario:
                continue

            # Status via header da partida
            header = sumario.get("header", {})
            comp = (header.get("competitions") or [{}])[0]
            status_type = (comp.get("status") or {}).get("type") or {}
            state = status_type.get("state", "pre")

            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            nome_casa = (home.get("team") or {}).get("displayName", "")
            nome_fora = (away.get("team") or {}).get("displayName", "")
            g_casa = _safe_score(home)
            g_fora = _safe_score(away)

            # Eventos-chave (gols e cartoes)
            for ev in sumario.get("keyEvents", []):
                ev_id = str(ev.get("id", ""))
                if not ev_id or ev_id in dados["eventos"]:
                    continue
                dados["eventos"].add(ev_id)

                tipo = (ev.get("type") or {}).get("text", "")
                clock = (ev.get("clock") or {}).get("displayValue", "")
                try:
                    minuto = int(clock.split(":")[0])
                except Exception:
                    minuto = "?"

                time_ev = (ev.get("team") or {}).get("displayName", "")
                participants = ev.get("participants", [])
                scorer = next(
                    (p for p in participants if "scorer" in (p.get("type") or {}).get("text", "").lower()),
                    participants[0] if participants else {}
                )
                jogador = (scorer.get("athlete") or {}).get("displayName", "?")

                tipo_lower = tipo.lower()
                if "goal" in tipo_lower:
                    contra = "own" in tipo_lower
                    emoji = "❌" if contra else "⚽"
                    await canal.send(
                        f"{emoji} **GOL!** `{minuto}'` — **{jogador}**"
                        f"{' (contra)' if contra else f' ({time_ev})'}\n"
                        f"📊 **{nome_casa} {g_casa} × {g_fora} {nome_fora}**"
                    )
                elif "yellow" in tipo_lower:
                    await canal.send(f"🟨 Cartão Amarelo `{minuto}'` — **{jogador}** ({time_ev})")
                elif "red" in tipo_lower:
                    await canal.send(f"🟥 Cartão Vermelho `{minuto}'` — **{jogador}** ({time_ev})")

            if state == "post":
                await canal.send(
                    f"🏁 **FIM DE JOGO!**\n"
                    f"**{nome_casa} {g_casa} × {g_fora} {nome_fora}**"
                )
                _revogar_sessoes(event_id)
                dados["encerrado"] = True

        except Exception as e:
            print(f"[Monitor] Erro no jogo {event_id}: {e}")


# ==========================================
# AUTO-ATUALIZAÇÃO DOS PLAYERS
# ==========================================

@tasks.loop(seconds=60)
async def atualizar_players():
    for guild_id, dados in list(PLAYERS_ATIVOS.items()):
        try:
            msg       = dados["message"]
            event_id  = dados["event_id"]
            slug      = dados["slug"]
            nome_canal = dados["canal_iptv"]

            embed, encerrado = await _montar_embed_player(event_id, slug, nome_canal)

            view = None
            if encerrado:
                view = PlayerView(event_id, slug, nome_canal)
                for item in view.children:
                    item.disabled = True
                _revogar_sessoes(event_id)
                del PLAYERS_ATIVOS[guild_id]
            else:
                view = PlayerView(event_id, slug, nome_canal)

            await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"[Player] Erro ao atualizar guild {guild_id}: {e}")


# ==========================================
# RESUMO DIÁRIO — task agendada
# ==========================================

@tasks.loop(time=HORARIO_RESUMO)
async def resumo_diario():
    canal = bot.get_channel(CANAL_RESUMO_ID)
    if not canal:
        print(f"[Resumo] Canal {CANAL_RESUMO_ID} não encontrado.")
        return

    print(f"[Resumo] Gerando resumo diário para #{canal.name}...")
    await canal.send("⏳ Buscando jogos do dia em todas as ligas...")

    jogos_por_liga = {}
    for chave in LIGAS_RESUMO:
        try:
            if chave in LIGAS_COPA:
                jogos = buscar_jogos_copa_hoje()
            else:
                jogos = buscar_jogos_do_dia(LIGAS[chave])
            if jogos:
                jogos_por_liga[chave] = jogos
        except Exception as e:
            print(f"[Resumo] Erro buscando {chave}: {e}")

    # Apaga a mensagem de "buscando" e envia a imagem
    async for msg in canal.history(limit=5):
        if msg.author == bot.user and "⏳" in msg.content:
            await msg.delete()
            break

    if not jogos_por_liga:
        await canal.send("📭 Nenhum jogo encontrado hoje em nenhuma liga.")
        return

    total = sum(len(v) for v in jogos_por_liga.values())
    img = await gerar_resumo_diario_png(jogos_por_liga)
    await canal.send(
        content=f"📅 **Jogos do Dia** — {total} partidas em {len(jogos_por_liga)} ligas",
        file=discord.File(img),
    )
    print(f"[Resumo] Enviado: {total} jogos em {len(jogos_por_liga)} ligas.")


# ==========================================
# COMPONENTES UI — botões de seguir jogo
# ==========================================

class DetalhesButton(discord.ui.Button):
    def __init__(self, jogo: dict, row: int):
        super().__init__(label="📋 Detalhes", style=discord.ButtonStyle.primary, row=row)
        self.jogo = jogo

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        slug = getattr(self.view, "slug", "")
        broadcasts = _canais_tv(self.jogo, slug)

        meta     = self.jogo.get("meta", {})
        venue    = meta.get("venue", "")
        odds_str = meta.get("odds", "")

        nome_casa = self.jogo["teams"]["home"]["name"]
        nome_fora = self.jogo["teams"]["away"]["name"]

        try:
            dt = datetime.fromisoformat(self.jogo["fixture"]["date"].replace("Z", "+00:00")).astimezone(BRT)
            data_hora = dt.strftime("%d/%m/%Y às %H:%M (BRT)")
        except Exception:
            data_hora = "—"

        embed = discord.Embed(
            title=f"📋 {nome_casa} × {nome_fora}",
            color=0x3B82F6,
        )
        embed.add_field(name="🗓️ Data/Hora",   value=data_hora,                                       inline=False)
        embed.add_field(name="🏟️ Estádio",     value=venue or "Não informado",                        inline=False)
        embed.add_field(name="📺 Transmissão", value="\n".join(broadcasts) if broadcasts else "Não informado", inline=False)
        if odds_str:
            embed.add_field(name="🎰 Odds",    value=odds_str,                                        inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


class SeguirButton(discord.ui.Button):
    def __init__(self, jogo: dict, slug: str, row: int):
        nome_casa = jogo["teams"]["home"]["name"]
        nome_fora = jogo["teams"]["away"]["name"]
        label = f"🔴 {nome_casa[:13]} × {nome_fora[:13]}"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.jogo     = jogo
        self.slug     = slug
        self.event_id = str(jogo["fixture"]["id"])

    async def callback(self, interaction: discord.Interaction):
        # Bug fix: permite re-monitorar após parar (verifica apenas se está ATIVO agora)
        if self.event_id in JOGOS_MONITORADOS and not JOGOS_MONITORADOS[self.event_id].get("encerrado"):
            await interaction.response.send_message("📌 Jogo já monitorado neste canal.", ephemeral=True)
            return

        sumario = buscar_partida_espn(self.slug, self.event_id)
        if not sumario:
            await interaction.response.send_message("❌ Não foi possível encontrar o jogo.", ephemeral=True)
            return

        header      = sumario.get("header", {})
        comp        = (header.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        nome_casa = (home.get("team") or {}).get("displayName", "?")
        nome_fora = (away.get("team") or {}).get("displayName", "?")

        JOGOS_MONITORADOS[self.event_id] = {
            "canal_id":  interaction.channel_id,
            "slug":      self.slug,
            "eventos":   {str(ev.get("id","")) for ev in sumario.get("keyEvents",[]) if ev.get("id")},
            "encerrado": False,
        }

        self.style    = discord.ButtonStyle.success
        self.disabled = True
        await interaction.message.edit(view=self.view)
        await interaction.response.send_message(
            f"✅ **Monitoramento ativado!**\n"
            f"⚽ **{nome_casa}** × **{nome_fora}**\n"
            f"*Avisarei aqui sobre gols, cartões e fim de jogo.*",
            view=PararView(self.event_id, nome_casa, nome_fora),
        )


class PararButton(discord.ui.Button):
    def __init__(self, event_id: str, nome_casa: str, nome_fora: str):
        super().__init__(label="🛑 Parar monitoramento", style=discord.ButtonStyle.danger)
        self.event_id  = event_id
        self.nome_casa = nome_casa
        self.nome_fora = nome_fora

    async def callback(self, interaction: discord.Interaction):
        if self.event_id in JOGOS_MONITORADOS:
            del JOGOS_MONITORADOS[self.event_id]
            self.disabled = True
            self.label    = "✅ Monitoramento encerrado"
            self.style    = discord.ButtonStyle.secondary
            await interaction.message.edit(view=self.view)
            await interaction.response.send_message(
                f"🛑 Monitoramento de **{self.nome_casa} × {self.nome_fora}** encerrado.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ Este jogo não está mais sendo monitorado.", ephemeral=True
            )


class PararView(discord.ui.View):
    def __init__(self, event_id: str, nome_casa: str, nome_fora: str):
        super().__init__(timeout=None)   # sem timeout: botão disponível até encerrar
        self.add_item(PararButton(event_id, nome_casa, nome_fora))


class PlayerView(discord.ui.View):
    def __init__(self, event_id: str, slug: str, canal_iptv: str):
        super().__init__(timeout=None)
        self.event_id   = event_id
        self.slug       = slug
        self.canal_iptv = canal_iptv

    @discord.ui.button(label="🔄 Atualizar", style=discord.ButtonStyle.primary)
    async def btn_atualizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        embed, encerrado = await _montar_embed_player(self.event_id, self.slug, self.canal_iptv)
        if encerrado:
            for item in self.children:
                item.disabled = True
            _revogar_sessoes(self.event_id)
            if interaction.guild_id in PLAYERS_ATIVOS:
                del PLAYERS_ATIVOS[interaction.guild_id]
        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="⏹ Fechar", style=discord.ButtonStyle.secondary)
    async def btn_fechar(self, interaction: discord.Interaction, button: discord.ui.Button):
        _revogar_sessoes(self.event_id)
        if interaction.guild_id in PLAYERS_ATIVOS:
            del PLAYERS_ATIVOS[interaction.guild_id]
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message("Player fechado.", ephemeral=True)


class TransmitirButton(discord.ui.Button):
    def __init__(self, jogo: dict, row: int):
        super().__init__(label="📺 Abrir Player", style=discord.ButtonStyle.danger, row=row)
        self.jogo      = jogo
        self.event_id  = str(jogo["fixture"]["id"])

    async def callback(self, interaction: discord.Interaction):
        if not IPTV_URL:
            await interaction.response.send_message("IPTV não configurado no `.env`.", ephemeral=True)
            return

        # Desabilita o botão imediatamente e mostra loading
        self.disabled = True
        self.label    = "⏳ Carregando..."
        self.style    = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self.view)

        slug      = getattr(self.view, "slug", "")
        nome_casa = self.jogo["teams"]["home"]["name"]
        nome_fora = self.jogo["teams"]["away"]["name"]

        try:
            broadcasts = _canais_tv(self.jogo, slug)
            loop       = asyncio.get_event_loop()

            canal_iptv = None
            for nome in broadcasts:
                canal_iptv = await loop.run_in_executor(None, _iptv_buscar_canal, nome)
                if canal_iptv:
                    break

            if canal_iptv:
                stream_url = canal_iptv["url"]
                title      = f"{nome_casa} × {nome_fora}"
                token      = _criar_sessao(stream_url, title, self.event_id, slug)
                player_url = f"{SERVER_URL}/player/{token}"

                self.label = f"📺 {canal_iptv['name'][:20]}"
                await interaction.message.edit(view=self.view)
                await interaction.followup.send(
                    f"📺 **{title}**\nCanal: **{canal_iptv['name']}**\n{player_url}",
                    ephemeral=False,
                )
            else:
                nome_canal  = broadcasts[0] if broadcasts else "Canal desconhecido"
                embed, _    = await _montar_embed_player(self.event_id, slug, nome_canal)
                view_player = PlayerView(self.event_id, slug, nome_canal)
                msg         = await interaction.channel.send(embed=embed, view=view_player)
                PLAYERS_ATIVOS[interaction.guild_id] = {
                    "message": msg, "event_id": self.event_id,
                    "slug": slug,   "canal_iptv": nome_canal,
                }
                self.label = "📺 Player aberto"
                await interaction.message.edit(view=self.view)
                await interaction.followup.send(
                    f"⚠️ Canal não encontrado na IPTV.\n"
                    f"Transmissão prevista em: **{', '.join(broadcasts) or 'desconhecido'}**",
                    ephemeral=True,
                )

        except Exception as e:
            print(f"[TransmitirButton] Erro: {e}")
            # Restaura o botão em caso de erro
            self.disabled = False
            self.label    = "📺 Abrir Player"
            self.style    = discord.ButtonStyle.danger
            try:
                await interaction.message.edit(view=self.view)
                await interaction.followup.send(f"❌ Erro ao abrir player: `{e}`", ephemeral=True)
            except Exception:
                pass


class SeguirView(discord.ui.View):
    def __init__(self, jogos: list, slug: str):
        super().__init__(timeout=600)
        self.slug = slug
        for i, jogo in enumerate(jogos[:5]):
            self.add_item(SeguirButton(jogo, slug, row=i))
            self.add_item(DetalhesButton(jogo, row=i))
            if IPTV_URL:
                self.add_item(TransmitirButton(jogo, row=i))


# ==========================================
# COMANDOS
# ==========================================

@bot.command(name="tabela")
async def cmd_tabela(ctx, *, nome_liga: str = "brasileirao"):
    chave = nome_liga.lower().replace(" ", "")
    if chave not in LIGAS:
        await ctx.send(f"❌ Liga inválida. Opções: `{'`, `'.join(LIGAS)}`")
        return

    # Copa do Brasil: mata-mata — mostra rodada atual, não pontos corridos
    if chave in LIGAS_COPA:
        msg = await ctx.send("🏆 Buscando rodada atual da **Copa do Brasil** (temporada 2024)...")
        rodada, fixtures = buscar_rodada_copa_brasil()
        if not fixtures:
            await msg.edit(content="❌ Sem dados da Copa do Brasil. A API-Football free tier cobre até a temporada 2024.")
            return
        img = await gerar_mata_mata_png(fixtures, "Copa do Brasil", rodada)
        await msg.delete()
        await ctx.send(file=discord.File(img))
        return

    msg = await ctx.send(f"📊 Gerando tabela do **{nome_liga.title()}**...")
    try:
        loop = asyncio.get_event_loop()
        dados = await loop.run_in_executor(None, buscar_tabela, LIGAS[chave])
        if not dados:
            await msg.edit(content="❌ Sem dados para esta liga. Tente outra ou aguarde a temporada começar.")
            return
        img = await gerar_tabela_png(dados, nome_liga.title(), slug=LIGAS[chave])
        await msg.delete()
        await ctx.send(file=discord.File(img))
    except Exception as e:
        print(f"[!tabela] Erro em {chave}: {e}")
        await msg.edit(content=f"❌ Erro ao gerar tabela: `{e}`")


@bot.command(name="tabela_brasileirao")
async def cmd_tabela_brasileirao(ctx):
    await ctx.invoke(bot.get_command("tabela"), nome_liga="brasileirao")


@bot.command(name="liga")
async def cmd_liga(ctx, *, nome_liga: str = "brasileirao"):
    chave = nome_liga.lower().replace(" ", "")
    if chave not in LIGAS:
        await ctx.send(f"❌ Liga inválida. Opções: `{'`, `'.join(LIGAS)}`")
        return
    msg = await ctx.send(f"📅 Buscando jogos — **{nome_liga.title()}**...")
    try:
        loop = asyncio.get_event_loop()
        if chave in LIGAS_COPA:
            jogos = await loop.run_in_executor(None, buscar_jogos_copa_hoje)
        else:
            jogos = await loop.run_in_executor(None, buscar_jogos_do_dia, LIGAS[chave])

        img = await gerar_jogos_png(jogos, nome_liga.title())
        await msg.delete()

        slug = LIGAS.get(chave)
        jogos_ativos = [
            j for j in jogos
            if j["fixture"]["status"]["short"] not in ("FT", "AET", "PEN")
        ]
        if jogos_ativos and slug:
            view = SeguirView(jogos_ativos, slug)
            await ctx.send(file=discord.File(img), view=view)
        else:
            await ctx.send(file=discord.File(img))
    except Exception as e:
        print(f"[!liga] Erro em {chave}: {e}")
        await msg.edit(content=f"❌ Erro ao buscar jogos de **{nome_liga.title()}**: `{e}`")


@bot.command(name="hoje")
async def cmd_hoje(ctx):
    msg = await ctx.send("📅 Buscando jogos do dia em todas as ligas...")
    loop = asyncio.get_event_loop()
    jogos_por_liga = {}
    for chave in LIGAS_RESUMO:
        try:
            if chave in LIGAS_COPA:
                jogos = await loop.run_in_executor(None, buscar_jogos_copa_hoje)
            else:
                jogos = await loop.run_in_executor(None, buscar_jogos_do_dia, LIGAS[chave])
            if jogos:
                jogos_por_liga[chave] = jogos
        except Exception as e:
            print(f"[!hoje] Erro em {chave}: {e}")

    await msg.delete()

    if not jogos_por_liga:
        await ctx.send("📭 Nenhum jogo encontrado hoje em nenhuma liga.")
        return

    total = sum(len(v) for v in jogos_por_liga.values())
    img = await gerar_resumo_diario_png(jogos_por_liga)
    await ctx.send(
        content=f"📅 **Jogos do Dia** — {total} partidas em {len(jogos_por_liga)} ligas",
        file=discord.File(img),
    )


@bot.command(name="seguir")
async def cmd_seguir(ctx, nome_liga: str, event_id: str):
    chave = nome_liga.lower().replace(" ", "")
    if chave not in LIGAS:
        await ctx.send(f"❌ Liga inválida. Opções: `{'`, `'.join(LIGAS)}`")
        return
    if event_id in JOGOS_MONITORADOS:
        await ctx.send("📌 Este jogo já está sendo monitorado aqui.")
        return

    slug = LIGAS[chave]
    sumario = buscar_partida_espn(slug, event_id)
    if not sumario:
        await ctx.send("❌ ID inválido ou jogo não encontrado. Verifique o ID na imagem do `!liga`.")
        return

    header = sumario.get("header", {})
    comp = (header.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    nome_casa = (home.get("team") or {}).get("displayName", "?")
    nome_fora = (away.get("team") or {}).get("displayName", "?")

    # Registra eventos já existentes para não re-notificar
    eventos_existentes = {
        str(ev.get("id", ""))
        for ev in sumario.get("keyEvents", [])
        if ev.get("id")
    }

    JOGOS_MONITORADOS[event_id] = {
        "canal_id": ctx.channel.id,
        "slug": slug,
        "eventos": eventos_existentes,
        "encerrado": False,
    }

    await ctx.send(
        f"✅ **Monitoramento ativado!**\n"
        f"⚽ **{nome_casa}** × **{nome_fora}**\n"
        f"*Vou avisar aqui quando sair gol, cartão ou o jogo terminar.*"
    )


@bot.command(name="parar")
async def cmd_parar(ctx, event_id: str):
    if event_id in JOGOS_MONITORADOS:
        del JOGOS_MONITORADOS[event_id]
        await ctx.send(f"🛑 Monitoramento encerrado.")
    else:
        await ctx.send("⚠️ Este jogo não está sendo monitorado.")


@bot.command(name="monitorando")
async def cmd_monitorando(ctx):
    """Lista todos os jogos sendo monitorados com botão de parar."""
    if not JOGOS_MONITORADOS:
        await ctx.send("📭 Nenhum jogo sendo monitorado no momento.")
        return

    for event_id, dados in JOGOS_MONITORADOS.items():
        slug    = dados.get("slug", "")
        sumario = buscar_partida_espn(slug, event_id) if slug else None

        if sumario:
            header      = sumario.get("header", {})
            comp        = (header.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            nome_casa = (home.get("team") or {}).get("displayName", "?")
            nome_fora = (away.get("team") or {}).get("displayName", "?")
            status    = (comp.get("status") or {}).get("type") or {}.get("shortDetail", "")
            g_casa    = _safe_score(home)
            g_fora    = _safe_score(away)
            linha = f"⚽ **{nome_casa} {g_casa} × {g_fora} {nome_fora}** — {status}"
        else:
            nome_casa, nome_fora = "?", "?"
            linha = f"⚽ Jogo `{event_id}`"

        await ctx.send(linha, view=PararView(event_id, nome_casa, nome_fora))


@bot.command(name="transmitir")
async def cmd_transmitir(ctx, *, canal: str = ""):
    if not IPTV_URL:
        await ctx.send("IPTV não configurado. Adicione `IPTV_URL`, `IPTV_USER` e `IPTV_PASS` no `.env`.")
        return
    if not canal:
        await ctx.send("Uso: `!transmitir [nome do canal]`\nEx: `!transmitir SporTV 2`\nUse `!canais [busca]` para ver canais disponíveis.")
        return

    msg = await ctx.send(f"🔍 Buscando canal `{canal}`...")
    loop = asyncio.get_event_loop()
    canal_iptv = await loop.run_in_executor(None, _iptv_buscar_canal, canal)

    nome_canal = canal_iptv.get("name", canal) if canal_iptv else canal

    embed = discord.Embed(
        title=f"📺 {nome_canal}",
        description="Sintonize este canal na sua IPTV para assistir.",
        color=0x3B82F6 if canal_iptv else 0xEF4444,
    )
    if not canal_iptv:
        embed.add_field(name="⚠️ Aviso", value=f"Canal `{canal}` não encontrado na lista IPTV.\nUse `!canais {canal}` para ver opções similares.", inline=False)
    embed.set_footer(text=datetime.now(tz=BRT).strftime("%d/%m/%Y %H:%M BRT"))

    await msg.delete()
    await ctx.send(embed=embed)


@bot.command(name="canais")
async def cmd_canais(ctx, *, busca: str = ""):
    if not IPTV_URL:
        await ctx.send("IPTV não configurado.")
        return
    msg = await ctx.send("🔍 Carregando canais...")
    loop = asyncio.get_event_loop()
    canais = await loop.run_in_executor(None, _iptv_canais)
    if not canais:
        await msg.edit(content="❌ Não foi possível acessar a IPTV. Verifique as credenciais.")
        return

    filtrado = [c for c in canais if busca.lower() in c.get("name", "").lower()] if busca else canais
    if not filtrado:
        await msg.edit(content=f"Nenhum canal encontrado para `{busca}`.")
        return

    linhas = [f"`{c['stream_id']}` {c['name']}" for c in filtrado[:30]]
    sufixo = f" (+{len(filtrado)-30} mais — refine a busca)" if len(filtrado) > 30 else ""
    header = f"📺 **{len(filtrado)} canais**{(' para `'+busca+'`') if busca else ''}{sufixo}\n"
    await msg.edit(content=header + "\n".join(linhas))


@bot.command(name="artilheiro")
async def cmd_artilheiro(ctx, liga: str = ""):
    liga = liga.lower()
    if not liga or liga not in LIGAS:
        ligas_disp = ", ".join(f"`{k}`" for k in LIGAS if k not in LIGAS_COPA)
        await ctx.send(f"Uso: `!artilheiro [liga]`\nLigas: {ligas_disp}")
        return
    if liga in LIGAS_COPA:
        await ctx.send("Artilheiros da Copa do Brasil não disponíveis.")
        return
    slug = LIGAS[liga]
    msg = await ctx.send("🔍 Buscando artilheiros...")
    loop = asyncio.get_event_loop()
    artilheiros = await loop.run_in_executor(None, buscar_artilheiros, slug)
    if not artilheiros:
        await msg.edit(content="Nenhum artilheiro encontrado para esta liga.")
        return
    meta = LIGAS_META.get(liga, {"nome": liga.title(), "emoji": "🏆"})
    nome_liga = f"{meta['emoji']} {meta['nome']}"
    try:
        caminho = await gerar_artilheiro_png(artilheiros, nome_liga)
        await msg.delete()
        await ctx.send(file=discord.File(caminho))
    except Exception as e:
        await msg.edit(content=f"Erro ao gerar imagem: {e}")


@bot.command(name="proximos")
async def cmd_proximos(ctx, liga: str = "", *, time: str = ""):
    liga = liga.lower()
    if not liga or liga not in LIGAS or not time:
        ligas_disp = ", ".join(f"`{k}`" for k in LIGAS if k not in LIGAS_COPA)
        await ctx.send(f"Uso: `!proximos [liga] [time]`\nEx: `!proximos brasileirao Flamengo`\nLigas: {ligas_disp}")
        return
    if liga in LIGAS_COPA:
        await ctx.send("Próximos jogos da Copa do Brasil não disponíveis.")
        return
    slug = LIGAS[liga]
    msg = await ctx.send(f"🔍 Buscando próximos jogos de **{time}**...")
    loop = asyncio.get_event_loop()
    resultado = await loop.run_in_executor(None, buscar_time_id, slug, time)
    if not resultado:
        await msg.edit(content=f"Time `{time}` não encontrado na liga `{liga}`.")
        return
    team_id, nome_oficial = resultado
    jogos = await loop.run_in_executor(None, buscar_proximos_jogos, slug, team_id)
    if not jogos:
        await msg.edit(content=f"Nenhum jogo futuro encontrado para **{nome_oficial}**.")
        return
    meta = LIGAS_META.get(liga, {"nome": liga.title(), "emoji": "🏆"})
    nome_liga = f"{meta['emoji']} {meta['nome']}"
    try:
        caminho = await gerar_proximos_png(jogos, nome_oficial, nome_liga)
        await msg.delete()
        await ctx.send(file=discord.File(caminho))
    except Exception as e:
        await msg.edit(content=f"Erro ao gerar imagem: {e}")


@bot.command(name="ajuda")
async def cmd_ajuda(ctx):
    embed = discord.Embed(title="⚽ Football Bot — Comandos", color=0x3B82F6)
    embed.add_field(name="!hoje",                    value="Jogos do dia em todas as ligas (imagem)",                          inline=False)
    embed.add_field(name="!liga [liga]",             value="Jogos da liga — botões para monitorar, ver detalhes e transmitir",  inline=False)
    embed.add_field(name="!tabela [liga]",           value="Classificação da liga",                                            inline=False)
    embed.add_field(name="!artilheiro [liga]",       value="Top artilheiros da liga",                                          inline=False)
    embed.add_field(name="!proximos [liga] [time]",  value="Próximos 5 jogos de um time. Ex: `!proximos brasileirao Flamengo`", inline=False)
    embed.add_field(name="!monitorando",             value="Lista jogos monitorados com botão para parar",                     inline=False)
    if IPTV_URL:
        embed.add_field(name="📺 IPTV", value=(
            "`!canais [busca]` — Lista canais disponíveis na IPTV\n"
            "`!transmitir [canal]` — Mostra qual canal sintonizar\n"
            "Botão **📺 Abrir Player** nos jogos — player ao vivo no chat"
        ), inline=False)
    embed.add_field(
        name="Ligas disponíveis",
        value="`" + "`  `".join(LIGAS.keys()) + "`",
        inline=False,
    )
    embed.set_footer(text="Dados: ESPN API 2026 · Copa do Brasil via API-Football (temporada 2024)")
    await ctx.send(embed=embed)


print("=== DEBUG ENV VARS ===")
for k in sorted(os.environ.keys()):
    print(f"  {k}")
print("=== FIM DEBUG ===")

if not TOKEN_DO_DISCORD:
    raise SystemExit("ERRO: TOKEN_DISCORD não encontrado. Verifique o nome exato da variável no Railway.")

bot.run(TOKEN_DO_DISCORD)
