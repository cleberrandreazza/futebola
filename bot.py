import os
import re
import json
import time
import base64
import asyncio
import functools
import secrets
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as _ET
import discord
from discord.ext import commands, tasks
import requests
import aiohttp
from datetime import date, datetime, timezone, timedelta
from urllib.parse import quote as url_quote
from playwright.async_api import async_playwright
from aiohttp import web as aiohttp_web
from dotenv import load_dotenv

load_dotenv()

TOKEN_DO_DISCORD  = os.getenv("TOKEN_DISCORD")
CANAL_RESUMO_ID   = int(os.getenv("CANAL_JOGOS_DO_DIA", "0"))
CANAL_EVENTO_ID   = int(os.getenv("CANAL_EVENTO", "1510344579927249017"))
CANAL_COMANDOS_ID = int(os.getenv("CANAL_COMANDOS", "0"))
# Railway injeta PORT automaticamente; localmente usa SERVER_PORT ou 8080
SERVER_PORT       = int(os.getenv("PORT", os.getenv("SERVER_PORT", "8080")))
SERVER_URL        = os.getenv("SERVER_URL", f"http://localhost:{SERVER_PORT}")
_hora_env         = os.getenv("HORA_RESUMO_DIARIO", "09:00").split(":")
BRT               = timezone(timedelta(hours=-3))
HORARIO_RESUMO    = __import__("datetime").time(
    int(_hora_env[0]), int(_hora_env[1]), tzinfo=BRT
)

# Cache HTTP (reduz chamadas repetidas em tasks e monitoramento)
_API_CACHE: dict[str, tuple[float, object]] = {}
_API_CACHE_TTL = 60  # segundos


def _cache_key(prefix: str, url: str, params: dict | None) -> str:
    return f"{prefix}:{url}:{json.dumps(params or {}, sort_keys=True)}"


def _cache_get(key: str):
    entry = _API_CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _API_CACHE_TTL:
        del _API_CACHE[key]
        return None
    return data


def _cache_set(key: str, data: object) -> None:
    _API_CACHE[key] = (time.time(), data)


# ==========================================
# ESPN API — sem chave, dados 2026 atuais
# ==========================================
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports/soccer"
ESPN_V1 = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Bzzoiro Sports Data API — Copa do Brasil 2026
BZZOIRO_TOKEN     = os.getenv("BZZOIRO_TOKEN", "")
BZZOIRO_BASE      = "https://sports.bzzoiro.com/api/v2"
_BZ_COPA_LEAGUE          = 35
_BZ_COPA_SEASON          = 78
_BZ_BRASILEIRAO_LEAGUE   = 9
_BZ_LIBERTADORES_LEAGUE  = 32
_BZ_SULAMERICANA_LEAGUE  = 33
_BZ_ROUND_NAMES   = {
    1: "1ª Fase", 2: "2ª Fase", 3: "3ª Fase", 4: "4ª Fase",
    5: "5ª Fase", 6: "Semifinais", 7: "Final",
}
_BZ_LEAGUE_NAMES: dict[int, str] = {
    9:  "Brasileirão Série A",
    35: "Copa do Brasil",
    32: "Copa Libertadores",
    33: "Copa Sul-Americana",
}
# Mapeamento de chave de liga (LIGAS) → league_id Bzzoiro (para filtro)
_BZ_LIGA_TO_ID: dict[str, int] = {
    "brasileirao":   9,
    "copadobrasil":  35,
    "libertadores":  32,
    "sulamericana":  33,
}
# league_id Bzzoiro → slug ESPN (para tags de liga em fixtures)
_BZ_ID_TO_SLUG: dict[int, str] = {
    9:  "BRA.1",
    32: "CONMEBOL.LIBERTADORES",
    33: "CONMEBOL.SUDAMERICANA",
}

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
    "amistosos":     "fifa.friendly",
    "copadomundo":     "fifa.world",
    # Copa do Brasil: roteado via API-Football, não ESPN
    "copadobrasil":  None,
}

# Ligas exibidas no !hoje e no resumo diário automático
LIGAS_RESUMO = ["copadomundo", "amistosos", "brasileirao", "champions", "libertadores", "premierleague", "sulamericana"]

PROXIMOS_PADRAO = 5
PROXIMOS_MAX    = 25


def _normalizar_limite_proximos(limite: int) -> int:
    try:
        n = int(limite)
    except (TypeError, ValueError):
        return PROXIMOS_PADRAO
    return max(1, min(n, PROXIMOS_MAX))


def _parse_proximos_args(primeiro: str, resto: str) -> tuple[int, str | None, str]:
    """Ex.: `10 flamengo` → (10, None, 'flamengo'); use `10 brasileirao flamengo` com número primeiro."""
    qtd   = PROXIMOS_PADRAO
    parts: list[str] = []
    if primeiro:
        parts.append(primeiro.strip())
    if resto:
        parts.extend(resto.strip().split())
    if parts and parts[0].isdigit():
        qtd   = _normalizar_limite_proximos(int(parts[0]))
        parts = parts[1:]
    if not parts:
        return qtd, None, ""
    if parts[0].lower() in LIGAS:
        return qtd, parts[0].lower(), " ".join(parts[1:]).strip()
    return qtd, None, " ".join(parts).strip()


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

# token -> HTML string (página de detalhes de partida)
_partida_pages: dict[str, str] = {}


def _criar_sessao(stream_url: str, title: str, event_id: str, slug: str,
                   bz_event_id: int | None = None,
                   nome_casa: str = "", nome_fora: str = "") -> str:
    token = secrets.token_urlsafe(24)
    _player_sessions[token] = {
        "stream_url":  stream_url,
        "title":       title,
        "event_id":    event_id,
        "slug":        slug,
        "bz_event_id": bz_event_id,
        "nome_casa":   nome_casa,
        "nome_fora":   nome_fora,
    }
    print(f"[Session] criada token={token[:8]}… event={event_id} bz={bz_event_id}")
    return token


def _find_bz_event_id(nome_casa: str, nome_fora: str) -> int | None:
    """Busca o event_id Bzzoiro para um jogo de hoje pelos nomes dos times."""
    hoje = datetime.now(tz=BRT).date()
    qh_orig = nome_casa.lower().strip()
    qa_orig = nome_fora.lower().strip()
    # Aplica alias ESPN→Bzzoiro (ex: "atlético-mg" → "atlético mineiro")
    qh = _ESPN_TO_BZ.get(qh_orig, qh_orig)
    qa = _ESPN_TO_BZ.get(qa_orig, qa_orig)

    def _match(ev: dict) -> bool:
        bh = (ev.get("home_team") or "").lower()
        ba = (ev.get("away_team") or "").lower()
        return (qh in bh or bh in qh) and (qa in ba or ba in qa)

    # Busca por liga específica (mais precisa)
    for league_id, extra in [
        (_BZ_BRASILEIRAO_LEAGUE,  {}),
        (_BZ_COPA_LEAGUE,         {"season_id": _BZ_COPA_SEASON}),
        (_BZ_LIBERTADORES_LEAGUE, {}),
        (_BZ_SULAMERICANA_LEAGUE, {}),
    ]:
        params = {"league_id": league_id, "date_from": str(hoje), "date_to": str(hoje), "limit": 50}
        params.update(extra)
        data = _bzzoiro_get("events/", params)
        for ev in (data or {}).get("results", []):
            if _match(ev):
                print(f"[BZ] encontrado liga={league_id} id={ev.get('id')} {ev.get('home_team')} x {ev.get('away_team')}")
                return ev.get("id")

    # Fallback: busca ampla sem filtro de liga (cobre todas as competições)
    data = _bzzoiro_get("events/", {"date_from": str(hoje), "date_to": str(hoje), "limit": 200})
    for ev in (data or {}).get("results", []):
        if _match(ev):
            print(f"[BZ] encontrado (broad) liga={ev.get('league_id')} id={ev.get('id')} {ev.get('home_team')} x {ev.get('away_team')}")
            return ev.get("id")

    print(f"[BZ] não encontrado: '{nome_casa}' x '{nome_fora}'")
    return None


def _revogar_sessoes(event_id: str) -> int:
    tokens = [t for t, s in _player_sessions.items() if s["event_id"] == event_id]
    for t in tokens:
        del _player_sessions[t]
    if tokens:
        print(f"[Session] {len(tokens)} sessão(ões) revogada(s) para event={event_id}")
    return len(tokens)

# Metadados das ligas para exibição no resumo diário
LIGAS_META = {
    "brasileirao":   {"nome": "Brasileirão Série A",        "emoji": "🇧🇷"},
    "premierleague": {"nome": "Premier League",             "emoji": "🏴󠁧󠁢󠁥󠁮󠁧󠁿"},
    "champions":     {"nome": "Champions League",           "emoji": "⭐"},
    "copadobrasil":  {"nome": "Copa do Brasil",             "emoji": "🏆"},
    "sulamericana":  {"nome": "Sul-Americana",              "emoji": "🌎"},
    "libertadores":  {"nome": "Libertadores",               "emoji": "🌎"},
    "laliga":        {"nome": "La Liga",                    "emoji": "🇪🇸"},
    "seriea":        {"nome": "Serie A",                    "emoji": "🇮🇹"},
    "bundesliga":    {"nome": "Bundesliga",                 "emoji": "🇩🇪"},
    "ligue1":        {"nome": "Ligue 1",                    "emoji": "🇫🇷"},
    "amistosos":     {"nome": "Amistosos Internacionais",   "emoji": "🌍"},
    "copadomundo":     {"nome": "Copa do Mundo FIFA",         "emoji": "🏆"},
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
    busca  = _normalizar_canal(nome).lower()

    candidatos = []

    # 1) nome exato (comparando sem acentos)
    for c in canais:
        if _strip_accents(c["name"]).lower() == busca:
            candidatos.append(c)
    # 2) busca contém o nome — mas "espn" NÃO pode match "espn 2" (verificação de dígito)
    if not candidatos:
        for c in canais:
            n = _strip_accents(c["name"]).lower()
            if busca in n:
                idx   = n.index(busca)
                resto = n[idx + len(busca):].lstrip()
                if not resto or not resto[0].isdigit():
                    candidatos.append(c)
            elif n in busca:
                candidatos.append(c)
    # 3) score por palavras
    if not candidatos:
        palavras = [p for p in busca.split() if len(p) > 2]
        melhor, score = None, 0
        for c in canais:
            n = _strip_accents(c["name"]).lower()
            s = sum(1 for p in palavras if p in n)
            if s > score:
                score, melhor = s, c
        return melhor if score > 0 else None

    # Ordena candidatos: prefer FHD, depois HD, depois sem sufixo, depois SD
    candidatos.sort(key=lambda c: _qualidade(c["name"]), reverse=True)
    return candidatos[0]


def _iptv_buscar_jogo(nome_casa: str, nome_fora: str) -> dict | None:
    """Busca canal IPTV específico para a partida (ex: 'ATLÉTICO-MG x PUERTO CABELLO - COPA ...')."""
    canais  = _iptv_canais()
    # Usa primeiro token de cada time (suficiente para match)
    token_h = _strip_accents(nome_casa.upper().split()[0])
    token_a = _strip_accents(nome_fora.upper().split()[0])
    candidatos = []
    for c in canais:
        n = _strip_accents(c["name"]).upper()
        if token_h in n and token_a in n:
            candidatos.append(c)
    if not candidatos:
        return None
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
    key = _cache_key("espn", url, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get(url, params=params, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            _cache_set(key, data)
            return data
        print(f"[ESPN] {url} -> HTTP {r.status_code}")
    except Exception as e:
        print(f"[ESPN] Erro: {e}")
    return None


# ==========================================
# BZZOIRO — Copa do Brasil 2026
# ==========================================

def _bzzoiro_get(endpoint: str, params: dict = None) -> dict | None:
    if not BZZOIRO_TOKEN:
        return None
    url = f"{BZZOIRO_BASE}/{endpoint.lstrip('/')}"
    key = _cache_key("bz", url, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            url,
            headers={"Authorization": f"Token {BZZOIRO_TOKEN}"},
            params=params,
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            _cache_set(key, data)
            return data
        print(f"[Bzzoiro] {endpoint} -> HTTP {r.status_code}")
    except Exception as e:
        print(f"[Bzzoiro] Erro {endpoint}: {e}")
    return None


_logo_cache_brasil: dict[str, str] = {}
_bz_team_map_cache: dict[str, tuple[int, str]] = {}

# Bzzoiro usa nomes diferentes dos que a ESPN usa para alguns times brasileiros
_BZ_ESPN_ALIASES: dict[str, str] = {
    "Atlético Mineiro":      "Atlético-MG",
    "Athletico":             "Athletico-PR",
    "Athletic Club":         "Athletic",
    "Grêmio Novorizontino":  "Novorizontino",
    "Operário-PR":           "Operário PR",
    "Paysandu SC":           "Paysandu",
}
# Inverso: ESPN name → Bzzoiro name (para busca em _find_bz_event_id)
_ESPN_TO_BZ: dict[str, str] = {v.lower(): k.lower() for k, v in _BZ_ESPN_ALIASES.items()}


def _buscar_logos_brasileiros() -> dict[str, str]:
    """Constrói mapa nome_time → logo_url a partir dos scoreboards ESPN (BRA.1-3, Lib, Sula)."""
    global _logo_cache_brasil
    if _logo_cache_brasil:
        return _logo_cache_brasil
    mapa: dict[str, str] = {}
    hoje   = datetime.now(tz=BRT).date()
    inicio = (hoje - timedelta(days=120)).strftime("%Y%m%d")
    fim    = (hoje + timedelta(days=30)).strftime("%Y%m%d")
    slugs  = ("BRA.1", "BRA.2", "BRA.3",
              "CONMEBOL.LIBERTADORES", "CONMEBOL.SUDAMERICANA")
    for slug in slugs:
        data = _espn_get(f"{ESPN_V1}/{slug}/scoreboard",
                         {"dates": f"{inicio}-{fim}", "limit": 500})
        for ev in (data or {}).get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            for c in comp.get("competitors", []):
                t    = c.get("team") or {}
                name = t.get("displayName", "")
                logo = t.get("logo", "")
                if name and logo and name not in mapa:
                    mapa[name] = logo
    # Aplica aliases: adiciona entradas com o nome Bzzoiro apontando para o logo ESPN
    for bz_name, espn_name in _BZ_ESPN_ALIASES.items():
        if bz_name not in mapa and espn_name in mapa:
            mapa[bz_name] = mapa[espn_name]
    _logo_cache_brasil = mapa
    return mapa


def _bzzoiro_status(ev: dict) -> tuple[str, int | None]:
    """(short_status, elapsed) a partir do formato Bzzoiro."""
    bz  = ev.get("status", "notstarted")
    per = (ev.get("period") or "").upper()
    pen = ev.get("penalty_shootout")
    if bz == "notstarted":
        return "NS", None
    if bz == "finished":
        if pen:
            return "PEN", None
        if per in ("ET", "AET"):
            return "AET", None
        return "FT", None
    if per == "HT":
        return "HT", None
    return "1H", ev.get("current_minute")


def _bzzoiro_ev_to_fixture(ev: dict, logo_map: dict) -> dict:
    """Converte evento Bzzoiro para o formato interno de fixture."""
    short, elapsed = _bzzoiro_status(ev)
    hn  = ev.get("home_team", "")
    an  = ev.get("away_team", "")
    pen = ev.get("penalty_shootout") or {}
    return {
        "fixture": {
            "id":    str(ev.get("id", "0")),
            "date":  ev.get("event_date", ""),
            "status": {"short": short, "elapsed": elapsed},
        },
        "teams": {
            "home": {"name": hn, "logo": logo_map.get(hn, "")},
            "away": {"name": an, "logo": logo_map.get(an, "")},
        },
        "goals": {
            "home": ev.get("home_score"),
            "away": ev.get("away_score"),
        },
        "penalty": {"home": pen.get("home"), "away": pen.get("away")},
        "meta":    {"canal": "", "liga": _BZ_LEAGUE_NAMES.get(ev.get("league_id") or 0, "")},
        "_slug":   _BZ_ID_TO_SLUG.get(ev.get("league_id") or 0, ""),
    }


def buscar_jogos_copa_hoje() -> list:
    """Fixtures de hoje na Copa do Brasil 2026 via Bzzoiro."""
    hoje = datetime.now(tz=BRT).date()
    data = _bzzoiro_get("events/", {
        "league_id": _BZ_COPA_LEAGUE, "season_id": _BZ_COPA_SEASON,
        "date_from": str(hoje), "date_to": str(hoje), "limit": 50,
    })
    if not data:
        return []
    logo_map = _buscar_logos_brasileiros()
    return [_bzzoiro_ev_to_fixture(ev, logo_map) for ev in data.get("results", [])]


# ----- Notificações ao vivo do !seguir (via Bzzoiro) -----

# Status Bzzoiro que NÃO são "ao vivo nem encerrado normalmente"
_BZ_PARADOS = {"notstarted", "cancelled", "postponed", "abandoned", "awarded", "deleted"}


def _bz_eventos_hoje() -> list:
    """Eventos de hoje em todas as ligas Bzzoiro (lista crua de results)."""
    hoje = datetime.now(tz=BRT).date()
    data = _bzzoiro_get("events/", {"date_from": str(hoje), "date_to": str(hoje), "limit": 300})
    return (data or {}).get("results", [])


def _buscar_jogos_ao_vivo_bzzoiro() -> list[dict]:
    """Jogos ao vivo/encerrados de hoje (todas as ligas) via Bzzoiro,
    no formato consumido pelo loop de notificações do !seguir."""
    out: list[dict] = []
    for ev in _bz_eventos_hoje():
        if ev.get("status", "") in _BZ_PARADOS:
            continue
        short, _ = _bzzoiro_status(ev)
        out.append({
            "event_id": str(ev.get("id")),
            "bz_id":    ev.get("id"),
            "home":     ev.get("home_team", ""),
            "away":     ev.get("away_team", ""),
            "state":    short,
        })
    return out


def _bz_incident_key(inc: dict) -> str:
    """Chave estável para deduplicar um incidente Bzzoiro."""
    return ":".join(str(inc.get(k, "")) for k in (
        "type", "minute", "player_id", "card_type", "goal_type",
        "home_score", "away_score",
    ))


def _bz_incident_para_msg(inc: dict, nome_h: str, nome_a: str) -> str | None:
    """Monta a mensagem de DM para um incidente de gol/cartão. None se irrelevante."""
    tipo    = inc.get("type")
    minuto  = inc.get("minute", "?")
    jogador = inc.get("player") or "?"
    time_ev = nome_h if inc.get("is_home") else nome_a
    hs      = inc.get("home_score")
    as_     = inc.get("away_score")
    if tipo == "goal":
        gt     = (inc.get("goal_type") or "").lower()
        contra = "own" in gt
        emoji  = "❌" if contra else "⚽"
        if contra:
            extra = " (gol contra)"
        elif "pen" in gt:
            extra = f" ({time_ev}, pênalti)"
        else:
            extra = f" ({time_ev})"
        assist  = inc.get("assist")
        ass_txt = f"\n🅰️ Assistência: **{assist}**" if assist and not contra else ""
        return (f"{emoji} **GOL!** `{minuto}'` — **{jogador}**{extra}{ass_txt}\n"
                f"📊 **{nome_h} {hs} × {as_} {nome_a}**")
    if tipo == "card":
        ct = (inc.get("card_type") or "").lower()
        if "red" in ct:
            return f"🟥 Cartão Vermelho `{minuto}'` — **{jogador}** ({time_ev})\n**{nome_h} × {nome_a}**"
        if "yellow" in ct:
            return f"🟨 Cartão Amarelo `{minuto}'` — **{jogador}** ({time_ev})\n**{nome_h} × {nome_a}**"
    return None


def _bz_current_season(league_id: int) -> int | None:
    """Retorna o season_id mais recente para uma liga Bzzoiro."""
    data = _bzzoiro_get("seasons/", {"league_id": league_id})
    if not data:
        return None
    now_year = datetime.now().year
    for s in sorted(data.get("results", []), key=lambda x: -(x.get("year", 0) or 0)):
        if (s.get("year") or 0) >= now_year - 1:
            return s.get("id")
    return None


def _bz_build_team_map() -> dict[str, tuple[int, str]]:
    """Constrói {nome_normalizado: (team_id, nome_display)} a partir dos eventos Bzzoiro."""
    global _bz_team_map_cache
    if _bz_team_map_cache:
        return _bz_team_map_cache
    team_map: dict[str, tuple[int, str]] = {}
    known: list[tuple[int, int | None]] = [(_BZ_COPA_LEAGUE, _BZ_COPA_SEASON)]
    bra_season = _bz_current_season(_BZ_BRASILEIRAO_LEAGUE)
    if bra_season:
        known.append((_BZ_BRASILEIRAO_LEAGUE, bra_season))
    lib_season = _bz_current_season(_BZ_LIBERTADORES_LEAGUE)
    if lib_season:
        known.append((_BZ_LIBERTADORES_LEAGUE, lib_season))
    sul_season = _bz_current_season(_BZ_SULAMERICANA_LEAGUE)
    if sul_season:
        known.append((_BZ_SULAMERICANA_LEAGUE, sul_season))
    for league_id, season_id in known:
        params: dict = {"league_id": league_id, "limit": 200}
        if season_id:
            params["season_id"] = season_id
        data = _bzzoiro_get("events/", params)
        for ev in (data or {}).get("results", []):
            for id_key, name_key in [("home_team_id", "home_team"), ("away_team_id", "away_team")]:
                tid  = ev.get(id_key)
                name = ev.get(name_key, "")
                if tid and name:
                    team_map[name.lower().strip()] = (tid, name)
    _bz_team_map_cache = team_map
    return team_map


def buscar_proximos_bzzoiro(
    nome_time: str,
    league_id_filter: int | None = None,
    limite: int = PROXIMOS_PADRAO,
) -> tuple[list, str] | None:
    """Retorna (fixtures, nome_oficial) dos próximos jogos de um time via Bzzoiro.
    Se league_id_filter for fornecido, retorna apenas jogos daquela liga."""
    limite = _normalizar_limite_proximos(limite)
    if not BZZOIRO_TOKEN:
        return None
    team_map = _bz_build_team_map()
    if not team_map:
        return None
    busca  = nome_time.lower().strip()
    result = team_map.get(busca)
    if not result:
        best, best_score = None, 0
        for norm_name, val in team_map.items():
            if busca in norm_name or norm_name in busca:
                score = len(set(busca) & set(norm_name))
                if score > best_score:
                    best_score, best = score, val
        result = best
    if not result:
        return None
    team_id, display_name = result
    hoje = datetime.now(tz=BRT).date()
    # Pede mais para compensar possíveis filtros por liga
    api_limit = min(max(limite * 3, 10), 50)
    data  = _bzzoiro_get(f"teams/{team_id}/fixtures/", {"date_from": str(hoje), "limit": api_limit})
    if not data:
        return None
    logo_map = _buscar_logos_brasileiros()
    now_brt  = datetime.now(tz=BRT)
    fixtures: list = []
    for ev in data.get("results", []):
        if league_id_filter and ev.get("league_id") != league_id_filter:
            continue
        f        = _bzzoiro_ev_to_fixture(ev, logo_map)
        date_str = f["fixture"]["date"]
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(BRT)
            if dt < now_brt:
                continue
        except Exception:
            pass
        fixtures.append(f)
        if len(fixtures) >= limite:
            break
    return (fixtures, display_name) if fixtures else None


def _bzzoiro_confrontos(events: list, logo_map: dict) -> list:
    """Agrega partidas de ida+volta em confrontos com placar acumulado.
    Retorna lista de fixtures no formato gerar_mata_mata_png."""
    ties: dict[tuple, dict] = {}
    for ev in sorted(events, key=lambda e: e.get("event_date", "")):
        hid = ev.get("home_team_id", 0)
        aid = ev.get("away_team_id", 0)
        key = tuple(sorted([hid, aid]))
        if key not in ties:
            ties[key] = {
                "home_id": hid, "home_name": ev["home_team"],
                "away_id": aid, "away_name": ev["away_team"],
                "home_goals": 0, "away_goals": 0,
                "legs": 0, "total_legs": 0,
                "pen": None, "status": "notstarted",
                "date": ev.get("event_date", ""),
            }
        t = ties[key]
        t["total_legs"] += 1
        if ev["status"] == "finished":
            t["legs"] += 1
            hs = ev.get("home_score") or 0
            as_ = ev.get("away_score") or 0
            # Aggregate: normalize home/away relative to first team seen
            if hid == t["home_id"]:
                t["home_goals"] += hs
                t["away_goals"] += as_
            else:
                t["home_goals"] += as_
                t["away_goals"] += hs
            pen = ev.get("penalty_shootout")
            if pen:
                # penalty winner: map back to home/away
                if hid == t["home_id"]:
                    t["pen"] = {"home": pen.get("home"), "away": pen.get("away")}
                else:
                    t["pen"] = {"home": pen.get("away"), "away": pen.get("home")}
        elif ev["status"] not in ("finished",):
            t["status"] = ev["status"]
            t["date"] = ev.get("event_date", "")

    fixtures = []
    for t in ties.values():
        if t["legs"] == 0:
            short = "NS"
        elif t["legs"] < t["total_legs"]:
            short = "1H"   # ida jogada, volta pendente
        elif t["pen"]:
            short = "PEN"
        else:
            short = "FT"

        fixtures.append({
            "fixture": {
                "id":   f"{t['home_id']}-{t['away_id']}",
                "date": t["date"],
                "status": {"short": short, "elapsed": None},
            },
            "teams": {
                "home": {"name": t["home_name"], "logo": logo_map.get(t["home_name"], "")},
                "away": {"name": t["away_name"], "logo": logo_map.get(t["away_name"], "")},
            },
            "goals": {
                "home": t["home_goals"] if t["legs"] > 0 else None,
                "away": t["away_goals"] if t["legs"] > 0 else None,
            },
            "penalty": t["pen"] or {},
        })
    # Ordena: ao vivo → pendentes → encerrados
    order = {"1H": 0, "NS": 1, "FT": 2, "PEN": 2, "AET": 2}
    fixtures.sort(key=lambda f: order.get(f["fixture"]["status"]["short"], 3))
    return fixtures


def buscar_rodada_copa_brasil() -> tuple[str, list]:
    """Retorna (nome_rodada, confrontos_agregados) da Copa do Brasil 2026 via Bzzoiro."""
    data = _bzzoiro_get("events/", {
        "league_id": _BZ_COPA_LEAGUE, "season_id": _BZ_COPA_SEASON, "limit": 200,
    })
    if not data:
        return ("", [])

    events   = data.get("results", [])
    logo_map = _buscar_logos_brasileiros()

    # Agrupa por round_number
    rounds: dict[object, list] = {}
    for ev in events:
        rn = ev.get("round_number")
        rounds.setdefault(rn, []).append(ev)

    # Prioridade: rodada com jogo ao vivo > rodada com jogos futuros > última completada
    best: object = None
    for rn in sorted(rounds, key=lambda x: (x is None, -(x or 0))):
        evs      = rounds[rn]
        statuses = {e["status"] for e in evs}
        if any(s not in ("notstarted", "finished") for s in statuses):
            best = rn; break
        if "notstarted" in statuses and best is None:
            best = rn

    if best is None:
        nums = [rn for rn in rounds if rn is not None]
        best = max(nums) if nums else None
    if best is None:
        return ("", [])

    nome      = _BZ_ROUND_NAMES.get(best, f"Fase {best}" if best is not None else "Próxima Fase")
    confrontos = _bzzoiro_confrontos(rounds[best], logo_map)
    return (nome, confrontos)


# Nomes oficiais das fases eliminatórias da Copa do Brasil pelo nº de confrontos
_COPA_FASE_NOMES: dict[int, str] = {
    8: "Oitavas de Final",
    4: "Quartas de Final",
    2: "Semifinais",
    1: "Final",
}


def buscar_chaveamento_copa_brasil() -> list | None:
    """Retorna rounds no formato gerar_chaveamento_png para as fases finais da Copa do Brasil."""
    data = _bzzoiro_get("events/", {
        "league_id": _BZ_COPA_LEAGUE, "season_id": _BZ_COPA_SEASON, "limit": 200,
    })
    if not data:
        return None

    events   = data.get("results", [])
    logo_map = _buscar_logos_brasileiros()

    # Agrupa por round_number; guarda só rodadas com ≤ 8 confrontos (fases finais)
    rounds: dict[object, list] = {}
    for ev in events:
        rn = ev.get("round_number")
        rounds.setdefault(rn, []).append(ev)

    result = []
    for rn in sorted(rounds, key=lambda x: (x is None, x or 0)):
        confrontos = _bzzoiro_confrontos(rounds[rn], logo_map)
        if len(confrontos) > 8:
            continue  # pula fases iniciais (muitos times)

        n = len(confrontos)
        nome = _COPA_FASE_NOMES.get(n) or _BZ_ROUND_NAMES.get(rn) or (
            f"Fase {rn}" if rn is not None else "Próxima Fase"
        )

        matchups = []
        for c in confrontos:
            short  = c["fixture"]["status"]["short"]
            hg     = c["goals"]["home"]
            ag     = c["goals"]["away"]
            pen    = c.get("penalty", {})
            ph, pa = pen.get("home"), pen.get("away")
            done   = short in ("FT", "AET", "PEN")

            if done and hg is not None:
                if short == "PEN" and ph is not None:
                    hw, aw = ph > pa, pa > ph
                else:
                    hw, aw = hg > ag, ag > hg
            else:
                hw = aw = False

            matchups.append({
                "home": {
                    "team": {"name": c["teams"]["home"]["name"],
                             "logo": c["teams"]["home"]["logo"], "abbr": ""},
                    "score":     str(hg) if done and hg is not None else "",
                    "aggregate": "",
                    "winner":    hw,
                },
                "away": {
                    "team": {"name": c["teams"]["away"]["name"],
                             "logo": c["teams"]["away"]["logo"], "abbr": ""},
                    "score":     str(ag) if done and ag is not None else "",
                    "aggregate": "",
                    "winner":    aw,
                },
            })

        if matchups:
            result.append({"name": nome, "matchups": matchups})

    return result or None


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


def _parsear_entries(entries: list, forma: dict = {}) -> list:
    resultado = []
    for entry in entries:
        team  = entry.get("team", {})
        logos = team.get("logos", [])
        stats = entry.get("stats", [])
        nome  = team.get("displayName", "")
        # forma é indexada por ID; fallback para displayName caso não haja ID
        tid   = team.get("id", "") or nome
        resultado.append({
            "rank": 0,
            "team": {
                "name": nome,
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
            "forma":     forma.get(tid, forma.get(nome, [])),
        })
    resultado.sort(key=lambda t: (-t["points"], -t["goalsDiff"], -t["all"]["win"]))
    for i, t in enumerate(resultado, 1):
        t["rank"] = i
    return resultado


def _buscar_forma_times(slug: str, n: int = 5) -> dict[str, list[str]]:
    """Retorna {nome_time: ['W','D','L',...]} dos últimos n jogos finalizados."""
    hoje   = datetime.now(tz=BRT).date()
    inicio = (hoje - timedelta(days=90)).strftime("%Y%m%d")
    fim    = hoje.strftime("%Y%m%d")
    todos_eventos: list = []
    data = _espn_get(f"{ESPN_V1}/{slug}/scoreboard",
                     {"dates": f"{inicio}-{fim}", "limit": 500})
    if data and data.get("events"):
        todos_eventos = data["events"]

    # Fallback: busca por data individual a cada 3 dias (cobre ~10 rodadas)
    if len(todos_eventos) < 10:
        vistos_ids: set = set(ev.get("id", "") for ev in todos_eventos)
        d = hoje
        for _ in range(30):
            r = _espn_get(f"{ESPN_V1}/{slug}/scoreboard", {"dates": d.strftime("%Y%m%d")})
            if r:
                for ev in r.get("events", []):
                    eid = ev.get("id", "")
                    if eid and eid not in vistos_ids:
                        vistos_ids.add(eid)
                        todos_eventos.append(ev)
            d -= timedelta(days=3)
    jogos: list = []
    vistos: set = set()
    for ev in todos_eventos:
        tipo = (ev.get("status") or {}).get("type") or {}
        if not tipo.get("completed"):
            continue
        comp  = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home  = next((c for c in comps if c.get("homeAway") == "home"), {})
        away  = next((c for c in comps if c.get("homeAway") == "away"), {})
        gh    = _safe_score(home)
        ga    = _safe_score(away)
        # Usar ID do time como chave — displayName difere entre scoreboard e standings
        ic    = (home.get("team") or {}).get("id", "") or (home.get("team") or {}).get("displayName", "")
        if_ = (away.get("team") or {}).get("id", "") or (away.get("team") or {}).get("displayName", "")
        data_ev = ev.get("date", "")
        k = (data_ev, ic, if_)
        if k in vistos:
            continue
        vistos.add(k)
        if gh > ga:   rc, rf = "W", "L"
        elif gh < ga: rc, rf = "L", "W"
        else:         rc, rf = "D", "D"
        jogos.append((data_ev, ic, rc, if_, rf))
    jogos.sort(key=lambda x: x[0], reverse=True)
    forma: dict[str, list[str]] = {}
    for _, nc, rc, nf, rf in jogos:
        if nc and len(forma.get(nc, [])) < n:
            forma.setdefault(nc, []).append(rc)
        if nf and len(forma.get(nf, [])) < n:
            forma.setdefault(nf, []).append(rf)
    return forma


def _forma_html(resultados: list) -> str:
    """Bolhas coloridas de forma: V=verde, E=cinza, D=vermelho. Mais antigo à esquerda, mais recente à direita."""
    mapa = {"W": ("fw", "V"), "D": ("fd", "E"), "L": ("fl", "D")}
    # resultados[0] = mais recente; invertemos para exibir mais antigo → mais recente
    spans = "".join(
        f'<div class="fc {cls}">{letra}</div>'
        for r in reversed((resultados or [])[:5])
        for cls, letra in [mapa.get(r, ("fd", "·"))]
    )
    return f'<div class="forma">{spans}</div>'


def _logo_time_bzzoiro(nome: str, logo_map: dict) -> str:
    """Resolve logo ESPN a partir do nome do time na tabela Bzzoiro."""
    if nome in logo_map:
        return logo_map[nome]
    n = _strip_accents(nome.lower())
    for k, url in logo_map.items():
        kn = _strip_accents(k.lower())
        if kn in n or n in kn:
            return url
    for bz, espn in _BZ_ESPN_ALIASES.items():
        if _strip_accents(bz.lower()) == n:
            return logo_map.get(espn, "")
    return ""


def _bz_form_to_list(form: str) -> list[str]:
    """Converte forma Bzzoiro (ex. 'WDDDW', antigo→recente) para lista [mais recente, …]."""
    s = (form or "").strip().upper()[:5]
    return list(reversed(s)) if s else []


def _bz_row_to_team(row: dict, logo_map: dict) -> dict:
    nome = row.get("team_name", "")
    return {
        "rank": row.get("position", 0),
        "team": {"name": nome, "logo": _logo_time_bzzoiro(nome, logo_map)},
        "all": {
            "played": row.get("played", 0),
            "win":    row.get("won", 0),
            "draw":   row.get("drawn", 0),
            "lose":   row.get("lost", 0),
        },
        "goalsDiff": row.get("gd", 0),
        "points":    row.get("pts", 0),
        "forma":     _bz_form_to_list(row.get("form", "")),
    }


def buscar_tabela_bzzoiro(league_id: int) -> dict | None:
    """Classificação via Bzzoiro (temporada is_current, forma e placar atualizados)."""
    data = _bzzoiro_get(f"leagues/{league_id}/standings/")
    if not data:
        return None
    season = data.get("season") or {}
    logo_map = _buscar_logos_brasileiros()
    year = season.get("year")

    if data.get("grouped"):
        grupos = []
        for gname, rows in (data.get("groups") or {}).items():
            teams = [_bz_row_to_team(r, logo_map) for r in (rows or [])]
            if teams:
                grupos.append({"name": gname, "teams": teams})
        if not grupos:
            return None
        return {"type": "groups", "groups": grupos, "season_year": year}

    teams = [_bz_row_to_team(r, logo_map) for r in (data.get("standings") or [])]
    teams.sort(key=lambda t: t.get("rank", 99))
    if not teams:
        return None
    return {"type": "league", "teams": teams, "season_year": year}


def buscar_tabela_por_chave(chave: str) -> dict | None:
    """Tabela da liga: Bzzoiro quando disponível (BR/Lib/Sula), senão ESPN."""
    bz_id = _BZ_LIGA_TO_ID.get(chave)
    if bz_id and BZZOIRO_TOKEN:
        dados = buscar_tabela_bzzoiro(bz_id)
        if dados:
            return dados
    slug = LIGAS.get(chave)
    return buscar_tabela(slug) if slug else None


def _espn_standings_max_played(entries: list) -> int:
    mx = 0
    for e in entries:
        try:
            mx = max(mx, int(_stat(e.get("stats", []), "gamesPlayed") or 0))
        except (TypeError, ValueError):
            pass
    return mx


def _espn_standings_raw_entries(data: dict) -> list:
    if not data:
        return []
    children = data.get("children", [])
    if len(children) > 1:
        return []  # grupos tratados no fluxo principal
    if children:
        return (children[0].get("standings") or {}).get("entries", [])
    return (data.get("standings") or {}).get("entries", [])


def buscar_tabela(slug: str) -> dict | None:
    """Retorna standings. Ligas com grupos → {"type":"groups","groups":[{name,teams}]}.
    Ligas normais → {"type":"league","teams":[...]}."""
    ano  = datetime.now(tz=BRT).year
    data = None
    for season in (ano, ano - 1):
        cand = _espn_get(f"{ESPN_V2}/{slug}/standings", {"season": season})
        if not cand:
            continue
        # Múltiplos grupos (Libertadores etc.) — aceita na primeira resposta válida
        if len(cand.get("children", [])) > 1:
            data = cand
            break
        entries = _espn_standings_raw_entries(cand)
        if not entries:
            continue
        # Não usar temporada anterior já encerrada (ex.: 38 rodadas em maio/2026)
        if season < ano and _espn_standings_max_played(entries) >= 34:
            continue
        data = cand
        break
    if not data:
        return None
    forma = _buscar_forma_times(slug)
    try:
        children = data.get("children", [])

        # Múltiplos filhos = liga com grupos (Libertadores, Champions, etc.)
        if len(children) > 1:
            grupos = []
            for child in children:
                nome_grupo = child.get("name") or child.get("abbreviation") or "Grupo"
                entries    = (child.get("standings") or {}).get("entries", [])
                times      = _parsear_entries(entries, forma)
                if times:
                    grupos.append({"name": nome_grupo, "teams": times})
            return {"type": "groups", "groups": grupos} if grupos else None

        # Um único filho ou fallback direto
        entries = []
        if children:
            entries = (children[0].get("standings") or {}).get("entries", [])
        if not entries:
            entries = (data.get("standings") or {}).get("entries", [])

        if not entries:
            print(f"[ESPN] standings vazio para {slug}")
            return None

        teams = _parsear_entries(entries, forma)
        return {"type": "league", "teams": teams} if teams else None
    except Exception as e:
        print(f"[ESPN] Erro ao parsear standings {slug}: {e}")
        return None


def _strip_accents(s: str) -> str:
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def _normalizar_canal(nome: str) -> str:
    """Normaliza nomes de canais: remove acentos, ESPN2→ESPN 2, etc."""
    nome = _strip_accents(nome.strip())
    return re.sub(r'(?i)^(ESPN|SPORTV|SPORTTV)(\d)$', r'\1 \2', nome)


def _extrair_meta(comp: dict) -> dict:
    """Extrai transmissão, estádio e odds de um objeto competition da ESPN."""
    broadcasts = []
    for b in (comp.get("geoBroadcasts") or []):
        media = b.get("media") or {}
        nome  = media.get("shortName", "") or media.get("callLetters", "")
        nome  = _normalizar_canal(nome)
        if nome and nome not in broadcasts:
            broadcasts.append(nome)
    if not broadcasts and comp.get("broadcast"):
        broadcasts = [_normalizar_canal(comp["broadcast"])]

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
    "BRA.1":                    ["Globo", "SporTV", "SporTV 2", "SporTV 3", "Premiere", "Caze"],
    "BRA.2":                    ["SporTV", "SporTV 2", "SporTV 3", "Premiere", "ESPN", "Band"],
    "BRA.3":                    ["DAZN"],
    "BRA.CUP":                  ["Amazon", "SporTV", "SporTV 2", "Globo"],
    "CONMEBOL.LIBERTADORES":    ["ESPN", "ESPN 2", "ESPN 3", "Disney+", "SBT"],
    "CONMEBOL.SUDAMERICANA":    ["ESPN", "ESPN 2", "ESPN 3", "Disney+"],
    "UEFA.CHAMPIONS":           ["TNT", "HBO Max", "SBT"],
    "UEFA.EUROPA":              ["TNT", "HBO Max"],
    "UEFA.EUROPA.CONFERENCE":   ["TNT", "HBO Max"],
    "eng.1":                    ["ESPN", "ESPN 2", "ESPN 3", "Disney+", "Star+"],
    "ESP.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+"],
    "ITA.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+"],
    "GER.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+", "RedeTV"],
    "FRA.1":                    ["ESPN", "ESPN 2", "Disney+", "Star+"],
    "fifa.friendly":            ["SporTV", "SporTV 2", "SporTV 3", "Globo", "Band"],
    "fifa.world":               ["Globo", "SporTV", "SporTV 2", "SporTV 3", "Band"],
}


# Famílias de canais: quando a transmissão é genérica (ex: "ESPN"),
# apresentamos todos os sub-canais para o usuário escolher
_FAMILIA_CANAIS: dict[str, list[str]] = {
    "espn":    ["ESPN", "ESPN 2", "ESPN 3"],
    "sportv":  ["SporTV", "SporTV 2", "SporTV 3"],
    "premiere": ["Premiere"],
}


def _familia_canal(nome: str) -> list[str] | None:
    """Se o nome for um canal-base de família, retorna todos os sub-canais; senão None."""
    return _FAMILIA_CANAIS.get(nome.lower().strip())


def _canais_tv(jogo: dict, slug: str = "") -> list[str]:
    """Retorna lista de canais ESPN (da API) + mapeamento fixo por liga."""
    espn = list((jogo.get("meta") or {}).get("broadcasts", []))
    fixos = _TV_POR_LIGA.get(slug, [])
    for c in fixos:
        if c not in espn:
            espn.append(c)
    return espn



def buscar_artilheiros(slug: str) -> list:
    """Retorna top artilheiros de uma liga via ESPN statistics endpoint."""
    data = _espn_get(f"{ESPN_V1}/{slug}/statistics")
    if not data:
        return []
    try:
        for categoria in data.get("stats", []):
            nome_cat = (categoria.get("name") or "").lower()
            disp_cat = (categoria.get("displayName") or "").lower()
            if "goal" in nome_cat or "goal" in disp_cat:
                return categoria.get("leaders", [])
        cats = data.get("stats", [])
        return cats[0].get("leaders", []) if cats else []
    except Exception as e:
        print(f"[ESPN] Erro statistics {slug}: {e}")
        return []


# Aliases → nome ESPN (seleções nacionais)
_SELECOES_ALIASES: dict[str, str] = {
    "brasil": "Brazil", "brazil": "Brazil",
    "selecao brasileira": "Brazil", "seleção brasileira": "Brazil",
    "argentina": "Argentina", "uruguai": "Uruguay", "uruguay": "Uruguay",
    "paraguai": "Paraguay", "paraguay": "Paraguay", "chile": "Chile",
    "colombia": "Colombia", "colômbia": "Colombia",
    "equador": "Ecuador", "ecuador": "Ecuador",
    "peru": "Peru", "bolivia": "Bolivia", "bolívia": "Bolivia",
    "venezuela": "Venezuela",
    "alemanha": "Germany", "germany": "Germany",
    "franca": "France", "france": "France",
    "espanha": "Spain", "spain": "Spain",
    "italia": "Italy", "itália": "Italy", "italy": "Italy",
    "inglaterra": "England", "england": "England",
    "portugal": "Portugal", "holanda": "Netherlands", "netherlands": "Netherlands",
    "belgica": "Belgium", "bélgica": "Belgium", "belgium": "Belgium",
    "croacia": "Croatia", "croácia": "Croatia", "croatia": "Croatia",
    "japao": "Japan", "japão": "Japan", "japan": "Japan",
    "mexico": "Mexico", "méxico": "Mexico",
    "eua": "United States", "estados unidos": "United States",
    "usa": "United States",
}
_LIGAS_SELECAO = frozenset({"amistosos", "copadomundo"})


# Mapa normalizado (sem acentos/pontuação) → nome ESPN, aceita digitação com ou sem acento
_SELECOES_NORM: dict[str, str] = {}
for _k, _v in _SELECOES_ALIASES.items():
    _SELECOES_NORM[_strip_accents(_k.lower().strip())] = _v
    _SELECOES_NORM[_strip_accents(_v.lower().strip())] = _v


def _canonical_selecao(nome: str) -> str | None:
    """Nome canônico ESPN se for busca por seleção; aceita com ou sem acento. Senão None."""
    n = _strip_accents((nome or "").lower().strip())
    return _SELECOES_NORM.get(n)


def buscar_time_id(slug: str, nome_time: str) -> tuple | None:
    """Busca team_id e nome oficial pelo nome parcial."""
    data = _espn_get(f"{ESPN_V1}/{slug}/teams", {"limit": 200})
    if not data:
        return None
    candidatos = [nome_time]
    canon = _canonical_selecao(nome_time)
    if canon and canon not in candidatos:
        candidatos.insert(0, canon)
    melhor = None
    melhor_score = 0
    for nome in candidatos:
        busca = _strip_accents(nome.lower())
        for t in data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", []):
            team = t.get("team", {})
            nomes = [
                team.get("displayName", ""),
                team.get("shortDisplayName", ""),
                team.get("abbreviation", ""),
                team.get("nickname", ""),
            ]
            for raw in nomes:
                n = _strip_accents(raw.lower())
                if not n:
                    continue
                if busca == n or busca in n or n in busca:
                    score = len(set(busca) & set(n)) + (10 if busca == n else 0)
                    if score > melhor_score:
                        melhor_score = score
                        melhor = (str(team.get("id", "")), team.get("displayName", nome_time))
    return melhor


def buscar_proximos_jogos(slug: str, team_id: str, limite: int = PROXIMOS_PADRAO) -> list:
    """Retorna próximos fixtures de um time."""
    limite = _normalizar_limite_proximos(limite)
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
                "_slug": slug,
            })
            if len(proximos) >= limite:
                break
        return proximos
    except Exception as e:
        print(f"[ESPN] Erro schedule {team_id}: {e}")
        return []


def buscar_proximos_espn_clube(slug: str, team_id: str, limite: int = PROXIMOS_PADRAO) -> list:
    """Schedule + scoreboard ESPN até `limite` jogos futuros."""
    limite = _normalizar_limite_proximos(limite)
    jogos  = buscar_proximos_jogos(slug, team_id, limite)
    if len(jogos) < limite:
        vistos = {j["fixture"]["id"] for j in jogos}
        for j in buscar_proximos_jogos_scoreboard(slug, team_id, limite - len(jogos)):
            if j["fixture"]["id"] not in vistos:
                jogos.append(j)
                vistos.add(j["fixture"]["id"])
    return jogos[:limite]


def _espn_ev_to_fixture_proximo(ev: dict, slug: str = "") -> dict:
    """Converte evento ESPN em fixture interno (jogo futuro)."""
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    return {
        "fixture": {
            "id": ev.get("id", "0"),
            "date": ev.get("date", ""),
            "status": {"short": "NS", "elapsed": None},
        },
        "teams": {
            "home": {
                "name": (home.get("team") or {}).get("displayName", ""),
                "logo": (home.get("team") or {}).get("logo", ""),
            },
            "away": {
                "name": (away.get("team") or {}).get("displayName", ""),
                "logo": (away.get("team") or {}).get("logo", ""),
            },
        },
        "goals": {"home": None, "away": None},
        "meta": _extrair_meta(comp),
        "_slug": slug,
    }


def buscar_proximos_jogos_scoreboard(slug: str, team_id: str, limite: int = 5) -> list:
    """Próximos jogos via scoreboard (schedule da ESPN costuma incompleto para seleções).

    A ESPN retorna vazio quando o intervalo `dates` passa de ~1 ano, então
    consultamos em janelas de 120 dias até juntar jogos suficientes.
    """
    tid     = str(team_id)
    hoje    = datetime.now(tz=BRT).date()
    hoje_dt = datetime.now(tz=BRT)
    vistos: set[str] = set()
    candidatos: list[tuple[datetime, dict]] = []

    for offset in range(0, 360, 120):
        ini = (hoje + timedelta(days=offset)).strftime("%Y%m%d")
        fim = (hoje + timedelta(days=offset + 120)).strftime("%Y%m%d")
        data = _espn_get(f"{ESPN_V1}/{slug}/scoreboard", {"dates": f"{ini}-{fim}", "limit": 300})
        if not data:
            continue
        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            ids  = {str((c.get("team") or {}).get("id", "")) for c in comp.get("competitors", [])}
            if tid not in ids:
                continue
            eid = str(ev.get("id", ""))
            if eid in vistos:
                continue
            try:
                dt = datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00")).astimezone(BRT)
            except Exception:
                continue
            if dt < hoje_dt:
                continue
            vistos.add(eid)
            candidatos.append((dt, _espn_ev_to_fixture_proximo(ev, slug)))
        if len(candidatos) >= limite:
            break

    candidatos.sort(key=lambda x: x[0])
    return [f for _, f in candidatos[:limite]]


def buscar_time_id_selecao(nome_time: str) -> tuple[str, str, str] | None:
    """(liga_key, team_id, nome_oficial) em amistosos ou copadomundo."""
    ligas = list(_LIGAS_SELECAO)
    for liga_key in ligas:
        slug = LIGAS.get(liga_key)
        if not slug:
            continue
        hit = buscar_time_id(slug, nome_time)
        if hit:
            return liga_key, hit[0], hit[1]
    return None


def buscar_proximos_selecao(
    nome_time: str,
    liga_key: str | None = None,
    limite: int = PROXIMOS_PADRAO,
) -> tuple[list, str] | None:
    """Próximos jogos de seleção nacional via ESPN (amistosos + Copa do Mundo)."""
    limite = _normalizar_limite_proximos(limite)
    if liga_key and liga_key in _LIGAS_SELECAO:
        slug = LIGAS.get(liga_key)
        if not slug:
            return None
        hit = buscar_time_id(slug, nome_time)
        if not hit:
            return None
        team_id, display = hit
        ligas_busca = [liga_key]
    else:
        if not _canonical_selecao(nome_time):
            return None
        hit3 = buscar_time_id_selecao(nome_time)
        if not hit3:
            return None
        liga_key, team_id, display = hit3
        ligas_busca = list(_LIGAS_SELECAO)

    vistos: set[str] = set()
    merged: list[tuple[str, dict]] = []

    for lk in ligas_busca:
        slug = LIGAS.get(lk)
        if not slug:
            continue
        for j in buscar_proximos_jogos(slug, team_id, limite):
            eid = j["fixture"]["id"]
            if eid not in vistos:
                vistos.add(eid)
                merged.append((j["fixture"]["date"], j))
        falta = limite - len(merged)
        if falta > 0:
            for j in buscar_proximos_jogos_scoreboard(slug, team_id, limite=falta):
                eid = j["fixture"]["id"]
                if eid not in vistos:
                    vistos.add(eid)
                    merged.append((j["fixture"]["date"], j))

    merged.sort(key=lambda x: x[0])
    fixtures = [j for _, j in merged[:limite]]
    return (fixtures, display) if fixtures else None


def _e_hoje(date_str: str) -> bool:
    """Retorna True se a data do jogo (UTC) cair no dia de hoje em BRT."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(BRT).date() == datetime.now(tz=BRT).date()
    except Exception:
        return True   # em caso de erro, inclui o jogo


def buscar_jogos_do_dia(slug: str, data_yyyymmdd: str = None) -> list:
    """Busca jogos de uma liga. data_yyyymmdd=None → hoje; caso contrário usa a data fornecida."""
    hoje_str   = datetime.now(tz=BRT).strftime("%Y%m%d")
    data_busca = data_yyyymmdd or hoje_str
    params     = {"dates": data_busca}
    data = _espn_get(f"{ESPN_V1}/{slug}/scoreboard", params)
    if not data:
        return []

    if data_yyyymmdd:
        dt_alvo = datetime.strptime(data_yyyymmdd, "%Y%m%d").date()
        def _filtro(d: str) -> bool:
            try:
                return datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone(BRT).date() == dt_alvo
            except Exception:
                return True
    else:
        _filtro = _e_hoje

    try:
        jogos = []
        for ev in data.get("events", []):
            if not _filtro(ev.get("date", "")):
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
                # ESPN sometimes delays updating state; if kickoff was 2+ min ago, treat as live
                try:
                    match_dt = datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) >= match_dt.astimezone(timezone.utc) + timedelta(minutes=2):
                        short = "1H"
                    else:
                        short = "NS"
                except Exception:
                    short = "NS"
            elif state == "post":
                short = "FT"
            elif type_name == "STATUS_HALFTIME":
                short = "HT"
            else:
                short = "1H"

            elapsed = None
            if short == "1H" and state != "post" and type_name != "STATUS_HALFTIME":
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
            pen   = f.get("penalty", {})
            ph, pa = pen.get("home"), pen.get("away")
            pen_html = (f'<div class="pen-score">({ph} × {pa} pen.)</div>'
                        if status == "PEN" and ph is not None else "")
            placar_html = (
                f'<div class="placar">{g_casa} <span class="sep">×</span> {g_fora}</div>'
                f'<div class="label-enc">{label}</div>'
                f'{pen_html}'
            )
            cls_card = " enc"
        elif status == "HT":
            placar_html = (
                f'<div class="placar live">{g_casa} <span class="sep">×</span> {g_fora}</div>'
                f'<div class="label-live">Intervalo</div>'
            )
            cls_card = " live"
        elif status == "1H" and elapsed is None:
            # Confronto agregado: ida jogada, volta pendente
            placar_html = (
                f'<div class="placar">{g_casa} <span class="sep">×</span> {g_fora}</div>'
                f'<div class="label-enc">Agg · Volta pend.</div>'
            )
            cls_card = ""
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
.pen-score{font-size:11px;color:#93c5fd;margin-top:2px}
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
        (2,    "za", "#3b82f6", "Oitavas de Final"),
        (3,    "zb", "#f59e0b", "Copa Sudamericana"),
        ("b1", "zc", "#ef4444", "Eliminado"),
    ],
    "CONMEBOL.SUDAMERICANA": [
        (2,    "za", "#3b82f6", "Eliminatórias"),
        (3,    "zb", "#f59e0b", "Próxima fase"),
        ("b1", "zc", "#ef4444", "Eliminado"),
    ],
    "fifa.world": [
        (2,    "za", "#3b82f6", "Oitavas de Final"),
        ("b2", "zc", "#ef4444", "Eliminado"),
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
            f'<td class="pts">{time["points"]}</td>'
            f'<td>{time["all"]["played"]}</td>'
            f'<td>{time["all"]["win"]}</td>'
            f'<td>{time["all"]["draw"]}</td>'
            f'<td>{time["all"]["lose"]}</td>'
            f'<td>{sg_str}</td>'
            f'<td class="forma-col">{_forma_html(time.get("forma", []))}</td>'
            f'</tr>'
        )

    legenda_html = "".join(
        f'<div class="leg"><div class="dot" style="background:{cor}"></div>{label}</div>'
        for _, _, cor, label in zonas
    )

    css = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;padding:22px;width:740px}}
.titulo{{font-size:20px;font-weight:700;color:#fff;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px}}
col.c-pos{{width:30px}}col.c-clube{{width:auto}}
col.c-stat{{width:34px}}col.c-sg{{width:38px}}col.c-pts{{width:40px}}col.c-forma{{width:112px}}
th{{color:#8892a4;font-weight:500;padding:7px 8px;border-bottom:2px solid #0f3460;
   text-align:center;font-size:11px;text-transform:uppercase;letter-spacing:.7px;overflow:hidden}}
th.th-clube{{text-align:left;padding-left:10px}}
td{{padding:9px 8px;border-bottom:1px solid #1e2f5e;text-align:center;vertical-align:middle;overflow:hidden}}
td.time-col{{text-align:left;display:flex;align-items:center;gap:9px;padding-left:10px}}
td.pos{{color:#8892a4;font-size:12px}}
td.pts{{font-weight:700;color:#fff;font-size:14px}}
td.forma-col{{padding:6px 4px}}
tr:hover td{{background:#1a2a5e}}
.sigla{{background:#0f3460;color:#ccc;padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}}
{css_zonas}
.forma{{display:flex;gap:3px;align-items:center;justify-content:center}}
.fc{{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;
     justify-content:center;font-size:8px;font-weight:800;color:#fff;flex-shrink:0}}
.fw{{background:#22c55e}}.fd{{background:#6b7280}}.fl{{background:#ef4444}}
.legenda{{display:flex;gap:18px;margin-top:13px;font-size:11px;color:#8892a4;flex-wrap:wrap}}
.leg{{display:flex;align-items:center;gap:5px}}
.dot{{width:10px;height:10px;border-radius:2px}}
"""

    ano = datetime.now().year
    colgroup = (
        '<colgroup>'
        '<col class="c-pos"><col class="c-clube">'
        '<col class="c-pts"><col class="c-stat"><col class="c-stat"><col class="c-stat"><col class="c-stat">'
        '<col class="c-sg"><col class="c-forma">'
        '</colgroup>'
    )
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
        f'<div class="titulo">🏆 {nome_liga} — Classificação {ano}</div>'
        f'<table>{colgroup}<thead><tr>'
        f'<th>#</th><th class="th-clube">Clube</th>'
        f'<th>PTS</th><th>PJ</th><th>V</th><th>E</th><th>D</th><th>SG</th><th>ÚLT. 5</th>'
        f'</tr></thead><tbody>{linhas}</tbody></table>'
        f'<div class="legenda">{legenda_html}</div>'
        f'</body></html>'
    )

    nome_arquivo = f"tabela_{nome_liga.lower().replace(' ', '')}.png"
    return await _html_para_png(html, nome_arquivo, width=780)


async def gerar_tabela_grupos_png(grupos: list, nome_liga: str, slug: str = "") -> str:
    """Gera imagem da tabela com múltiplos grupos (Libertadores, etc.) em grid 2 colunas."""
    todas_urls = [
        t["team"].get("logo", "")
        for g in grupos for t in g["teams"]
    ]
    logos = await _baixar_logos_paralelo(todas_urls)

    zonas = _ZONAS_LIGA.get(slug, _ZONAS_LIGA["CONMEBOL.LIBERTADORES"])

    css_zonas = ""
    for _, cls, cor, _ in zonas:
        css_zonas += f"tr.{cls} td:first-child{{border-left:3px solid {cor}}}\n"

    legenda_html = "".join(
        f'<div class="leg"><div class="dot" style="background:{cor}"></div>{label}</div>'
        for _, _, cor, label in zonas
    )

    colgroup = (
        '<colgroup>'
        '<col class="c-pos"><col class="c-clube">'
        '<col class="c-pts"><col class="c-stat"><col class="c-stat"><col class="c-stat"><col class="c-stat">'
        '<col class="c-sg"><col class="c-forma">'
        '</colgroup>'
    )

    blocos = ""
    for grupo in grupos:
        times  = grupo["teams"]
        total  = len(times)
        linhas = ""
        for time in times:
            rank   = time["rank"]
            nome   = time["team"]["name"]
            b64    = logos.get(time["team"].get("logo", ""), "")
            zona   = _zona_rank(rank, total, zonas)
            sg     = time["goalsDiff"]
            sg_str = f"+{sg}" if sg > 0 else str(sg)
            linhas += (
                f'<tr class="{zona}">'
                f'<td class="pos">{rank}</td>'
                f'<td class="time-col">{_img_tag(b64, nome, 18)}<span>{nome}</span></td>'
                f'<td class="pts">{time["points"]}</td>'
                f'<td>{time["all"]["played"]}</td>'
                f'<td>{time["all"]["win"]}</td>'
                f'<td>{time["all"]["draw"]}</td>'
                f'<td>{time["all"]["lose"]}</td>'
                f'<td>{sg_str}</td>'
                f'<td class="forma-col">{_forma_html(time.get("forma", []))}</td>'
                f'</tr>'
            )
        blocos += (
            f'<div class="grupo">'
            f'<div class="grupo-nome">{grupo["name"]}</div>'
            f'<table>{colgroup}<thead><tr>'
            f'<th>#</th><th class="th-clube">Clube</th>'
            f'<th>PTS</th><th>PJ</th><th>V</th><th>E</th><th>D</th><th>SG</th><th>ÚLT. 5</th>'
            f'</tr></thead><tbody>{linhas}</tbody></table>'
            f'</div>'
        )

    css = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;padding:22px;width:1500px}}
.titulo{{font-size:20px;font-weight:700;color:#fff;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.grupo{{background:#1a2744;border-radius:8px;padding:12px}}
.grupo-nome{{font-size:13px;font-weight:700;color:#93c5fd;margin-bottom:8px;text-transform:uppercase;letter-spacing:.5px}}
table{{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12px}}
col.c-pos{{width:24px}}col.c-clube{{width:auto}}
col.c-stat{{width:30px}}col.c-sg{{width:34px}}col.c-pts{{width:36px}}col.c-forma{{width:100px}}
th{{color:#8892a4;font-weight:500;padding:5px 6px;border-bottom:2px solid #0f3460;
   text-align:center;font-size:10px;text-transform:uppercase;letter-spacing:.5px;overflow:hidden}}
th.th-clube{{text-align:left;padding-left:8px}}
td{{padding:7px 6px;border-bottom:1px solid #1e2f5e;text-align:center;vertical-align:middle;overflow:hidden}}
td.time-col{{text-align:left;display:flex;align-items:center;gap:7px;padding-left:8px}}
td.pos{{color:#8892a4;font-size:11px}}
td.pts{{font-weight:700;color:#fff;font-size:13px}}
td.forma-col{{padding:5px 3px}}
tr:last-child td{{border-bottom:none}}
{css_zonas}
.forma{{display:flex;gap:3px;align-items:center;justify-content:center}}
.fc{{width:16px;height:16px;border-radius:50%;display:flex;align-items:center;
     justify-content:center;font-size:7px;font-weight:800;color:#fff;flex-shrink:0}}
.fw{{background:#22c55e}}.fd{{background:#6b7280}}.fl{{background:#ef4444}}
.legenda{{display:flex;gap:18px;margin-top:16px;font-size:11px;color:#8892a4;flex-wrap:wrap}}
.leg{{display:flex;align-items:center;gap:5px}}
.dot{{width:10px;height:10px;border-radius:2px}}
.sigla{{background:#0f3460;color:#ccc;padding:2px 4px;border-radius:3px;font-size:9px;font-weight:700}}
"""
    ano  = datetime.now().year
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<style>{css}</style></head><body>'
        f'<div class="titulo">🏆 {nome_liga} — Fase de Grupos {ano}</div>'
        f'<div class="grid">{blocos}</div>'
        f'<div class="legenda">{legenda_html}</div>'
        f'</body></html>'
    )

    nome_arquivo = f"tabela_{nome_liga.lower().replace(' ', '')}_grupos.png"
    return await _html_para_png(html, nome_arquivo, width=1540)


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


async def gerar_evento_cover_png(jogo: dict, liga_nome: str) -> str:
    """Capa 16:9 (960×540) para evento agendado do Discord."""
    nome_casa = jogo["teams"]["home"]["name"]
    nome_fora = jogo["teams"]["away"]["name"]
    urls = [u for u in (
        jogo["teams"]["home"].get("logo", ""),
        jogo["teams"]["away"].get("logo", ""),
    ) if u]
    logos = await _baixar_logos_paralelo(urls) if urls else {}
    b64_casa = logos.get(jogo["teams"]["home"].get("logo", ""), "")
    b64_fora = logos.get(jogo["teams"]["away"].get("logo", ""), "")

    try:
        dt = datetime.fromisoformat(jogo["fixture"]["date"].replace("Z", "+00:00")).astimezone(BRT)
        data_linha = dt.strftime("%d/%m/%Y")
        hora_linha = dt.strftime("%H:%M")
    except Exception:
        data_linha, hora_linha = "—", "—"

    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:960px;height:540px;background:linear-gradient(145deg,#0f172a 0%,#1e3a5f 45%,#16213e 100%);
     color:#fff;font-family:'Segoe UI',Arial,sans-serif;display:flex;flex-direction:column;
     align-items:center;justify-content:center;overflow:hidden;position:relative}
.bg{position:absolute;inset:0;opacity:.08;background:radial-gradient(circle at 20% 50%,#3b82f6 0%,transparent 50%),
     radial-gradient(circle at 80% 50%,#ef4444 0%,transparent 50%)}
.liga{font-size:15px;font-weight:600;color:#93c5fd;letter-spacing:1.2px;text-transform:uppercase;
      margin-bottom:28px;z-index:1}
.confronto{display:flex;align-items:center;justify-content:center;gap:36px;width:100%;padding:0 48px;z-index:1}
.lado{display:flex;flex-direction:column;align-items:center;gap:14px;flex:1;max-width:320px}
.lado .nome{font-size:22px;font-weight:800;text-align:center;line-height:1.2}
.lado img{width:88px;height:88px;object-fit:contain;filter:drop-shadow(0 4px 12px rgba(0,0,0,.4))}
.vs{font-size:42px;font-weight:900;color:#64748b;flex-shrink:0}
.horario{margin-top:32px;font-size:20px;font-weight:700;color:#e2e8f0;z-index:1}
.horario span{color:#93c5fd;font-size:28px;margin-left:8px}
.rodape{position:absolute;bottom:18px;font-size:11px;color:#475569;letter-spacing:.5px}
"""
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{css}</style></head><body>'
        f'<div class="bg"></div>'
        f'<div class="liga">⚽ {liga_nome}</div>'
        f'<div class="confronto">'
        f'<div class="lado">'
        f'{_img_tag(b64_casa, nome_casa, 88)}'
        f'<span class="nome">{nome_casa}</span></div>'
        f'<div class="vs">×</div>'
        f'<div class="lado">'
        f'{_img_tag(b64_fora, nome_fora, 88)}'
        f'<span class="nome">{nome_fora}</span></div>'
        f'</div>'
        f'<div class="horario">{data_linha} · <span>{hora_linha}</span> BRT</div>'
        f'<div class="rodape">Futebol</div>'
        f'</body></html>'
    )
    return await _html_para_png(html, "evento_cover_temp.png", width=960)


async def gerar_artilheiro_png(artilheiros: list, nome_liga: str) -> str:
    # Coleta todas as URLs de logos para download paralelo
    logo_urls = []
    parsed = []
    for art in artilheiros[:15]:
        atleta   = art.get("athlete") or {}
        time_obj = atleta.get("team") or {}
        nome     = atleta.get("displayName", "?")
        time_nm  = time_obj.get("displayName", "")
        gols     = int(float(art.get("value") or 0))
        logos    = time_obj.get("logos") or []
        logo_url = logos[0].get("href", "") if logos else ""
        logo_urls.append(logo_url)
        parsed.append({"nome": nome, "time": time_nm, "gols": gols, "logo_url": logo_url})

    logos_b64 = await _baixar_logos_paralelo([u for u in logo_urls if u])
    linhas = ""
    for i, p in enumerate(parsed, 1):
        b64      = logos_b64.get(p["logo_url"], "")
        img      = (f'<img src="{b64}" width="22" height="22" style="object-fit:contain">'
                    if b64 else f'<span class="sigla">{p["time"][:3].upper()}</span>')
        destaque = ' class="top"' if i == 1 else ""
        linhas  += (
            f'<tr{destaque}>'
            f'<td class="pos">{i}</td>'
            f'<td class="atleta">{img}<span>{p["nome"]}</span></td>'
            f'<td class="clube">{p["time"]}</td>'
            f'<td class="gols">{p["gols"]}</td>'
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


# Mapeamento de palavras-chave para ordenação de rodadas
_ROUND_ORDER_KEYS: list[tuple[str, int]] = [
    ("playoff", 0), ("play-off", 0),
    ("round of 64", 1),
    ("round of 32", 2), ("32 avos", 2),
    ("round of 16", 3), ("16 avos", 3), ("oitavas", 3),
    ("quarterfinal", 4), ("quartas", 4), ("quarter", 4),
    ("semifinal", 5), ("semi-final", 5), ("semis", 5),
    ("final", 6),
]

# Mapa de season.slug da ESPN → nome legível para o chaveamento
_SEASON_SLUG_DISPLAY: dict[str, str] = {
    "knockout-round-playoffs":  "Playoffs",
    "knockout-round-play-offs": "Playoffs",
    "round-of-32":              "16 Avos de Final",
    "round-of-16":              "Oitavas de Final",
    "quarterfinals":            "Quartas de Final",
    "quarter-finals":           "Quartas de Final",
    "semifinals":               "Semifinais",
    "semi-finals":              "Semifinais",
    "final":                    "Final",
    # CONMEBOL Libertadores / Sul-Americana (ESPN não expõe /bracket — só scoreboard)
    "first-stage":              "Primeira Fase",
    "second-stage":             "Oitavas de Final",
    "third-stage":              "Quartas de Final",
    "fourth-stage":             "Semifinais",
}
# Fases de pontos corridos — não entram no bracket mata-mata
_BRACKET_SKIP_SLUGS = frozenset({
    "group-stage", "league-stage", "regular-season", "group",
})


def _round_sort_key(name: str) -> int:
    n = name.lower()
    for kw, order in _ROUND_ORDER_KEYS:
        if kw in n:
            return order
    return 99


def _normalize_round_name(name: str) -> str:
    """Remove info de ida/volta: '1st Leg', 'Ida', 'Volta', '- 2'..."""
    name = re.sub(
        r'\s*[-–]\s*(\d+(st|nd|rd|th)?\s*(leg|jogo)|ida|volta)\s*$',
        '', name, flags=re.IGNORECASE,
    ).strip()
    return name


def _parsear_bracket_rounds(rounds_raw: list) -> list | None:
    """Converte rounds da ESPN bracket API para [{name, order, matchups}]."""
    resultado = []
    for r in rounds_raw:
        matchups = []
        for m in r.get("matchups", []):
            comps = m.get("competitors", [])
            if len(comps) < 2:
                continue
            def _parse_comp(c):
                team = c.get("team") or {}
                agg  = (c.get("aggregate") or {})
                agg_v = (agg.get("score") or {}).get("displayValue", "")
                s = c.get("score") or {}
                score = s.get("displayValue", "") if isinstance(s, dict) else str(s)
                return {
                    "team": {"name": team.get("displayName", "TBD"),
                             "logo": team.get("logo", ""),
                             "abbr": team.get("abbreviation", "")},
                    "score":     score,
                    "aggregate": agg_v,
                    "winner":    c.get("winner", False) or agg.get("winner", False),
                }
            matchups.append({"home": _parse_comp(comps[0]), "away": _parse_comp(comps[1])})
        if matchups:
            resultado.append({"name": r.get("name", "Rodada"),
                               "order": _round_sort_key(r.get("name", "")),
                               "matchups": matchups})
    if not resultado:
        return None
    resultado.sort(key=lambda r: r["order"])
    return resultado


def _bracket_via_scoreboard_wide(slug: str) -> list | None:
    """Busca TODOS os jogos mata-mata (6 meses) e agrupa por rodada com placar agregado."""
    hoje   = datetime.now(tz=BRT).date()
    inicio = (hoje - timedelta(days=210)).strftime("%Y%m%d")
    fim    = (hoje + timedelta(days=90)).strftime("%Y%m%d")

    eventos: list = []
    # Tentativa com date range
    data = _espn_get(f"{ESPN_V1}/{slug}/scoreboard",
                     {"dates": f"{inicio}-{fim}", "limit": 500})
    if data and data.get("events"):
        eventos = data["events"]
        print(f"[Bracket] {len(eventos)} eventos via date range para {slug}")

    if not eventos:
        # Fallback: chamadas mensais (8 meses)
        vistos: set = set()
        d = hoje
        for _ in range(9):
            r = _espn_get(f"{ESPN_V1}/{slug}/scoreboard",
                          {"dates": d.strftime("%Y%m%d")})
            if r:
                for ev in r.get("events", []):
                    eid = ev.get("id", "")
                    if eid and eid not in vistos:
                        vistos.add(eid)
                        eventos.append(ev)
            d -= timedelta(days=28)
        print(f"[Bracket] {len(eventos)} eventos via chamadas mensais para {slug}")

    if not eventos:
        return None

    # rounds_map: round_name -> {tie_key -> dict}
    rounds_map:      dict[str, dict] = {}
    round_sort_idx:  dict[str, int]  = {}

    for ev in eventos:
        comp        = (ev.get("competitions") or [{}])[0]
        notes       = comp.get("notes") or []
        # Preferir season.slug (ESPN guarda o nome da fase aí, ex: 'round-of-16')
        season_slug = (ev.get("season") or {}).get("slug", "")
        if season_slug in _BRACKET_SKIP_SLUGS:
            continue
        if season_slug and season_slug in _SEASON_SLUG_DISPLAY:
            round_name = _SEASON_SLUG_DISPLAY[season_slug]
        else:
            raw_name   = ((notes[0].get("headline", "") if notes else "")
                          or (ev.get("week") or {}).get("displayValue", "")
                          or "")
            raw_name   = _normalize_round_name(raw_name)
            # Ida/volta sem slug de fase: ignora (evita centenas de "2nd Leg - ..." soltos)
            if not raw_name or re.search(r"(?i)\b(leg|ida|volta)\b", raw_name):
                continue
            round_name = raw_name
        competitors = comp.get("competitors") or []
        home  = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away  = next((c for c in competitors if c.get("homeAway") == "away"), {})
        ht    = home.get("team") or {}
        at    = away.get("team") or {}
        hn    = ht.get("displayName", "")
        an    = at.get("displayName", "")

        completed = (comp.get("status") or {}).get("type", {}).get("completed", False)
        date_ev   = ev.get("date", "")
        gh = _safe_score(home)
        ga = _safe_score(away)

        tie_key = tuple(sorted([hn, an]))
        if round_name not in rounds_map:
            rounds_map[round_name]     = {}
            round_sort_idx[round_name] = _round_sort_key(round_name)

        if tie_key not in rounds_map[round_name]:
            rounds_map[round_name][tie_key] = {
                "home_name": hn,  "away_name": an,
                "home_team": ht,  "away_team": at,
                "home_goals": 0,  "away_goals": 0,
                "n_legs": 0,
                "completed": False,
                "date": date_ev,
            }
        tie = rounds_map[round_name][tie_key]
        if completed:
            tie["home_goals"] += gh
            tie["away_goals"] += ga
            tie["n_legs"]     += 1
            tie["completed"]   = True
        if date_ev and (not tie["date"] or date_ev < tie["date"]):
            tie["date"] = date_ev

    # Filtra apenas rodadas mata-mata (exclui matchdays/fase de liga)
    rounds_map = {rn: ties for rn, ties in rounds_map.items()
                  if round_sort_idx.get(rn, 99) < 99}
    round_sort_idx = {rn: v for rn, v in round_sort_idx.items() if rn in rounds_map}
    if not rounds_map:
        return None

    # Monta resultado ordenado por rodada
    sorted_rnames = sorted(rounds_map.keys(),
                           key=lambda rn: (round_sort_idx[rn], rn))
    result = []
    for rn in sorted_rnames:
        ties_sorted = sorted(rounds_map[rn].values(), key=lambda t: t["date"])
        matchups = []
        for tie in ties_sorted:
            hg, ag = tie["home_goals"], tie["away_goals"]
            done   = tie["completed"]
            hw     = done and hg > ag
            aw     = done and ag > hg
            matchups.append({
                "home": {
                    "team": {"name": tie["home_name"],
                             "logo": tie["home_team"].get("logo", ""),
                             "abbr": tie["home_team"].get("abbreviation", "")},
                    "score": str(hg) if done else "", "aggregate": "", "winner": hw,
                },
                "away": {
                    "team": {"name": tie["away_name"],
                             "logo": tie["away_team"].get("logo", ""),
                             "abbr": tie["away_team"].get("abbreviation", "")},
                    "score": str(ag) if done else "", "aggregate": "", "winner": aw,
                },
            })
        if matchups:
            result.append({"name": rn, "matchups": matchups})
    return result or None


def buscar_chaveamento(slug: str) -> list | None:
    """Retorna lista de rodadas [{name, matchups}] para chaveamento mata-mata."""
    ano = datetime.now().year

    # 1. Tenta bracket endpoint da ESPN (temporada atual e anterior, v2 e v1)
    for season in (ano, ano - 1, None):
        for base in (ESPN_V2, ESPN_V1):
            params = {"season": season} if season is not None else None
            data = _espn_get(f"{base}/{slug}/bracket", params)
            if not data:
                continue
            rounds_raw = (
                (data.get("bracket") or {}).get("rounds")
                or data.get("rounds") or []
            )
            if rounds_raw:
                parsed = _parsear_bracket_rounds(rounds_raw)
                # Só usa bracket API se retornou chaveamento útil (≥2 rodadas)
                if parsed and len(parsed) >= 2:
                    return parsed

    # 2. Fallback: scoreboard de período amplo (cobrindo toda fase mata-mata)
    return _bracket_via_scoreboard_wide(slug)


# Layout constants for bracket rendering
_BK_CARD_W = 190   # card width
_BK_CARD_H = 58    # card height (2 × 26px team row + 6px divider)
_BK_CON_W  = 44    # horizontal connector width between rounds
_BK_PAD    = 26    # outer padding
_BK_TITLE  = 52    # title area height
_BK_SLOT_H = 74    # height per slot (card + spacing)


async def gerar_chaveamento_png(rounds: list, nome_liga: str) -> str:
    """Gera imagem de chaveamento (bracket tree) com SVG connector lines."""
    if not rounds:
        return None

    all_urls = list({
        m.get(side, {}).get("team", {}).get("logo", "")
        for rd in rounds for m in rd.get("matchups", [])
        for side in ("home", "away")
        if m.get(side, {}).get("team", {}).get("logo")
    })
    logos = await _baixar_logos_paralelo(all_urls) if all_urls else {}

    n_rounds = len(rounds)
    # n_first = maior número de partidas em qualquer rodada (normalmente a primeira)
    n_first  = max((len(rd["matchups"]) for rd in rounds), default=1)
    bracket_h = n_first * _BK_SLOT_H
    bracket_w = n_rounds * (_BK_CARD_W + _BK_CON_W) - _BK_CON_W
    total_w   = _BK_PAD * 2 + bracket_w
    total_h   = _BK_PAD * 2 + _BK_TITLE + bracket_h

    def slot_cy(r_idx: int, m_idx: int) -> float:
        n = len(rounds[r_idx]["matchups"])
        slots_per = n_first / max(n, 1)
        return _BK_PAD + _BK_TITLE + (m_idx + 0.5) * slots_per * _BK_SLOT_H

    def card_x(r_idx: int) -> float:
        return _BK_PAD + r_idx * (_BK_CARD_W + _BK_CON_W)

    # SVG connector lines
    svg_parts = []
    for r_idx in range(n_rounds - 1):
        n_this = len(rounds[r_idx]["matchups"])
        n_next = len(rounds[r_idx + 1]["matchups"])
        # Só desenha conectores se o próximo round tem metade das partidas
        if n_next >= n_this:
            continue
        for m_idx in range(0, n_this - 1, 2):
            next_m = m_idx // 2
            if next_m >= n_next:
                continue
            cy1  = slot_cy(r_idx, m_idx)
            cy2  = slot_cy(r_idx, m_idx + 1)
            cyn  = slot_cy(r_idx + 1, next_m)
            x_r  = card_x(r_idx) + _BK_CARD_W
            x_mid = x_r + _BK_CON_W / 2
            x_n  = card_x(r_idx + 1)
            c = "#3b82f6"
            svg_parts += [
                f'<line x1="{x_r:.1f}"   y1="{cy1:.1f}" x2="{x_mid:.1f}" y2="{cy1:.1f}" stroke="{c}" stroke-width="1.5"/>',
                f'<line x1="{x_r:.1f}"   y1="{cy2:.1f}" x2="{x_mid:.1f}" y2="{cy2:.1f}" stroke="{c}" stroke-width="1.5"/>',
                f'<line x1="{x_mid:.1f}" y1="{cy1:.1f}" x2="{x_mid:.1f}" y2="{cy2:.1f}" stroke="{c}" stroke-width="1.5"/>',
                f'<line x1="{x_mid:.1f}" y1="{cyn:.1f}" x2="{x_n:.1f}"   y2="{cyn:.1f}" stroke="{c}" stroke-width="1.5"/>',
            ]

    # Match cards
    cards_html = ""
    for r_idx, rd in enumerate(rounds):
        for m_idx, m in enumerate(rd["matchups"]):
            cx = card_x(r_idx)
            cy = slot_cy(r_idx, m_idx) - _BK_CARD_H / 2

            def _team_row(side_key, is_winner):
                side = m.get(side_key, {})
                team = side.get("team", {})
                nome = team.get("name", "TBD")[:22]
                b64  = logos.get(team.get("logo", ""), "")
                img  = (f'<img src="{b64}" width="18" height="18" style="object-fit:contain;flex-shrink:0;">'
                        if b64 else f'<span class="sig">{team.get("abbr", nome[:3]).upper()[:3]}</span>')
                agg   = side.get("aggregate", "")
                score = side.get("score", "")
                disp  = agg if agg else score
                cls   = ' class="tr win"' if is_winner else ' class="tr"'
                return f'<div{cls}>{img}<span class="tn">{nome}</span><span class="ts">{disp}</span></div>'

            home_win = m.get("home", {}).get("winner", False)
            away_win = m.get("away", {}).get("winner", False)
            cards_html += (
                f'<div class="mc" style="left:{cx:.0f}px;top:{cy:.0f}px">'
                f'{_team_row("home", home_win)}'
                f'<div class="sep"></div>'
                f'{_team_row("away", away_win)}'
                f'</div>'
            )

    labels_html = "".join(
        f'<div class="rl" style="left:{card_x(r):.0f}px;width:{_BK_CARD_W}px">'
        f'{rounds[r]["name"].upper()}</div>'
        for r in range(n_rounds)
    )
    svg = (
        f'<svg style="position:absolute;top:0;left:0;pointer-events:none" '
        f'width="{total_w}" height="{total_h}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(svg_parts) + '</svg>'
    )

    css = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#111827;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;
     width:{total_w}px;height:{total_h}px;position:relative;overflow:hidden}}
.titulo{{position:absolute;top:{_BK_PAD}px;left:{_BK_PAD}px;
         font-size:19px;font-weight:700;color:#fff}}
.rl{{position:absolute;top:{_BK_PAD + _BK_TITLE - 20}px;text-align:center;
     font-size:9px;font-weight:700;color:#6b7280;letter-spacing:1.2px}}
.mc{{position:absolute;width:{_BK_CARD_W}px;background:#1e2f5e;
     border-radius:8px;overflow:hidden;border:1px solid #243b73;z-index:1}}
.tr{{display:flex;align-items:center;gap:6px;padding:4px 8px;height:26px}}
.tr.win{{background:#1a3a6e}}
.sep{{height:1px;background:#243b73}}
.tn{{flex:1;font-size:11px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.ts{{font-size:12px;font-weight:700;color:#f59e0b;min-width:18px;text-align:right}}
.tr:not(.win) .ts{{color:#6b7280}}
.sig{{background:#0f1f4a;color:#ccc;border-radius:3px;font-size:8px;font-weight:700;
      flex-shrink:0;width:18px;height:18px;display:flex;align-items:center;justify-content:center}}
"""
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{css}</style></head>'
        f'<body>'
        f'<div class="titulo">⭐ {nome_liga} — Chaveamento {datetime.now().year}</div>'
        f'{labels_html}{svg}{cards_html}'
        f'</body></html>'
    )
    return await _html_para_png(html, "chaveamento_temp.png", width=total_w)


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

        liga_card  = (j.get("meta") or {}).get("liga", "")
        liga_badge = (f'<span class="liga-badge">{liga_card}</span>' if liga_card and not nome_liga else "")
        cards += (
            f'<div class="card{" destaque" if destaque else ""}">'
            f'<div class="lado home"><span class="tnome">{nome_casa}</span>'
            f'{_img_tag(b64_casa, nome_casa, 26)}</div>'
            f'<div class="centro"><span class="hora">{data_hora}</span>{liga_badge}</div>'
            f'<div class="lado away">{_img_tag(b64_fora, nome_fora, 26)}'
            f'<span class="tnome">{nome_fora}</span></div>'
            f'</div>'
        )

    sub_html = f'<div class="sub">{nome_liga}</div>' if nome_liga else ""
    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#16213e;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;width:560px;padding:20px}
.titulo{font-size:18px;font-weight:700;color:#fff;margin-bottom:4px}
.sub{font-size:12px;color:#8892a4;margin-bottom:14px}
.card{display:flex;align-items:center;background:#1a2a5e;margin-bottom:7px;
      padding:11px 14px;border-radius:9px;gap:6px}
.card.destaque{border-left:3px solid #3b82f6;background:#1e3270}
.lado{display:flex;align-items:center;gap:8px;flex:1;min-width:0}
.home{justify-content:flex-end;text-align:right}
.away{justify-content:flex-start;text-align:left}
.tnome{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}
.centro{width:110px;text-align:center;flex-shrink:0}
.hora{color:#93c5fd;font-size:14px;font-weight:700;display:block}
.liga-badge{display:inline-block;margin-top:3px;background:#0f3460;color:#93c5fd;
            padding:1px 6px;border-radius:4px;font-size:9px;font-weight:600;white-space:nowrap}
.sigla{background:#0f3460;color:#ccc;padding:2px 5px;border-radius:4px;font-size:10px;font-weight:700}
"""
    html = (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8"><style>{css}</style></head><body>'
        f'<div class="titulo">📅 Próximos jogos — {nome_time}</div>'
        f'{sub_html}'
        f'{cards}</body></html>'
    )
    return await _html_para_png(html, "proximos_temp.png", width=600)


# ==========================================
# RESUMO DIÁRIO — imagem consolidada de todas as ligas
# ==========================================

async def gerar_resumo_diario_png(jogos_por_liga: dict, data_display: str = None) -> str:
    """
    jogos_por_liga: {"brasileirao": [...], "premierleague": [...], ...}
    Gera uma única imagem com todos os jogos do dia agrupados por liga.
    data_display: data formatada para exibição (ex: "28/05/2026"); None = hoje.
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

    if data_display:
        data_hoje = data_display
        titulo_img = f"📅 Jogos de {data_display}"
    else:
        data_hoje = datetime.now(tz=BRT).strftime("%A, %d de %B de %Y").capitalize()
        titulo_img = "⚽ Jogos do Dia"

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
        f'  <div class="titulo">{titulo_img}</div>'
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
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<script src="https://cdn.jsdelivr.net/npm/mpegts.js@latest/dist/mpegts.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif}
/* ── vídeo ── */
.vwrap{background:#000;width:100%;line-height:0}
video{width:100%;max-height:100vh;display:block;outline:none}
.vstatus{font-size:12px;color:#8892a4;text-align:center;padding:6px;background:#0d1117}
/* ── score header ── */
.dheader{padding:14px 16px 12px;background:#16213e;border-bottom:1px solid #21262d}
.comp{font-size:11px;color:#8892a4;text-align:center;margin-bottom:10px}
.score-row{display:flex;align-items:center;justify-content:center;gap:8px}
.team{display:flex;flex-direction:column;align-items:center;gap:7px;flex:1;min-width:0}
.team img{width:52px;height:52px;object-fit:contain}
.logo-ph{width:52px;height:52px;background:#0f3460;border-radius:10px}
.tname{font-size:14px;font-weight:700;text-align:center}
.smid{text-align:center;flex-shrink:0;min-width:110px}
.score{font-size:52px;font-weight:800;color:#fff;letter-spacing:4px}
.ht{font-size:11px;color:#8892a4;margin-top:3px}
.badge{display:inline-block;padding:4px 12px;border-radius:12px;font-size:11px;font-weight:700;color:#fff;margin-top:8px}
.venue{font-size:11px;color:#8892a4;margin-top:10px;text-align:center}
/* ── tabs ── */
.tabs{display:flex;border-bottom:1px solid #21262d;background:#0d1117}
.tab{flex:1;padding:11px 4px;text-align:center;font-size:12px;font-weight:700;color:#8892a4;cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s}
.tab.active{color:#93c5fd;border-bottom-color:#3b82f6;background:#161b22}
.tab:hover:not(.active){color:#cbd5e1}
/* ── sections ── */
.sec-hdr{background:#1a2a5e;color:#93c5fd;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;padding:8px 16px;display:flex;justify-content:space-between;align-items:center}
.updated{font-size:9px;color:#4b5563;font-weight:400}
/* ── incidents ── */
.inc-hdr,.inc-row{display:flex;align-items:center;padding:0 12px}
.inc-h{flex:1;display:flex;align-items:center;justify-content:flex-end;gap:5px;text-align:right;padding:7px 6px;font-size:13px}
.inc-a{flex:1;display:flex;align-items:center;gap:5px;text-align:left;padding:7px 6px;font-size:13px}
.inc-m{width:60px;text-align:center;color:#93c5fd;font-size:12px;font-weight:700;flex-shrink:0}
.inc-row{border-bottom:1px solid #0d1117}
.inc-sub .inc-h,.inc-sub .inc-a{font-size:11px;color:#8892a4}
.ic{font-size:16px;flex-shrink:0}
.assist{color:#8892a4;font-size:11px}
.sub-in{color:#10b981}.sub-out{color:#ef4444}
.psep{background:#0f1c33;color:#6b7a99;text-align:center;font-size:11px;padding:6px;letter-spacing:1px}
/* ── stats ── */
.stats-body{padding:8px 16px 14px;max-width:700px;margin:0 auto}
.stat-row{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #0f1c33}
.sv{width:48px;font-size:13px;font-weight:700;flex-shrink:0}
.sh{text-align:right;color:#60a5fa}.sa{text-align:left;color:#fbbf24}
.sbar{flex:1;height:6px;background:#0f3460;border-radius:3px;overflow:hidden}
.sb-h{background:#3b82f6;height:100%;float:right}.sb-a{background:#f59e0b;height:100%;float:left}
.slabel{width:130px;text-align:center;font-size:11px;color:#8892a4;flex-shrink:0}
/* ── lineups ── */
.lu-wrap{display:flex;max-width:700px;margin:0 auto}
.lu-col{flex:1;padding:10px 14px;min-width:0}
.lu-col-r{text-align:right}
.lu-div{width:1px;background:#21262d;flex-shrink:0}
.lu-head{font-size:12px;color:#93c5fd;font-weight:700;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid #21262d}
.pl{display:flex;align-items:center;gap:6px;padding:5px 0;border-bottom:1px solid #0d1117;font-size:12px}
.pl-r{justify-content:flex-end}
.pj{width:22px;height:22px;background:#0f3460;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;color:#93c5fd;flex-shrink:0;text-align:center}
.pp{font-size:9px;color:#8892a4;width:14px;flex-shrink:0;text-align:center}
.pn{font-size:12px;font-weight:500}
.no-data{padding:24px;text-align:center;color:#4b5563;font-size:13px}
</style>
</head>
<body>
<div class="vwrap">
  <video id="v" controls autoplay playsinline></video>
</div>
<div class="vstatus" id="vstatus">Conectando...</div>
<div id="dheader"><div class="no-data" style="padding:12px">Carregando dados da partida...</div></div>
<div class="tabs" id="tabs" style="display:none">
  <div class="tab active" data-tab="inc" onclick="showTab('inc')">⏱ Lance a Lance</div>
  <div class="tab" data-tab="stats" onclick="showTab('stats')">📊 Estatísticas</div>
  <div class="tab" data-tab="lu" onclick="showTab('lu')">👥 Escalações</div>
</div>
<div id="tab-inc"></div>
<div id="tab-stats" style="display:none"></div>
<div id="tab-lu"    style="display:none"></div>
<script>
const TOKEN    = location.pathname.split("/").pop();
const PROXY    = "__SERVER_URL__/proxy/" + TOKEN;
const API      = "__SERVER_URL__/api/live/" + TOKEN;
const isHLS    = "__STREAM_URL__".includes(".m3u8");
const v        = document.getElementById("v");
const vstatus  = document.getElementById("vstatus");
let   curTab   = "inc";

// ── Video ──────────────────────────────────────────────────────────────────
function setVStatus(m) { vstatus.textContent = m; }
if (isHLS && Hls.isSupported()) {
    const hls = new Hls({enableWorker:true,lowLatencyMode:true});
    hls.loadSource(PROXY); hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED, () => { v.play(); setVStatus("🔴 Ao vivo"); });
    hls.on(Hls.Events.ERROR, (_,d) => { if(d.fatal) setVStatus("❌ "+d.details); });
} else if (mpegts.isSupported()) {
    const p = mpegts.createPlayer({type:"mpegts",isLive:true,url:PROXY},
        {enableWorker:true,lazyLoadMaxDuration:180,seekType:"range"});
    p.attachMediaElement(v); p.load(); p.play();
    p.on(mpegts.Events.MEDIA_INFO, () => setVStatus("🔴 Ao vivo"));
    p.on(mpegts.Events.ERROR, (t) => setVStatus("❌ "+t));
} else {
    setVStatus("❌ Navegador sem suporte. Use Chrome.");
}

// ── Tabs ────────────────────────────────────────────────────────────────────
function showTab(id) {
    curTab = id;
    ["inc","stats","lu"].forEach(t => {
        document.getElementById("tab-"+t).style.display = (t===id ? "" : "none");
    });
    document.querySelectorAll(".tab").forEach(el => {
        el.classList.toggle("active", el.dataset.tab === id);
    });
}

// ── Utils ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function logo(url, sz) {
    sz = sz||42;
    if (url) return '<img src="'+url+'" width="'+sz+'" height="'+sz+'" style="object-fit:contain" onerror="this.remove()">';
    return '<div class="logo-ph" style="width:'+sz+'px;height:'+sz+'px"></div>';
}
function now() {
    return new Date().toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit"});
}
function sv(side, keys) {
    for (var i=0;i<keys.length;i++) {
        var v = (side||{})[keys[i]];
        if (typeof v==="object"&&v!==null) v=v.actual??v.value??v.pct??0;
        if (v!==null&&v!==undefined) { var n=parseFloat(v); if(!isNaN(n)) return n; }
    }
    return 0;
}

// ── Header ──────────────────────────────────────────────────────────────────
function renderHeader(ev, venue, logos) {
    if (!ev) return;
    var st=ev.status||"", mn=ev.current_minute;
    var bText="A INICIAR", bColor="#8892a4";
    if (st==="finished") { bText=(ev.period||"FT").toUpperCase(); bColor="#10b981"; }
    else if (st!=="notstarted") { bText=mn?mn+"'":"AO VIVO"; bColor="#ef4444"; }
    var hasScore=st!=="notstarted";
    var scoreStr=hasScore?(ev.home_score??"-")+" · "+(ev.away_score??"-"):"vs";
    var htHtml=(hasScore&&ev.home_score_ht!=null)
        ? '<div class="ht">Intervalo '+ev.home_score_ht+"-"+ev.away_score_ht+"</div>" : "";
    var lg=logos||{};
    document.getElementById("dheader").innerHTML =
        '<div class="comp">'+esc(ev.competition_name||"")+'</div>'+
        '<div class="score-row">'+
          '<div class="team">'+logo(lg[ev.home_team||""])+'<div class="tname">'+esc(ev.home_team||"?")+'</div></div>'+
          '<div class="smid">'+
            '<div class="score">'+scoreStr+'</div>'+htHtml+
            '<span class="badge" style="background:'+bColor+'">'+bText+'</span>'+
          '</div>'+
          '<div class="team">'+logo(lg[ev.away_team||""])+'<div class="tname">'+esc(ev.away_team||"?")+'</div></div>'+
        '</div>'+
        (venue&&venue.name?'<div class="venue">📍 '+esc(venue.name)+(venue.city?" "+esc(venue.city):"")+'</div>':"");
    document.getElementById("tabs").style.display="";
}

// ── Incidents ───────────────────────────────────────────────────────────────
function renderIncidents(incs, ev) {
    var hName=esc(ev&&ev.home_team||"Casa"), aName=esc(ev&&ev.away_team||"Fora");
    if (!incs||!incs.length) {
        document.getElementById("tab-inc").innerHTML='<div class="no-data">Sem incidentes registrados.</div>';
        return;
    }
    var sorted=incs.slice().sort(function(a,b){return (a.minute||0)-(b.minute||0)||(a.added_time||0)-(b.added_time||0);});
    var rows='<div class="inc-hdr"><div class="inc-h" style="font-size:10px;color:#8892a4">'+hName+'</div><div class="inc-m"></div><div class="inc-a" style="font-size:10px;color:#8892a4">'+aName+'</div></div>';
    function minStr(i) { return i.added_time?i.minute+"+"+i.added_time+"'":i.minute+"'"; }
    for (var i=0;i<sorted.length;i++) {
        var inc=sorted[i], t=inc.type||"", mn=minStr(inc), home=inc.is_home!==false;
        if (t==="period") {
            if (/(HT|HALF)/i.test(inc.text||""))
                rows+='<div class="psep">── INTERVALO '+(inc.home_score||"")+"-"+(inc.away_score||"")+" ──</div>";
            continue;
        }
        if (t==="injuryTime") {
            rows+='<div class="inc-row inc-sub"><div class="inc-h"></div><div class="inc-m">+'+(inc.length||"?")+"'</div>"+'<div class="inc-a" style="font-size:9px;color:#6b7a99">acréscimos</div></div>';
            continue;
        }
        if (t==="goal") {
            var own=inc.goal_type==="own";
            var txt='<b>'+esc(inc.player||"")+'</b>'+(inc.assist?' <span class="assist">('+esc(inc.assist)+')</span>':"")+(own?' <span style="color:#ef4444;font-size:9px">CG</span>':"");
            if (home) rows+='<div class="inc-row"><div class="inc-h"><span class="ic">⚽</span>'+txt+'</div><div class="inc-m">'+mn+'</div><div class="inc-a"></div></div>';
            else      rows+='<div class="inc-row"><div class="inc-h"></div><div class="inc-m">'+mn+'</div><div class="inc-a">'+txt+'<span class="ic">⚽</span></div></div>';
        } else if (t==="card") {
            var icon=inc.card_type==="yellow"?"🟡":"🟥";
            if (home) rows+='<div class="inc-row inc-sub"><div class="inc-h"><span class="ic">'+icon+'</span>'+esc(inc.player||"")+'</div><div class="inc-m">'+mn+'</div><div class="inc-a"></div></div>';
            else      rows+='<div class="inc-row inc-sub"><div class="inc-h"></div><div class="inc-m">'+mn+'</div><div class="inc-a">'+esc(inc.player||"")+'<span class="ic">'+icon+'</span></div></div>';
        } else if (t==="substitution") {
            var stxt='<span class="sub-in">↑ '+esc(inc.player_in||"")+'</span> <span class="sub-out">↓ '+esc(inc.player_out||"")+'</span>';
            if (home) rows+='<div class="inc-row inc-sub"><div class="inc-h">'+stxt+'</div><div class="inc-m">'+mn+'</div><div class="inc-a"></div></div>';
            else      rows+='<div class="inc-row inc-sub"><div class="inc-h"></div><div class="inc-m">'+mn+'</div><div class="inc-a">'+stxt+'</div></div>';
        }
    }
    document.getElementById("tab-inc").innerHTML=
        '<div class="sec-hdr">Lance a Lance<span class="updated">att. '+now()+'</span></div>'+rows;
}

// ── Stats ───────────────────────────────────────────────────────────────────
function statRow(label, hv, av, pct, dec) {
    var total=hv+av;
    var hBar=pct?Math.round(hv):(total>0?Math.round(hv/total*100):50);
    var aBar=100-hBar;
    function fmt(n) { return dec?n.toFixed(dec):Math.round(n); }
    var suf=pct?"%":"";
    return '<div class="stat-row">'+
        '<div class="sv sh">'+fmt(hv)+suf+'</div>'+
        '<div class="sbar"><div class="sb-h" style="width:'+hBar+'%"></div></div>'+
        '<div class="slabel">'+label+'</div>'+
        '<div class="sbar"><div class="sb-a" style="width:'+aBar+'%"></div></div>'+
        '<div class="sv sa">'+fmt(av)+suf+'</div>'+
    '</div>';
}
function renderStats(stats) {
    var sh=stats.home||{}, sa=stats.away||{};
    if (!Object.keys(sh).length&&!Object.keys(sa).length) {
        document.getElementById("tab-stats").innerHTML='<div class="no-data">Estatísticas não disponíveis.</div>';
        return;
    }
    var rows="";
    rows+=statRow("Posse",          sv(sh,["ball_possession"]),              sv(sa,["ball_possession"]),              true,  0);
    rows+=statRow("xG",             sv(sh,["xg","expected_goals"]),          sv(sa,["xg","expected_goals"]),          false, 2);
    rows+=statRow("Finalizações",   sv(sh,["total_shots"]),                  sv(sa,["total_shots"]),                  false, 0);
    rows+=statRow("No Alvo",        sv(sh,["shots_on_target"]),              sv(sa,["shots_on_target"]),              false, 0);
    rows+=statRow("Dentro da Área", sv(sh,["shots_inside_box"]),             sv(sa,["shots_inside_box"]),             false, 0);
    rows+=statRow("Prec. Passe",    sv(sh,["pass_accuracy_pct"]),            sv(sa,["pass_accuracy_pct"]),            true,  0);
    rows+=statRow("Escanteios",     sv(sh,["corner_kicks"]),                 sv(sa,["corner_kicks"]),                 false, 0);
    rows+=statRow("Faltas",         sv(sh,["fouls"]),                        sv(sa,["fouls"]),                        false, 0);
    rows+=statRow("Cart. Amarelo",  sv(sh,["yellow_cards"]),                 sv(sa,["yellow_cards"]),                 false, 0);
    rows+=statRow("Defesas",        sv(sh,["goalkeeper_saves","total_saves"]),sv(sa,["goalkeeper_saves","total_saves"]),false, 0);
    document.getElementById("tab-stats").innerHTML=
        '<div class="sec-hdr">Estatísticas<span class="updated">att. '+now()+'</span></div>'+
        '<div class="stats-body">'+rows+'</div>';
}

// ── Lineups ──────────────────────────────────────────────────────────────────
function renderLineups(lineups, ev) {
    var lh=lineups.home||{}, la=lineups.away||{};
    var hxi=lh.players||[], axi=la.players||[];
    if (!hxi.length&&!axi.length) {
        document.getElementById("tab-lu").innerHTML='<div class="no-data">Escalação não disponível.</div>';
        return;
    }
    var hHead=esc((ev&&ev.home_team)||"Casa")+(lh.formation?" ("+lh.formation+")":"");
    var aHead=esc((ev&&ev.away_team)||"Fora")+(la.formation?" ("+la.formation+")":"");
    function plH(p) { return '<div class="pl"><div class="pj">'+esc(p.jersey_number||"")+'</div><div class="pp">'+esc(p.position||"")+'</div><div class="pn">'+esc(p.name||"")+'</div></div>'; }
    function plA(p) { return '<div class="pl pl-r"><div class="pn">'+esc(p.name||"")+'</div><div class="pp">'+esc(p.position||"")+'</div><div class="pj">'+esc(p.jersey_number||"")+'</div></div>'; }
    document.getElementById("tab-lu").innerHTML=
        '<div class="sec-hdr">Escalações</div>'+
        '<div class="lu-wrap">'+
          '<div class="lu-col"><div class="lu-head">'+hHead+'</div>'+hxi.map(plH).join("")+'</div>'+
          '<div class="lu-div"></div>'+
          '<div class="lu-col lu-col-r"><div class="lu-head">'+aHead+'</div>'+axi.map(plA).join("")+'</div>'+
        '</div>';
}

// ── Fetch & refresh ──────────────────────────────────────────────────────────
var _fetchErrors = 0;
async function fetchLive() {
    try {
        var r = await fetch(API+"?t="+Date.now());
        if (!r.ok) {
            _fetchErrors++;
            if (_fetchErrors === 1) {
                var err = {};
                try { err = await r.json(); } catch(_) {}
                document.getElementById("dheader").innerHTML =
                    '<div class="no-data" style="padding:16px;color:#ef4444">⚠️ ' +
                    (err.error || "Dados indisponíveis para esta partida") + '</div>';
            }
            return;
        }
        _fetchErrors = 0;
        var d = await r.json();
        renderHeader(d.evento, d.venue, d.logos);
        renderIncidents((d.incidents||{}).incidents||[], d.evento);
        renderStats((d.stats||{}).stats||{});
        renderLineups((d.lineups||{}).lineups||{}, d.evento);
    } catch(e) { console.error("[Live]", e); }
}
fetchLive();
setInterval(fetchLive, 30000);
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
    html = (
        _PLAYER_HTML
        .replace("__TITLE__",      sessao["title"])
        .replace("__STREAM_URL__", sessao["stream_url"])
        .replace("__SERVER_URL__", SERVER_URL)
    )
    return aiohttp_web.Response(text=html, content_type="text/html", charset="utf-8")


async def _web_partida_html_handler(request: aiohttp_web.Request) -> aiohttp_web.Response:
    token = request.match_info.get("token", "")
    html  = _partida_pages.get(token)
    if not html:
        return aiohttp_web.Response(
            text="<h2>⛔ Página não encontrada ou expirada.</h2>",
            content_type="text/html", status=404,
        )
    return aiohttp_web.Response(text=html, content_type="text/html", charset="utf-8")


async def _web_live_api_handler(request: aiohttp_web.Request) -> aiohttp_web.Response:
    token  = request.match_info.get("token", "")
    sessao = _player_sessions.get(token)
    if not sessao:
        return aiohttp_web.json_response({"error": "sessão não encontrada"}, status=404, headers=_CORS_HEADERS)
    bz_id = sessao.get("bz_event_id")
    if not bz_id:
        # Tenta encontrar o evento agora (pode não ter existido na criação da sessão)
        nc = sessao.get("nome_casa", "")
        nf = sessao.get("nome_fora", "")
        if nc and nf:
            loop = asyncio.get_event_loop()
            bz_id = await loop.run_in_executor(None, _find_bz_event_id, nc, nf)
            if bz_id:
                sessao["bz_event_id"] = bz_id  # cache para próximas chamadas
    if not bz_id:
        return aiohttp_web.json_response({"error": "sem dados Bzzoiro para esta partida"}, status=404, headers=_CORS_HEADERS)
    loop  = asyncio.get_event_loop()
    dados = await loop.run_in_executor(None, buscar_detalhes_partida, bz_id)
    if not dados:
        return aiohttp_web.json_response({"error": "dados indisponíveis"}, status=503, headers=_CORS_HEADERS)
    logo_map = _buscar_logos_brasileiros()
    ev = dados.get("evento") or {}
    dados["logos"] = {
        ev.get("home_team", ""): logo_map.get(ev.get("home_team", ""), ""),
        ev.get("away_team", ""): logo_map.get(ev.get("away_team", ""), ""),
    }
    return aiohttp_web.json_response(dados, headers=_CORS_HEADERS)


def _publicar_partida_web(html: str) -> str:
    """Armazena HTML de partida e retorna URL de acesso."""
    token = secrets.token_urlsafe(12)
    _partida_pages[token] = html
    return f"{SERVER_URL}/partida/{token}"


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
    app.router.add_get("/player/{token}",   _web_player_handler)
    app.router.add_get("/partida/{token}", _web_partida_html_handler)
    app.router.add_get("/api/live/{token}", _web_live_api_handler)
    app.router.add_get("/proxy/{token}",   _proxy_handler)
    app.router.add_get("/proxy",           _proxy_handler)   # segmentos reescritos do M3U8
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
        self.add_view(MenuPrincipalView(persistent=True))
        await _iniciar_servidor_web()
        checar_jogos_ao_vivo.start()
        atualizar_players.start()
        verificar_noticias_times.start()
        verificar_jogos_times.start()
        monitorar_eventos_times.start()
        lembrete_pre_jogo.start()
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
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands sincronizados: {len(synced)}")
    except Exception as e:
        print(f"Erro ao sincronizar slash commands: {e}")
    if BZZOIRO_TOKEN:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _bz_build_team_map)
    await _publicar_menu_canal()


# ==========================================
# SEGUIR TIME — persistência
# ==========================================

_SEGUINDO_PATH = os.path.join(os.path.dirname(__file__), "seguindo.json")

# Convex: persistência durável dos seguidores (o disco do Railway é efêmero).
# Se CONVEX_URL não estiver configurado, cai automaticamente no arquivo local.
_CONVEX_URL    = os.getenv("CONVEX_URL", "").strip()
_BOT_SECRET    = os.getenv("BOT_SHARED_SECRET", "").strip()
_convex_client = None
if _CONVEX_URL:
    try:
        from convex import ConvexClient
        _convex_client = ConvexClient(_CONVEX_URL)
        print(f"[Convex] Cliente conectado em {_CONVEX_URL}")
    except Exception as e:
        print(f"[Convex] Falha ao inicializar cliente ({e}); usando arquivo local.")
        _convex_client = None
else:
    print("[Convex] CONVEX_URL não definido; seguidores serão salvos em arquivo local.")


def _convex_args(extra: dict | None = None) -> dict:
    args = dict(extra or {})
    if _BOT_SECRET:
        args["secret"] = _BOT_SECRET
    return args


_PREFS_PADRAO = {"noticias": True, "jogos": True, "lembrete": True}
LEMBRETE_MIN_ANTES = 25   # minutos antes do apito
LEMBRETE_MAX_ANTES = 55


def _prefs_de(dados: dict) -> dict:
    p = dados.get("prefs") or {}
    return {
        "noticias": bool(p.get("noticias", True)),
        "jogos":    bool(p.get("jogos", True)),
        "lembrete": bool(p.get("lembrete", True)),
    }


def _uids_com_pref(uids: set[int], chave: str) -> set[int]:
    out: set[int] = set()
    for uid in uids:
        if _prefs_de(_SEGUINDO.get(str(uid), {})).get(chave, True):
            out.add(uid)
    return out


def _carregar_seguindo() -> dict:
    if _convex_client:
        try:
            data = _convex_client.query("seguidores:getAll", _convex_args())
            out: dict = {}
            for uid, entry in (data or {}).items():
                row = {
                    "times": list(entry.get("times", [])),
                    "noticias_vistas": dict(entry.get("noticiasVistas", {})),
                }
                if entry.get("prefs"):
                    row["prefs"] = dict(entry["prefs"])
                if entry.get("lembretesEnviados"):
                    row["lembretes_enviados"] = list(entry["lembretesEnviados"])
                out[uid] = row
            print(f"[Convex] {len(out)} seguidor(es) carregado(s).")
            return out
        except Exception as e:
            print(f"[Convex] Erro ao carregar ({e}); tentando arquivo local.")
    try:
        with open(_SEGUINDO_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _salvar_seguindo(data: dict) -> None:
    if _convex_client:
        try:
            payload = {}
            for uid, entry in data.items():
                row = {
                    "times": list(entry.get("times", [])),
                    "noticiasVistas": dict(entry.get("noticias_vistas", {})),
                }
                prefs = entry.get("prefs")
                if prefs:
                    row["prefs"] = {
                        "noticias": bool(prefs.get("noticias", True)),
                        "jogos":    bool(prefs.get("jogos", True)),
                        "lembrete": bool(prefs.get("lembrete", True)),
                    }
                lem = entry.get("lembretes_enviados")
                if lem:
                    row["lembretesEnviados"] = list(lem)[-150:]
                payload[uid] = row
            _convex_client.mutation("seguidores:replaceAll", _convex_args({"dados": payload}))
            return
        except Exception as e:
            print(f"[Convex] Erro ao salvar ({e}); gravando arquivo local.")
    with open(_SEGUINDO_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_SEGUINDO: dict = _carregar_seguindo()
# event_id → {home, away, slug, user_ids: set, eventos: set, encerrado: bool, notif_inicio: bool}
_JOGOS_TIMES: dict = {}
# event_ids de jogos já finalizados (evita reprocessar/reenviar quando o jogo
# encerrado continua aparecendo no placar do dia).
_JOGOS_ENCERRADOS: set = set()


def _time_match(query: str, nome: str) -> bool:
    """True se query corresponde ao nome do time (sem acentos, case-insensitive)."""
    q = _strip_accents(query.lower())
    n = _strip_accents(nome.lower())
    return q in n or n in q


def _buscar_jogos_ao_vivo_todos() -> list[dict]:
    """Varre todas as ligas ESPN + Copa do Brasil e retorna todos os jogos de hoje."""
    resultado: list[dict] = []
    vistos: set[str] = set()
    for slug in set(v for v in LIGAS.values() if v):
        try:
            for j in buscar_jogos_do_dia(slug):
                eid = str(j["fixture"]["id"])
                if eid not in vistos:
                    vistos.add(eid)
                    resultado.append({
                        "event_id": eid,
                        "home":  j["teams"]["home"]["name"],
                        "away":  j["teams"]["away"]["name"],
                        "slug":  slug,
                        "state": j["fixture"]["status"]["short"],
                        "date":  j["fixture"]["date"],
                    })
        except Exception:
            pass
    try:
        for j in buscar_jogos_copa_hoje():
            eid = str(j["fixture"]["id"])
            if eid not in vistos:
                vistos.add(eid)
                resultado.append({
                    "event_id": eid,
                    "home":  j["teams"]["home"]["name"],
                    "away":  j["teams"]["away"]["name"],
                    "slug":  None,
                    "state": j["fixture"]["status"]["short"],
                    "date":  j["fixture"]["date"],
                })
    except Exception:
        pass
    return resultado


# Títulos que indicam notícia fora do time principal (feminino, base, outros esportes…)
_NOTICIAS_FORA_PRINCIPAL_RE = re.compile(
    r"(?i)(?:"
    r"feminino|feminina|femininos|femininas|"
    r"women|woman|ladies|wsl\b|"
    r"futebol\s+feminino|equipe\s+feminina|sele[cç][aã]o\s+feminina|"
    r"liga\s+feminina|brasileir[aã]o\s+feminino|libertadores\s+feminina|"
    r"copa\s+do\s+brasil\s+feminina|mundial\s+feminino|"
    r"sub[\-\s]?20|sub[\-\s]?17|sub[\-\s]?15|sub[\-\s]?23|"
    r"categorias?\s+de\s+base|base\s+sub|juvenil|"
    r"\bu[\-\s]?20\b|\bu[\-\s]?17\b|\bu[\-\s]?23\b|"
    r"\btime\s+b\b|reservas?|"
    r"futsal|v[oô]lei|basquete|handebol|e[\-\s]?sports?"
    r")"
)


def _noticia_equipe_principal(time: str, title: str) -> bool:
    """True se a notícia parece ser do time principal (masculino/profissional), não feminino/base/outros."""
    t = _strip_accents((title or "").lower())
    if _NOTICIAS_FORA_PRINCIPAL_RE.search(t):
        return False
    # Ex.: segue "Flamengo" mas o título fala de "Flamengo feminino"
    tk = _strip_accents(time.lower())
    for sufixo in (
        " feminino", " feminina", " sub-20", " sub-17", " sub-15",
        " base", " futsal", " juvenil", " reservas",
    ):
        if tk + sufixo.replace("-", "") in t.replace("-", "").replace(" ", ""):
            return False
        if tk + sufixo in t:
            return False
    return True


_NOTICIA_ONTEM_RE = re.compile(
    r"(?i)\b(ontem|yesterday|dia anterior|há \d+ dia|ha \d+ dia)\b"
)


def _parse_rss_pub_date(item: _ET.Element) -> datetime | None:
    """Data de publicação do item RSS (Google News usa pubDate RFC 822)."""
    pub = item.findtext("pubDate") or item.findtext("{http://www.w3.org/2005/Atom}published")
    if not pub:
        return None
    try:
        dt = parsedate_to_datetime(pub.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BRT)
    except Exception:
        return None


def _noticia_e_de_hoje(pub_dt: datetime | None, title: str) -> bool:
    """Aceita só notícias publicadas hoje (BRT), nunca do dia anterior."""
    if pub_dt is None:
        return False
    if pub_dt.date() != datetime.now(tz=BRT).date():
        return False
    if _NOTICIA_ONTEM_RE.search(title or ""):
        return False
    return True


async def _buscar_noticias_time(time: str) -> list[dict]:
    """Notícias de hoje (BRT) do time principal via Google News RSS."""
    query = url_quote(
        f'"{time}" futebol when:1d -feminino -feminina -"futebol feminino"'
    )
    url = f"https://news.google.com/rss/search?q={query}&hl=pt-BR&gl=BR&ceid=BR:pt"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return []
                xml_text = await r.text()
        root  = _ET.fromstring(xml_text)
        items = []
        for item in root.findall(".//item")[:40]:
            title  = item.findtext("title") or ""
            link   = item.findtext("link") or ""
            pub_dt = _parse_rss_pub_date(item)
            if not title or not link:
                continue
            if not _noticia_equipe_principal(time, title):
                continue
            if not _noticia_e_de_hoje(pub_dt, title):
                continue
            items.append({"title": title, "link": link, "pub": pub_dt})
            if len(items) >= 5:
                break
        return items
    except Exception as e:
        print(f"[Noticias] {time}: {e}")
        return []


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


async def _buscar_jogos_resumo_paralelo(chaves: list[str] | None = None) -> dict:
    """Busca jogos de várias ligas em paralelo (resumo / !hoje)."""
    chaves = chaves or LIGAS_RESUMO
    loop = asyncio.get_event_loop()

    def _fetch(chave: str):
        try:
            if chave in LIGAS_COPA:
                jogos = buscar_jogos_copa_hoje()
            else:
                jogos = buscar_jogos_do_dia(LIGAS[chave])
            return chave, jogos if jogos else None
        except Exception as e:
            print(f"[Resumo] {chave}: {e}")
            return chave, None

    parts = await asyncio.gather(
        *[loop.run_in_executor(None, _fetch, c) for c in chaves],
        return_exceptions=True,
    )
    out: dict = {}
    for p in parts:
        if isinstance(p, Exception):
            print(f"[Resumo] gather: {p}")
            continue
        chave, jogos = p
        if jogos:
            out[chave] = jogos
    return out


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

    jogos_por_liga = await _buscar_jogos_resumo_paralelo()

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
# SEGUIR TIME — tasks
# ==========================================

@tasks.loop(minutes=30)
async def verificar_noticias_times():
    """A cada 30 min verifica novidades para os times seguidos e envia DM."""
    if not _SEGUINDO:
        return
    for uid_str, dados in list(_SEGUINDO.items()):
        if not _prefs_de(dados).get("noticias", True):
            continue
        times  = dados.get("times", [])
        vistas = dados.get("noticias_vistas", {})
        salvar = False
        for time in times:
            try:
                noticias    = await _buscar_noticias_time(time)
                urls_vistas = set(vistas.get(time, []))
                novas       = [
                    n for n in noticias
                    if n["link"] not in urls_vistas
                    and _noticia_equipe_principal(time, n["title"])
                    and _noticia_e_de_hoje(n.get("pub"), n["title"])
                ]
                if not novas:
                    continue
                user = await bot.fetch_user(int(uid_str))
                for n in novas[:3]:
                    await user.send(f"📰 **{time}**\n**{n['title']}**\n{n['link']}")
                    urls_vistas.add(n["link"])
                vistas[time] = list(urls_vistas)[-100:]
                salvar = True
            except Exception as e:
                print(f"[Noticias] {uid_str}/{time}: {e}")
        if salvar:
            dados["noticias_vistas"] = vistas
    _salvar_seguindo(_SEGUINDO)


@tasks.loop(minutes=10)
async def lembrete_pre_jogo():
    """Avisa ~30 min antes do apito inicial dos jogos de times seguidos."""
    if not _SEGUINDO or not BZZOIRO_TOKEN:
        return
    agora = datetime.now(tz=BRT)
    loop  = asyncio.get_event_loop()
    eventos = await loop.run_in_executor(None, eventos_hoje_bzzoiro_todos)
    salvar  = False
    for uid_str, dados in list(_SEGUINDO.items()):
        if not _prefs_de(dados).get("lembrete", True):
            continue
        times = dados.get("times", [])
        if not times:
            continue
        enviados = set(dados.get("lembretes_enviados", []))
        mudou    = False
        for ev in eventos:
            status = (ev.get("status") or "").lower()
            if status not in ("notstarted", "ns", ""):
                continue
            eid = str(ev.get("id"))
            if eid in enviados:
                continue
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            if not any(_time_match(t, home) or _time_match(t, away) for t in times):
                continue
            try:
                kick = datetime.fromisoformat(
                    (ev.get("event_date") or "").replace("Z", "+00:00")
                ).astimezone(BRT)
            except Exception:
                continue
            delta_min = (kick - agora).total_seconds() / 60
            if not (LEMBRETE_MIN_ANTES <= delta_min <= LEMBRETE_MAX_ANTES):
                continue
            liga = _BZ_LEAGUE_NAMES.get(ev.get("league_id") or 0, "")
            liga_txt = f"\n🏆 {liga}" if liga else ""
            try:
                user = await bot.fetch_user(int(uid_str))
                await user.send(
                    f"⏰ **Lembrete** — jogo em ~{int(delta_min)} min\n"
                    f"**{home} × {away}**{liga_txt}\n"
                    f"🕐 {kick.strftime('%H:%M')} (horário de Brasília)"
                )
            except Exception:
                pass
            enviados.add(eid)
            mudou = True
        if mudou:
            dados["lembretes_enviados"] = list(enviados)[-150:]
            salvar = True
    if salvar:
        _salvar_seguindo(_SEGUINDO)


@tasks.loop(minutes=2)
async def verificar_jogos_times():
    """A cada 2 min detecta jogos ao vivo de times seguidos e avisa o início via DM.
    O monitoramento de gols/cartões/fim fica em monitorar_eventos_times (30s)."""
    if not _SEGUINDO:
        return

    # Mapa: token_normalizado → set[user_id]
    times_map: dict[str, set[int]] = {}
    for uid_str, dados in _SEGUINDO.items():
        if not _prefs_de(dados).get("jogos", True):
            continue
        for t in dados.get("times", []):
            tk = _strip_accents(t.lower())
            times_map.setdefault(tk, set()).add(int(uid_str))

    if not times_map:
        return

    loop = asyncio.get_event_loop()
    jogos = await loop.run_in_executor(None, _buscar_jogos_ao_vivo_bzzoiro)

    for j in jogos:
        eid   = j["event_id"]
        home  = j["home"]
        away  = j["away"]
        state = j["state"]

        interessados: set[int] = set()
        for tk, uids in times_map.items():
            if _time_match(tk, home) or _time_match(tk, away):
                interessados |= uids

        if not interessados:
            continue

        # Jogo já finalizado anteriormente: não ressuscitar nem reenviar nada.
        if eid in _JOGOS_ENCERRADOS:
            continue

        if eid not in _JOGOS_TIMES:
            # Só passa a acompanhar jogos que estão ao vivo; se ao ser visto
            # pela primeira vez já estiver encerrado, ignora (resultado antigo).
            if state in ("FT", "AET", "PEN"):
                continue
            _JOGOS_TIMES[eid] = {
                "home": home, "away": away,
                "bz_id": j["bz_id"],
                "user_ids": interessados,
                "eventos": set(),
                "encerrado": False,
                "notif_inicio": False,
                "primeira_leitura": True,
            }
        else:
            _JOGOS_TIMES[eid]["user_ids"] |= interessados

        jd = _JOGOS_TIMES[eid]

        # Notificação de início
        if state not in ("NS", "FT", "AET", "PEN") and not jd["notif_inicio"]:
            jd["notif_inicio"] = True
            for uid in _uids_com_pref(interessados, "jogos"):
                try:
                    user = await bot.fetch_user(uid)
                    await user.send(
                        f"⚽ **Jogo ao vivo!**\n"
                        f"**{home} × {away}** está em andamento!"
                    )
                except Exception:
                    pass


@tasks.loop(seconds=30)
async def monitorar_eventos_times():
    """A cada 30s consulta placar/eventos dos jogos já detectados e envia gols, cartões e fim de jogo via DM."""
    if not _JOGOS_TIMES:
        return
    loop = asyncio.get_event_loop()
    # Uma única chamada traz status/placar de todos os jogos de hoje.
    eventos_hoje = await loop.run_in_executor(None, _bz_eventos_hoje)
    mapa_ev = {ev.get("id"): ev for ev in eventos_hoje}

    for eid, jd in list(_JOGOS_TIMES.items()):
        if eid in _JOGOS_ENCERRADOS or jd.get("encerrado"):
            _JOGOS_TIMES.pop(eid, None)
            continue
        bz_id = jd.get("bz_id")
        if not bz_id:
            continue
        try:
            ev = mapa_ev.get(bz_id)
            if ev is None:
                ev = await loop.run_in_executor(None, _bzzoiro_get, f"events/{bz_id}/") or {}
            status = ev.get("status", "")
            home_s = ev.get("home_score")
            away_s = ev.get("away_score")
            nome_h = ev.get("home_team") or jd["home"]
            nome_a = ev.get("away_team") or jd["away"]

            inc_data  = await loop.run_in_executor(None, _bzzoiro_get, f"events/{bz_id}/incidents/")
            incidents = (inc_data or {}).get("incidents", [])

            primeira = jd.get("primeira_leitura", False)
            for inc in incidents:
                if inc.get("type") not in ("goal", "card"):
                    continue
                key = _bz_incident_key(inc)
                if key in jd["eventos"]:
                    continue
                jd["eventos"].add(key)
                # Na primeira leitura (jogo já estava em andamento ao ser
                # detectado), apenas marca como visto sem despejar o histórico.
                if primeira:
                    continue
                msg = _bz_incident_para_msg(inc, nome_h, nome_a)
                if not msg:
                    continue
                for uid in _uids_com_pref(jd["user_ids"], "jogos"):
                    try:
                        user = await bot.fetch_user(uid)
                        await user.send(msg)
                    except Exception:
                        pass

            if primeira:
                jd["primeira_leitura"] = False

            if status == "finished" and not jd.get("encerrado"):
                if not primeira:
                    pen     = ev.get("penalty_shootout") or {}
                    pen_txt = (f"  (pênaltis {pen.get('home')} × {pen.get('away')})"
                               if pen.get("home") is not None else "")
                    msg_fim = f"🏁 **Fim de jogo!**\n**{nome_h} {home_s} × {away_s} {nome_a}**{pen_txt}"
                    for uid in _uids_com_pref(jd["user_ids"], "jogos"):
                        try:
                            user = await bot.fetch_user(uid)
                            await user.send(msg_fim)
                        except Exception:
                            pass
                jd["encerrado"] = True
                _JOGOS_ENCERRADOS.add(eid)
                _JOGOS_TIMES.pop(eid, None)

        except Exception as e:
            print(f"[JogosTimes] {eid}: {e}")


def _log_task_error(nome: str, error: BaseException) -> None:
    import traceback
    print(f"[Task:{nome}] {error!r}")
    traceback.print_exception(type(error), error, error.__traceback__)


for _task_loop, _task_nome in (
    (checar_jogos_ao_vivo, "checar_jogos_ao_vivo"),
    (atualizar_players, "atualizar_players"),
    (resumo_diario, "resumo_diario"),
    (verificar_noticias_times, "verificar_noticias_times"),
    (lembrete_pre_jogo, "lembrete_pre_jogo"),
    (verificar_jogos_times, "verificar_jogos_times"),
    (monitorar_eventos_times, "monitorar_eventos_times"),
):
    @_task_loop.before_loop
    async def _before_task(_loop=_task_loop):
        await bot.wait_until_ready()

    @_task_loop.error
    async def _on_task_error(error, _nome=_task_nome):
        _log_task_error(_nome, error)


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


class _BotaoCanal(discord.ui.Button):
    """Botão de seleção de canal dentro de SelecaoCanaisView."""
    def __init__(self, label: str, canal: dict, event_id: str, slug: str,
                 nome_casa: str, nome_fora: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.canal     = canal
        self.event_id  = event_id
        self.slug      = slug
        self.nome_casa = nome_casa
        self.nome_fora = nome_fora

    async def callback(self, interaction: discord.Interaction):
        title      = f"{self.nome_casa} × {self.nome_fora}"
        token      = _criar_sessao(self.canal["url"], title, self.event_id, self.slug,
                                   None, self.nome_casa, self.nome_fora)
        player_url = f"{SERVER_URL}/player/{token}"
        for item in self.view.children:
            item.disabled = True
        # Responde imediatamente para não expirar a interação (limite Discord = 3s)
        await interaction.response.edit_message(view=self.view)
        await interaction.followup.send(
            f"📺 **{title}**\nCanal: **{self.canal['name']}**\n{player_url}",
            ephemeral=False,
        )


class SelecaoCanaisView(discord.ui.View):
    """View com botões para o usuário escolher o sub-canal (ESPN / ESPN 2 / ESPN 3)."""
    def __init__(self, opcoes: list[tuple[str, dict]], event_id: str, slug: str,
                 nome_casa: str, nome_fora: str):
        super().__init__(timeout=120)
        for nome, canal in opcoes:
            self.add_item(_BotaoCanal(nome, canal, event_id, slug, nome_casa, nome_fora))


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
            broadcasts_api = list((self.jogo.get("meta") or {}).get("broadcasts", []))
            broadcasts_all = _canais_tv(self.jogo, slug)
            loop = asyncio.get_event_loop()
            print(f"[Player] {nome_casa} × {nome_fora} → api={broadcasts_api} all={broadcasts_all}")

            # Candidatos em ordem de prioridade: canais da API → outros da liga → fallback ESPN FHD
            candidatos: list[str] = []
            for c in broadcasts_api + broadcasts_all:
                if c not in candidatos:
                    candidatos.append(c)
            for fallback in ["ESPN FHD", "ESPN"]:
                if fallback not in candidatos:
                    candidatos.append(fallback)

            canal_iptv = None
            for nome in candidatos:
                canal_iptv = await loop.run_in_executor(None, _iptv_buscar_canal, nome)
                if canal_iptv:
                    print(f"[Player] canal escolhido: {canal_iptv['name']}")
                    break

            if canal_iptv:
                title      = f"{nome_casa} × {nome_fora}"
                token      = _criar_sessao(canal_iptv["url"], title, self.event_id, slug,
                                           None, nome_casa, nome_fora)
                player_url = f"{SERVER_URL}/player/{token}"
                self.label = f"📺 {canal_iptv['name'][:20]}"
                await interaction.message.edit(view=self.view)
                await interaction.followup.send(
                    f"📺 **{title}**\nCanal: **{canal_iptv['name']}**\n{player_url}",
                    ephemeral=False,
                )
            else:
                self.label = "❌ Sem canal"
                await interaction.message.edit(view=self.view)
                await interaction.followup.send(
                    f"⚠️ Canal não encontrado na IPTV para **{nome_casa} × {nome_fora}**.",
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


def _botao_player_visivel(jogo: dict) -> bool:
    """True se o jogo está ao vivo ou começa em ≤5 minutos."""
    status = jogo["fixture"]["status"]["short"]
    if status not in ("NS", "TBD"):
        return True
    try:
        dt_jogo = datetime.fromisoformat(jogo["fixture"]["date"].replace("Z", "+00:00"))
        agora   = datetime.now(timezone.utc)
        if dt_jogo.tzinfo is None:
            dt_jogo = dt_jogo.replace(tzinfo=timezone.utc)
        return (dt_jogo - agora).total_seconds() <= 300
    except Exception:
        return False


class SeguindoView(discord.ui.View):
    """Lista de times seguidos com opção de deixar de seguir."""

    def __init__(self, uid: int, times: list[str]):
        super().__init__(timeout=120)
        self.uid   = uid
        self.times = list(times)

        opts = [discord.SelectOption(label=t, value=t) for t in times[:25]]
        self.sel = discord.ui.Select(
            placeholder="Selecione um time para deixar de seguir...",
            options=opts, row=0,
        )
        self.sel.callback = self._on_select
        self.add_item(self.sel)

        self.btn_deixar = discord.ui.Button(
            label="❌ Deixar de seguir", style=discord.ButtonStyle.danger,
            disabled=True, row=1,
        )
        self.btn_deixar.callback = self._on_deixar
        self.add_item(self.btn_deixar)

    async def _on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Este menu não é seu.", ephemeral=True)
            return
        self._selected = self.sel.values[0]
        self.btn_deixar.disabled = False
        await interaction.response.edit_message(view=self)

    async def _on_deixar(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Este menu não é seu.", ephemeral=True)
            return
        time_sel = getattr(self, "_selected", None)
        if not time_sel:
            await interaction.response.send_message("Selecione um time primeiro.", ephemeral=True)
            return
        msg = _executar_deixar(interaction.user, time_sel)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=msg, view=None)


def _flatten_jogos_ativos(jogos_por_liga: dict) -> list:
    """Lista plana de jogos não encerrados, cada um com `_slug` da liga ESPN."""
    out: list = []
    for chave, jogos in jogos_por_liga.items():
        slug = LIGAS.get(chave) or ""
        for j in jogos:
            if j["fixture"]["status"]["short"] in ("FT", "AET", "PEN"):
                continue
            j["_slug"] = slug
            out.append(j)
    return out[:25]


def _slug_do_jogo(jogo: dict, slug_padrao: str) -> str:
    return jogo.get("_slug") or slug_padrao


async def _criar_evento_voz(guild: discord.Guild, jogo: dict, slug_jogo: str) -> tuple[bool, str]:
    """Cria um evento de voz (CANAL_EVENTO) para o jogo e avisa no chat do canal.

    Retorna (sucesso, mensagem). Assume que a interação já foi reconhecida —
    o chamador faz o followup com a mensagem retornada.
    """
    me = guild.me
    if not me or not me.guild_permissions.manage_events:
        return False, "❌ O bot precisa da permissão **Gerenciar Eventos** neste servidor."

    nome_casa = jogo["teams"]["home"]["name"]
    nome_fora = jogo["teams"]["away"]["name"]
    titulo    = f"{nome_casa} × {nome_fora}"

    try:
        start_dt = datetime.fromisoformat(
            jogo["fixture"]["date"].replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except Exception:
        return False, "❌ Data do jogo inválida."

    agora = datetime.now(timezone.utc)
    if start_dt < agora:
        if jogo["fixture"]["status"]["short"] in ("FT", "AET", "PEN"):
            return False, "❌ Este jogo já encerrou — não é possível criar evento."
        start_dt = agora + timedelta(minutes=2)

    slug_key  = next((k for k, v in LIGAS.items() if v == slug_jogo), slug_jogo or "brasileirao")
    liga_nome = LIGAS_META.get(slug_key, {}).get("nome", slug_jogo or "Futebol")
    cover_path = None

    try:
        cover_path = await gerar_evento_cover_png(jogo, liga_nome)
        with open(cover_path, "rb") as f:
            cover_bytes = f.read()

        meta       = jogo.get("meta", {})
        venue      = meta.get("venue", "")
        broadcasts = _canais_tv(jogo, slug_jogo)
        tv_line    = ", ".join(broadcasts[:3]) if broadcasts else "A confirmar"
        descricao  = (
            f"⚽ {liga_nome}\n"
            f"🏟️ {venue or 'Estádio a confirmar'}\n"
            f"📺 {tv_line}\n\n"
            f"Assista com a galera no servidor!"
        )[:1000]

        canal_voz = bot.get_channel(CANAL_EVENTO_ID)
        if canal_voz is None:
            try:
                canal_voz = await bot.fetch_channel(CANAL_EVENTO_ID)
            except Exception:
                canal_voz = None

        if not isinstance(canal_voz, (discord.VoiceChannel, discord.StageChannel)):
            return False, f"❌ O canal `{CANAL_EVENTO_ID}` não foi encontrado ou não é um canal de voz."

        evento = await guild.create_scheduled_event(
            name=titulo[:100],
            description=descricao,
            start_time=start_dt,
            entity_type=discord.EntityType.voice,
            channel=canal_voz,
            privacy_level=discord.PrivacyLevel.guild_only,
            image=cover_bytes,
        )

        link     = evento.url
        data_brt = start_dt.astimezone(BRT).strftime("%d/%m/%Y às %H:%M")

        avisou = True
        try:
            await canal_voz.send(
                f"📅 **Evento criado:** {titulo}\n"
                f"🗓️ {data_brt} (BRT)\n"
                f"🔊 {canal_voz.mention}\n"
                f"🔗 {link}"
            )
        except Exception as e:
            avisou = False
            print(f"[Evento] Falha ao avisar no canal de voz {CANAL_EVENTO_ID}: {e}")

        return True, (
            f"✅ Evento **{titulo}** criado!\n"
            f"🗓️ {data_brt} (BRT)\n"
            f"🔊 {canal_voz.mention}\n"
            f"🔗 {link}"
            + ("" if avisou else "\n⚠️ Não foi possível avisar no chat do canal de voz.")
        )
    except discord.Forbidden:
        return False, "❌ Sem permissão para criar eventos neste servidor."
    except Exception as e:
        print(f"[CriarEvento] Erro: {e}")
        return False, f"❌ Erro ao criar evento: `{e}`"
    finally:
        if cover_path and os.path.isfile(cover_path):
            try:
                os.remove(cover_path)
            except OSError:
                pass


class SeguirView(discord.ui.View):
    """Select menu com todos os jogos + botões Detalhes / Abrir Player / Criar Evento."""

    _LIVE_STATUSES = {"1H", "2H", "ET", "BT", "P", "LIVE"}

    def __init__(self, jogos: list, slug: str):
        super().__init__(timeout=600)
        self.slug  = slug
        self.jogos = jogos[:25]
        self.selected: dict | None = None

        # Row 0 — Select menu
        options = []
        for i, j in enumerate(self.jogos):
            short  = j["fixture"]["status"]["short"]
            home   = j["teams"]["home"]["name"]
            away   = j["teams"]["away"]["name"]
            slug_j = _slug_do_jogo(j, slug)
            chave  = next((k for k, v in LIGAS.items() if v == slug_j), "")
            emoji  = LIGAS_META.get(chave, {}).get("emoji", "⚽") if slug_j else "⚽"
            prefix = f"{emoji} " if j.get("_slug") else ""
            label  = f"{prefix}{home[:16]} × {away[:16]}"[:100]
            if short in self._LIVE_STATUSES:
                elapsed = j["fixture"]["status"].get("elapsed") or ""
                desc = f"🔴 Ao Vivo{' · ' + str(elapsed) + chr(39) if elapsed else ''}"
            elif short == "HT":
                desc = "⏸ Intervalo"
            elif short in ("FT", "AET", "PEN"):
                desc = "✅ Encerrado"
            else:
                try:
                    dt  = datetime.fromisoformat(j["fixture"]["date"].replace("Z", "+00:00")).astimezone(BRT)
                    if dt.date() == datetime.now(tz=BRT).date():
                        desc = f"🕐 {dt.strftime('%H:%M')}"
                    else:
                        desc = f"🗓️ {dt.strftime('%d/%m %H:%M')}"
                except Exception:
                    desc = "🕐 —"
            options.append(discord.SelectOption(label=label, value=str(i), description=desc[:100]))

        sel = discord.ui.Select(placeholder="🔍 Selecione um jogo...", options=options, row=0)
        sel.callback = self._on_select
        self.add_item(sel)

        # Row 1 — botões (desabilitados até seleção)
        self.btn_det = discord.ui.Button(
            label="📋 Detalhes", style=discord.ButtonStyle.primary, disabled=True, row=1)
        self.btn_det.callback = self._on_detalhes
        self.add_item(self.btn_det)

        self.btn_play = discord.ui.Button(
            label="📺 Abrir Player", style=discord.ButtonStyle.danger, disabled=True, row=1)
        self.btn_play.callback = self._on_player
        self.add_item(self.btn_play)

        self.btn_evento = discord.ui.Button(
            label="📅 Criar Evento", style=discord.ButtonStyle.success, disabled=True, row=1)
        self.btn_evento.callback = self._on_criar_evento
        self.add_item(self.btn_evento)

        self.btn_seguir = discord.ui.Button(
            label="⭐ Seguir time", style=discord.ButtonStyle.success, disabled=True, row=2)
        self.btn_seguir.callback = self._on_seguir_time
        self.add_item(self.btn_seguir)

    async def _on_select(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        self.selected = self.jogos[idx]
        short = self.selected["fixture"]["status"]["short"]
        is_live = short in self._LIVE_STATUSES or short == "HT" or _botao_player_visivel(self.selected)
        self.btn_det.disabled     = False
        self.btn_play.disabled    = not (IPTV_URL and is_live)
        self.btn_evento.disabled  = False
        self.btn_seguir.disabled  = False
        await interaction.response.edit_message(view=self)

    async def _on_seguir_time(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message("Selecione um jogo primeiro.", ephemeral=True)
            return
        home = self.selected["teams"]["home"]["name"]
        away = self.selected["teams"]["away"]["name"]
        await interaction.response.send_message(
            "Qual time você quer seguir?",
            view=SeguirEscolhaTimeView([home, away]),
            ephemeral=True,
        )

    async def _on_detalhes(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message("Selecione um jogo primeiro.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        jogo      = self.selected
        slug      = _slug_do_jogo(jogo, self.slug)
        broadcasts = _canais_tv(jogo, slug)
        meta       = jogo.get("meta", {})
        nome_casa  = jogo["teams"]["home"]["name"]
        nome_fora  = jogo["teams"]["away"]["name"]
        try:
            dt = datetime.fromisoformat(jogo["fixture"]["date"].replace("Z", "+00:00")).astimezone(BRT)
            data_hora = dt.strftime("%d/%m/%Y às %H:%M (BRT)")
        except Exception:
            data_hora = "—"
        embed = discord.Embed(title=f"📋 {nome_casa} × {nome_fora}", color=0x3B82F6)
        embed.add_field(name="🗓️ Data/Hora",   value=data_hora, inline=False)
        embed.add_field(name="🏟️ Estádio",     value=meta.get("venue") or "Não informado", inline=False)
        embed.add_field(name="📺 Transmissão", value="\n".join(broadcasts) if broadcasts else "Não informado", inline=False)
        if meta.get("odds"):
            embed.add_field(name="🎰 Odds", value=meta["odds"], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _on_player(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message("Selecione um jogo primeiro.", ephemeral=True)
            return
        self.btn_play.disabled = True
        self.btn_play.label    = "⏳ Carregando..."
        self.btn_play.style    = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

        jogo      = self.selected
        slug      = _slug_do_jogo(jogo, self.slug)
        nome_casa = jogo["teams"]["home"]["name"]
        nome_fora = jogo["teams"]["away"]["name"]
        event_id  = str(jogo["fixture"]["id"])
        broadcasts_api = list((jogo.get("meta") or {}).get("broadcasts", []))
        broadcasts_all = _canais_tv(jogo, slug)
        loop = asyncio.get_event_loop()

        # Prioridade 1: canal IPTV específico para a partida
        canal_iptv = await loop.run_in_executor(None, _iptv_buscar_jogo, nome_casa, nome_fora)

        # Prioridade 2: canais por nome (ESPN API + mapeamento fixo + fallback)
        if not canal_iptv:
            candidatos: list[str] = []
            for c in broadcasts_api + broadcasts_all:
                if c not in candidatos:
                    candidatos.append(c)
            for fb in ["ESPN FHD", "ESPN"]:
                if fb not in candidatos:
                    candidatos.append(fb)
            for nome in candidatos:
                canal_iptv = await loop.run_in_executor(None, _iptv_buscar_canal, nome)
                if canal_iptv:
                    break

        if canal_iptv:
            title      = f"{nome_casa} × {nome_fora}"
            token      = _criar_sessao(canal_iptv["url"], title, event_id, slug,
                                       None, nome_casa, nome_fora)
            player_url = f"{SERVER_URL}/player/{token}"
            self.btn_play.label = f"📺 {canal_iptv['name'][:20]}"
            self.btn_play.style = discord.ButtonStyle.danger
            await interaction.message.edit(view=self)
            await interaction.followup.send(
                f"📺 **{title}**\nCanal: **{canal_iptv['name']}**\n{player_url}",
                ephemeral=False,
            )
        else:
            self.btn_play.label = "❌ Sem canal"
            self.btn_play.style = discord.ButtonStyle.secondary
            await interaction.message.edit(view=self)
            await interaction.followup.send(
                f"⚠️ Canal não encontrado na IPTV para **{nome_casa} × {nome_fora}**.",
                ephemeral=True,
            )

    async def _on_criar_evento(self, interaction: discord.Interaction):
        if not self.selected:
            await interaction.response.send_message("Selecione um jogo primeiro.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("Use este botão em um servidor.", ephemeral=True)
            return

        self.btn_evento.disabled = True
        self.btn_evento.label    = "⏳ Criando..."
        await interaction.response.edit_message(view=self)

        slug_jogo = _slug_do_jogo(self.selected, self.slug)
        ok, msg   = await _criar_evento_voz(interaction.guild, self.selected, slug_jogo)

        if ok:
            self.btn_evento.label = "✅ Evento criado"
        else:
            self.btn_evento.disabled = False
            self.btn_evento.label    = "📅 Criar Evento"
        await interaction.message.edit(view=self)
        await interaction.followup.send(msg, ephemeral=True)


# ==========================================
# UX — menu, atalhos e menos digitação
# ==========================================

def _lista_times_menu() -> list[str]:
    """Times para selects do menu / !seguir sem argumentos."""
    if _bz_team_map_cache:
        nomes = sorted({d for _, d in _bz_team_map_cache.values()})
    else:
        nomes = []
    vistos: set[str] = set()
    out: list[str] = []
    for t in _TIMES_BR_AUTOCOMPLETE + nomes:
        k = t.lower()
        if k not in vistos:
            vistos.add(k)
            out.append(t)
        if len(out) >= 25:
            break
    return out


async def _executar_notificacoes(
    user: discord.abc.User,
    tipo: str | None = None,
    estado: str | None = None,
) -> str:
    """Mostra ou altera preferências de DM do !seguir."""
    uid_str = str(user.id)
    if uid_str not in _SEGUINDO:
        _SEGUINDO[uid_str] = {"times": [], "noticias_vistas": {}, "prefs": dict(_PREFS_PADRAO)}
    dados = _SEGUINDO[uid_str]
    prefs = _prefs_de(dados)
    chaves = {"noticias", "jogos", "lembrete", "notícias", "noticia"}
    ligar  = {"ligar", "on", "ativar", "sim", "true", "1"}
    desligar = {"desligar", "off", "desativar", "nao", "não", "false", "0"}

    if not tipo:
        return (
            "**🔔 Suas notificações**\n"
            f"• 📰 Notícias: {'✅ ligado' if prefs['noticias'] else '❌ desligado'}\n"
            f"• ⚽ Jogos ao vivo: {'✅ ligado' if prefs['jogos'] else '❌ desligado'}\n"
            f"• ⏰ Lembrete pré-jogo (~30 min): {'✅ ligado' if prefs['lembrete'] else '❌ desligado'}\n\n"
            "Alterar: `!notificacoes noticias desligar` ou `/notificacoes`"
        )

    t = tipo.lower().replace("notícias", "noticias").replace("noticia", "noticias")
    if t not in ("noticias", "jogos", "lembrete"):
        return "❌ Tipo inválido. Use: `noticias`, `jogos` ou `lembrete`."
    if not estado:
        val = prefs[t]
        return f"**{t}** está {'ligado' if val else 'desligado'}. Use `ligar` ou `desligar`."
    e = estado.lower()
    if e in ligar:
        prefs[t] = True
    elif e in desligar:
        prefs[t] = False
    else:
        return "❌ Estado inválido. Use `ligar` ou `desligar`."
    dados["prefs"] = prefs
    _salvar_seguindo(_SEGUINDO)
    return f"✅ **{t}** {'ativado' if prefs[t] else 'desativado'}."


async def _executar_seguir(user: discord.abc.User, time: str) -> str:
    """Registra time seguido e tenta DM. Retorna mensagem para o usuário."""
    time = (time or "").strip()
    if not time:
        return "❌ Informe o nome do time."
    if BZZOIRO_TOKEN:
        resolvido = _bz_resolve_team(time)
        if resolvido:
            _, time = resolvido
        else:
            return (
                f"❌ Time **{time}** não encontrado na base do bot.\n"
                f"Tente o nome oficial (ex.: Flamengo, Palmeiras, Corinthians)."
            )
    uid_str = str(user.id)
    if uid_str not in _SEGUINDO:
        _SEGUINDO[uid_str] = {
            "times": [], "noticias_vistas": {}, "prefs": dict(_PREFS_PADRAO),
        }
    dados = _SEGUINDO[uid_str]
    dados.setdefault("prefs", dict(_PREFS_PADRAO))
    if any(_strip_accents(t.lower()) == _strip_accents(time.lower())
           for t in dados.get("times", [])):
        return f"📌 Você já segue **{time}**. Use `!seguindo` ou o menu **Meus times**."
    dados.setdefault("times", []).append(time)
    _salvar_seguindo(_SEGUINDO)
    prefs = _prefs_de(dados)
    linhas = []
    if prefs["noticias"]:
        linhas.append("• 📰 Notícias e novidades")
    if prefs["jogos"]:
        linhas.append("• ⚽ Alertas quando o jogo começar")
        linhas.append("• 🥅 Gols e eventos importantes")
        linhas.append("• 🏁 Resultado final")
    if prefs["lembrete"]:
        linhas.append("• ⏰ Lembrete ~30 min antes do jogo")
    aviso_prefs = "\n".join(linhas) if linhas else "• (nenhum alerta ligado — use `!notificacoes`)"
    try:
        await user.send(
            f"⭐ **Você está seguindo {time}!**\n"
            f"Você receberá aqui:\n{aviso_prefs}\n\n"
            f"Ajuste em `!notificacoes`. Use `!seguindo` para deixar de seguir."
        )
        return f"✅ Seguindo **{time}**! Confirmação enviada no privado."
    except discord.Forbidden:
        return (
            f"✅ Seguindo **{time}**!\n"
            f"⚠️ Não foi possível enviar DM. Ative mensagens diretas do servidor "
            f"em Configurações → Privacidade."
        )


def _executar_deixar(user: discord.abc.User, time: str) -> str:
    """Remove time da lista seguida. Retorna mensagem para o usuário."""
    time = (time or "").strip()
    if not time:
        return "❌ Informe o nome do time."
    busca = time
    if BZZOIRO_TOKEN:
        resolvido = _bz_resolve_team(time)
        if resolvido:
            _, busca = resolvido
    uid_str = str(user.id)
    dados   = _SEGUINDO.get(uid_str, {})
    times   = dados.get("times", [])
    match   = next(
        (t for t in times if _strip_accents(t.lower()) == _strip_accents(busca.lower())),
        None,
    )
    if not match:
        match = next(
            (t for t in times if _time_match(busca, t) or _time_match(time, t)),
            None,
        )
    if not match:
        return f"❌ Você não está seguindo **{time}**. Use `!seguindo` ou `/deixar`."
    times.remove(match)
    _salvar_seguindo(_SEGUINDO)
    return f"✅ Você deixou de seguir **{match}**."


async def _responder_seguindo(
    user: discord.abc.User,
    *,
    interaction: discord.Interaction | None = None,
    ctx=None,
):
    """Lista times seguidos com view para deixar de seguir."""
    times = _SEGUINDO.get(str(user.id), {}).get("times", [])
    if not times:
        texto = (
            "📭 Você não segue nenhum time ainda.\n"
            "Use `!seguir <time>` ou `/seguir` para começar."
        )
        if interaction:
            await interaction.response.send_message(texto, ephemeral=True)
        elif ctx:
            await ctx.send(texto)
        return
    lista = "\n".join(f"• {t}" for t in times)
    embed = discord.Embed(
        title="⭐ Times que você segue",
        description=lista,
        color=0x22C55E,
    )
    embed.set_footer(text=f"{len(times)} time(s) · !deixar <time> · /deixar")
    view = SeguindoView(user.id, times)
    if interaction:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    elif ctx:
        await ctx.send(embed=embed, view=view)


async def _responder_jogos_hoje(
    *,
    interaction: discord.Interaction | None = None,
    ctx=None,
):
    """Envia resumo do dia + select de partidas (mesmo fluxo do !hoje)."""
    msg = None
    if interaction:
        if not interaction.response.is_done():
            await interaction.response.defer()
    elif ctx:
        msg = await ctx.send("📅 Buscando jogos do dia em todas as ligas...")

    loop = asyncio.get_event_loop()
    jogos_por_liga = await _buscar_jogos_resumo_paralelo()

    if not jogos_por_liga:
        texto = "📭 Nenhum jogo encontrado hoje em nenhuma liga."
        if interaction:
            await interaction.followup.send(texto)
        elif msg:
            await msg.edit(content=texto)
        return

    total = sum(len(v) for v in jogos_por_liga.values())
    img   = await gerar_resumo_diario_png(jogos_por_liga)
    bz_events = await loop.run_in_executor(None, buscar_eventos_hoje_bzzoiro)
    view = None
    if bz_events:
        opcoes = _build_partida_options(bz_events)
        if opcoes:
            view = PartidaSelectView(opcoes, permitir_evento=True)

    content = f"📅 **Jogos do Dia** — {total} partidas em {len(jogos_por_liga)} ligas"
    arquivo = discord.File(img)
    if interaction:
        await interaction.followup.send(content=content, file=arquivo, view=view)
    else:
        await msg.delete()
        await ctx.send(content=content, file=arquivo, view=view)


async def _responder_calendario_data(
    data,
    *,
    interaction: discord.Interaction | None = None,
    ctx=None,
):
    """Calendário de uma data (imagem + SeguirView se houver jogos ativos)."""
    data_yyyymmdd = data.strftime("%Y%m%d")
    data_display  = data.strftime("%d/%m/%Y")
    msg = None
    if interaction:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False)
    elif ctx:
        msg = await ctx.send(f"📅 Buscando jogos de **{data_display}**...")

    loop = asyncio.get_event_loop()
    jogos_por_liga: dict = {}
    for chave in LIGAS_RESUMO:
        if chave in LIGAS_COPA:
            continue
        try:
            jogos = await loop.run_in_executor(None, buscar_jogos_do_dia, LIGAS[chave], data_yyyymmdd)
            if jogos:
                jogos_por_liga[chave] = jogos
        except Exception as e:
            print(f"[Calendario] Erro em {chave}: {e}")

    if not jogos_por_liga:
        texto = f"📭 Nenhum jogo em **{data_display}** nas ligas monitoradas."
        if interaction:
            await interaction.followup.send(texto)
        elif msg:
            await msg.edit(content=texto)
        return

    total = sum(len(v) for v in jogos_por_liga.values())
    img   = await gerar_resumo_diario_png(jogos_por_liga, data_display=data_display)
    jogos_ativos = _flatten_jogos_ativos(jogos_por_liga)
    view = SeguirView(jogos_ativos, "") if jogos_ativos else None
    content = f"📅 **Jogos de {data_display}** — {total} partidas em {len(jogos_por_liga)} ligas"
    arquivo = discord.File(img)
    if interaction:
        await interaction.followup.send(content=content, file=arquivo, view=view)
    else:
        await msg.delete()
        await ctx.send(content=content, file=arquivo, view=view)


async def _responder_proximos_time(
    nome_time: str,
    *,
    interaction: discord.Interaction | None = None,
    ctx=None,
    limite: int = PROXIMOS_PADRAO,
):
    limite = _normalizar_limite_proximos(limite)
    msg = None
    if interaction:
        if not interaction.response.is_done():
            await interaction.response.defer()
    elif ctx:
        msg = await ctx.send(f"🔍 Buscando os próximos **{limite}** jogos de **{nome_time}**...")

    loop = asyncio.get_event_loop()

    async def _enviar(jogos: list, nome_oficial: str, nome_liga: str):
        try:
            caminho = await gerar_proximos_png(jogos, nome_oficial, nome_liga)
        except Exception as e:
            err = f"Erro ao gerar imagem: {e}"
            if interaction:
                await interaction.followup.send(err)
            elif msg:
                await msg.edit(content=err)
            return
        arq = discord.File(caminho)
        v   = _view_proximos(jogos)
        if interaction:
            await interaction.followup.send(file=arq, view=v)
        else:
            await msg.delete()
            await ctx.send(file=arq, view=v)

    eh_selecao = bool(_canonical_selecao(nome_time))
    if eh_selecao:
        r = await loop.run_in_executor(
            None, functools.partial(buscar_proximos_selecao, nome_time, None, limite=limite),
        )
        if r:
            jogos, nome_oficial = r
            await _enviar(jogos, nome_oficial, "🌍 Seleções")
            return
        txt = f"Nenhum jogo futuro para **{nome_time}**."
        if interaction:
            await interaction.followup.send(txt)
        elif msg:
            await msg.edit(content=txt)
        return

    bz_result = await loop.run_in_executor(
        None, functools.partial(buscar_proximos_bzzoiro, nome_time, None, limite=limite),
    )
    if bz_result:
        jogos, nome_oficial = bz_result
        await _enviar(jogos, nome_oficial, "")
        return

    txt = f"Nenhum jogo futuro encontrado para **{nome_time}**."
    if interaction:
        await interaction.followup.send(txt)
    elif msg:
        await msg.edit(content=txt)


class SeguirEscolhaTimeView(discord.ui.View):
    """Escolhe casa ou visitante para seguir (ephemeral)."""

    def __init__(self, times: list[str]):
        super().__init__(timeout=60)
        opts = [discord.SelectOption(label=t, value=t) for t in times[:25]]
        sel = discord.ui.Select(placeholder="Qual time seguir?", options=opts)
        sel.callback = self._on_pick
        self.add_item(sel)

    async def _on_pick(self, interaction: discord.Interaction):
        time = interaction.data["values"][0]
        msg  = await _executar_seguir(interaction.user, time)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=msg, view=self)


class SeguirTimeSelectView(discord.ui.View):
    """Select de times populares para !seguir / menu."""

    def __init__(self, modo: str = "seguir"):
        super().__init__(timeout=120)
        self.modo = modo
        times = _lista_times_menu()
        if not times:
            return
        ph = "⭐ Escolha um time para seguir..." if modo == "seguir" else "📅 Escolha um time (próximos jogos)..."
        sel = discord.ui.Select(
            placeholder=ph,
            options=[discord.SelectOption(label=t, value=t) for t in times],
            row=0,
        )
        sel.callback = self._on_pick
        self.add_item(sel)

    async def _on_pick(self, interaction: discord.Interaction):
        time = interaction.data["values"][0]
        if self.modo == "proximos":
            await interaction.response.defer(ephemeral=True)
            await _responder_proximos_time(time, interaction=interaction)
            return
        msg = await _executar_seguir(interaction.user, time)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content=msg, view=self)


class MenuLigaSelectView(discord.ui.View):
    """Select de liga para gerar tabela pelo menu."""

    def __init__(self):
        super().__init__(timeout=90)
        opts = [
            discord.SelectOption(
                label=f"{LIGAS_META.get(k, {}).get('emoji', '🏆')} {LIGAS_META.get(k, {}).get('nome', k.title())}",
                value=k,
            )
            for k in LIGAS
            if k not in LIGAS_COPA
        ][:25]
        sel = discord.ui.Select(placeholder="📊 Escolha a liga...", options=opts)
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        chave = interaction.data["values"][0]
        meta  = LIGAS_META.get(chave, {"nome": chave.title(), "emoji": "🏆"})
        nome  = f"{meta['emoji']} {meta['nome']}"
        await interaction.response.defer(ephemeral=True)
        loop  = asyncio.get_event_loop()
        try:
            dados = await loop.run_in_executor(None, buscar_tabela_por_chave, chave)
            if not dados:
                await interaction.followup.send("❌ Sem dados de classificação para esta liga.")
                return
            slug = LIGAS.get(chave, "")
            if dados["type"] == "groups":
                img = await gerar_tabela_grupos_png(dados["groups"], meta["nome"], slug=slug)
            else:
                img = await gerar_tabela_png(dados["teams"], meta["nome"], slug=slug)
            await interaction.followup.send(
                content=f"📊 **{nome}**",
                file=discord.File(img),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: `{e}`")


class CalendarioDataModal(discord.ui.Modal, title="Data do calendário"):
    data_input = discord.ui.TextInput(
        label="Data (dd/mm/aaaa)",
        placeholder="30/05/2026",
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dt = datetime.strptime(self.data_input.value.strip(), "%d/%m/%Y").date()
        except ValueError:
            await interaction.response.send_message(
                "❌ Data inválida. Use `dd/mm/aaaa`.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        await _responder_calendario_data(dt, interaction=interaction)


class CalendarioDataView(discord.ui.View):
    """Hoje / amanhã / outra data — sem digitar comando."""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="📅 Hoje", style=discord.ButtonStyle.primary, row=0)
    async def btn_hoje(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        hoje = datetime.now(tz=BRT).date()
        await _responder_calendario_data(hoje, interaction=interaction)

    @discord.ui.button(label="📆 Amanhã", style=discord.ButtonStyle.secondary, row=0)
    async def btn_amanha(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        amanha = datetime.now(tz=BRT).date() + timedelta(days=1)
        await _responder_calendario_data(amanha, interaction=interaction)

    @discord.ui.button(label="🗓 Outra data", style=discord.ButtonStyle.secondary, row=0)
    async def btn_outra(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CalendarioDataModal())


class MenuPrincipalView(discord.ui.View):
    """Painel principal — navegação por botões.

    persistent=True: timeout=None + custom_id (canal fixo, sobrevive restart).
    """

    def __init__(self, *, persistent: bool = False):
        super().__init__(timeout=None if persistent else 300)

    @discord.ui.button(
        label="📅 Jogos de hoje", style=discord.ButtonStyle.primary, row=0,
        custom_id="futebola:menu:hoje",
    )
    async def btn_hoje(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _responder_jogos_hoje(interaction=interaction)

    @discord.ui.button(
        label="📊 Tabela", style=discord.ButtonStyle.secondary, row=0,
        custom_id="futebola:menu:tabela",
    )
    async def btn_tabela(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Escolha a liga:", view=MenuLigaSelectView(), ephemeral=True,
        )

    @discord.ui.button(
        label="⭐ Seguir time", style=discord.ButtonStyle.success, row=0,
        custom_id="futebola:menu:seguir",
    )
    async def btn_seguir(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SeguirTimeSelectView(modo="seguir")
        if not view.children:
            await interaction.response.send_message(
                "Lista de times indisponível no momento.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Escolha o time (notificações no privado):", view=view, ephemeral=True,
        )

    @discord.ui.button(
        label="📋 Meus times", style=discord.ButtonStyle.secondary, row=1,
        custom_id="futebola:menu:seguindo",
    )
    async def btn_seguindo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _responder_seguindo(interaction.user, interaction=interaction)

    @discord.ui.button(
        label="🔜 Próximos jogos", style=discord.ButtonStyle.secondary, row=1,
        custom_id="futebola:menu:proximos",
    )
    async def btn_proximos(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SeguirTimeSelectView(modo="proximos")
        if not view.children:
            await interaction.response.send_message(
                "Lista de times indisponível.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Escolha o time:", view=view, ephemeral=True,
        )

    @discord.ui.button(
        label="🗓 Calendário", style=discord.ButtonStyle.secondary, row=1,
        custom_id="futebola:menu:calendario",
    )
    async def btn_calendario(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "📅 **Calendário** — escolha a data:",
            view=CalendarioDataView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="🔔 Alertas", style=discord.ButtonStyle.secondary, row=2,
        custom_id="futebola:menu:notificacoes",
    )
    async def btn_notificacoes(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = await _executar_notificacoes(interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)


def _embed_menu(*, fixo: bool = False) -> discord.Embed:
    embed = discord.Embed(
        title="⚽ Football Bot — Menu",
        description=(
            "Use os **botões** abaixo — quase não precisa digitar.\n\n"
            "Atalhos: `!hoje` · `/hoje` · `!seguindo` · `/seguir`"
        ),
        color=0x3B82F6,
    )
    embed.add_field(
        name="Dica",
        value="Comandos `/` no Discord têm **autocomplete** (liga, time).",
        inline=False,
    )
    if fixo:
        embed.set_footer(text="Painel fixo deste canal — os botões não expiram")
    return embed


_MENU_CANAL_PATH = os.path.join(os.path.dirname(__file__), "menu_canal.json")


def _carregar_menu_canal() -> dict:
    try:
        with open(_MENU_CANAL_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _salvar_menu_canal(data: dict) -> None:
    with open(_MENU_CANAL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


async def _limpar_menus_duplicados(canal: discord.TextChannel, manter_id: int) -> None:
    """Remove painéis antigos do bot no canal, exceto a mensagem oficial."""
    try:
        async for msg in canal.history(limit=50):
            if msg.id == manter_id or msg.author.id != bot.user.id or not msg.embeds:
                continue
            titulo = msg.embeds[0].title or ""
            if "Football Bot" in titulo and "Menu" in titulo:
                try:
                    await msg.delete()
                    print(f"[Menu] Painel duplicado removido (msg {msg.id})")
                except Exception:
                    pass
    except Exception as e:
        print(f"[Menu] Erro ao limpar duplicados: {e}")


async def _publicar_menu_canal():
    """Publica ou atualiza o menu fixo no canal CANAL_COMANDOS (sempre no on_ready)."""
    if not CANAL_COMANDOS_ID:
        print("[Menu] CANAL_COMANDOS não configurado — painel fixo desativado.")
        return
    canal = bot.get_channel(CANAL_COMANDOS_ID)
    if canal is None:
        try:
            canal = await bot.fetch_channel(CANAL_COMANDOS_ID)
        except Exception as e:
            print(f"[Menu] Canal {CANAL_COMANDOS_ID} inacessível: {e}")
            return
    if not isinstance(canal, discord.TextChannel):
        print(f"[Menu] {CANAL_COMANDOS_ID} não é um canal de texto.")
        return

    view  = MenuPrincipalView(persistent=True)
    embed = _embed_menu(fixo=True)
    meta  = _carregar_menu_canal()
    msg_id_oficial: int | None = None

    if meta.get("channel_id") == CANAL_COMANDOS_ID and meta.get("message_id"):
        try:
            msg = await canal.fetch_message(int(meta["message_id"]))
            await msg.edit(embed=embed, view=view)
            msg_id_oficial = msg.id
            print(f"[Menu] Painel atualizado (#{canal.name}, msg {msg.id})")
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[Menu] Erro ao editar msg {meta.get('message_id')}: {e}")

    if msg_id_oficial is None:
        try:
            async for msg in canal.history(limit=40):
                if msg.author.id != bot.user.id or not msg.embeds:
                    continue
                titulo = msg.embeds[0].title or ""
                if "Football Bot" in titulo and "Menu" in titulo:
                    await msg.edit(embed=embed, view=view)
                    msg_id_oficial = msg.id
                    _salvar_menu_canal({"channel_id": CANAL_COMANDOS_ID, "message_id": msg.id})
                    print(f"[Menu] Painel reutilizado (#{canal.name}, msg {msg.id})")
                    break
        except discord.Forbidden:
            print(f"[Menu] Sem permissão de ler histórico em #{canal.name}")
        except Exception as e:
            print(f"[Menu] Erro ao buscar histórico: {e}")

    if msg_id_oficial is None:
        try:
            msg = await canal.send(embed=embed, view=view)
            msg_id_oficial = msg.id
            _salvar_menu_canal({"channel_id": CANAL_COMANDOS_ID, "message_id": msg.id})
            print(f"[Menu] Novo painel publicado (#{canal.name}, msg {msg.id})")
        except Exception as e:
            print(f"[Menu] Erro ao enviar painel: {e}")
            return

    await _limpar_menus_duplicados(canal, msg_id_oficial)


# ==========================================
# COMANDOS
# ==========================================

@bot.command(name="menu")
async def cmd_menu(ctx):
    """Painel interativo — navegação por botões."""
    await ctx.send(embed=_embed_menu(), view=MenuPrincipalView())


@bot.command(name="tabela")
async def cmd_tabela(ctx, *, nome_liga: str = "brasileirao"):
    chave = nome_liga.lower().replace(" ", "")
    if chave not in LIGAS:
        await ctx.send(f"❌ Liga inválida. Opções: `{'`, `'.join(LIGAS)}`")
        return

    # Copa do Brasil: mata-mata — mostra rodada atual, não pontos corridos
    if chave in LIGAS_COPA:
        msg = await ctx.send("🏆 Buscando rodada atual da **Copa do Brasil**...")
        loop = asyncio.get_event_loop()
        rodada, fixtures = await loop.run_in_executor(None, buscar_rodada_copa_brasil)
        if not fixtures:
            await msg.edit(content="❌ Sem dados da Copa do Brasil no momento.")
            return
        img = await gerar_mata_mata_png(fixtures, "Copa do Brasil", rodada)
        await msg.delete()
        await ctx.send(file=discord.File(img))
        return

    msg = await ctx.send(f"📊 Gerando tabela do **{nome_liga.title()}**...")
    try:
        loop  = asyncio.get_event_loop()
        dados = await loop.run_in_executor(None, buscar_tabela_por_chave, chave)
        if not dados:
            await msg.edit(content="❌ Sem dados de classificação para esta liga. Se for fase eliminatória, use `!chaveamento`.")
            return
        slug = LIGAS[chave]
        if dados["type"] == "groups":
            img = await gerar_tabela_grupos_png(dados["groups"], nome_liga.title(), slug=slug)
        else:
            img = await gerar_tabela_png(dados["teams"], nome_liga.title(), slug=slug)
        await msg.delete()
        await ctx.send(file=discord.File(img))
    except Exception as e:
        print(f"[!tabela] Erro em {chave}: {e}")
        await msg.edit(content=f"❌ Erro ao gerar tabela: `{e}`")


@bot.command(name="tabela_brasileirao")
async def cmd_tabela_brasileirao(ctx):
    await ctx.invoke(bot.get_command("tabela"), nome_liga="brasileirao")


async def _cmd_liga_handler(ctx, chave: str, data_str: str = ""):
    meta         = LIGAS_META.get(chave, {"nome": chave.title(), "emoji": "🏆"})
    nome_display = f"{meta['emoji']} {meta['nome']}"

    data_yyyymmdd = None
    data_display  = None
    if data_str:
        try:
            dt            = datetime.strptime(data_str.strip(), "%d/%m/%Y")
            data_yyyymmdd = dt.strftime("%Y%m%d")
            data_display  = dt.strftime("%d/%m/%Y")
        except ValueError:
            await ctx.send(f"❌ Data inválida. Use `dd/mm/yyyy`. Ex: `!{chave} 01/06/2026`")
            return

    suffix = f" · {data_display}" if data_display else ""
    msg    = await ctx.send(f"📅 Buscando jogos — **{nome_display}**{suffix}...")
    try:
        loop = asyncio.get_event_loop()
        if chave in LIGAS_COPA:
            if data_yyyymmdd:
                await msg.edit(content="❌ A Copa do Brasil não suporta consulta por data.")
                return
            jogos = await loop.run_in_executor(None, buscar_jogos_copa_hoje)
        else:
            jogos = await loop.run_in_executor(None, buscar_jogos_do_dia, LIGAS[chave], data_yyyymmdd)

        img = await gerar_jogos_png(jogos, meta["nome"] + suffix)
        await msg.delete()

        slug = LIGAS.get(chave)
        if not data_yyyymmdd:
            jogos_ativos = [j for j in jogos if j["fixture"]["status"]["short"] not in ("FT", "AET", "PEN")]
            if jogos_ativos and slug:
                await ctx.send(file=discord.File(img), view=SeguirView(jogos_ativos, slug))
                return
        await ctx.send(file=discord.File(img))
    except Exception as e:
        print(f"[!{chave}] Erro: {e}")
        await msg.edit(content=f"❌ Erro ao buscar jogos de **{meta['nome']}**: `{e}`")


def _register_liga_commands():
    def _make(chave):
        async def _cmd(ctx, data_str: str = ""):
            await _cmd_liga_handler(ctx, chave, data_str)
        _cmd.__name__ = f"cmd_{chave}"
        bot.command(name=chave)(_cmd)
    for chave in LIGAS:
        _make(chave)

_register_liga_commands()


@bot.command(name="hoje")
async def cmd_hoje(ctx):
    await _responder_jogos_hoje(ctx=ctx)


@bot.command(name="calendario")
async def cmd_calendario(ctx, data_str: str = None):
    if not data_str:
        await ctx.send(
            "📅 **Calendário** — escolha a data:",
            view=CalendarioDataView(),
        )
        return
    try:
        dt = datetime.strptime(data_str.strip(), "%d/%m/%Y").date()
    except ValueError:
        await ctx.send("❌ Data inválida. Use `dd/mm/aaaa` ou só `!calendario` para os botões.")
        return
    await _responder_calendario_data(dt, ctx=ctx)


@bot.command(name="seguir")
async def cmd_seguir(ctx, *, time: str = ""):
    """Segue um time: recebe notícias e alertas de jogos ao vivo no privado."""
    if not time:
        view = SeguirTimeSelectView(modo="seguir")
        if view.children:
            await ctx.send(
                "⭐ **Seguir time** — escolha na lista (ou use `!seguir Flamengo`):",
                view=view,
            )
        else:
            await ctx.send(
                "**Uso:** `!seguir` (menu) ou `!seguir Flamengo`\n"
                "Também: `!menu` → **⭐ Seguir time**"
            )
        return
    msg = await _executar_seguir(ctx.author, time.strip())
    if ctx.guild:
        try:
            await ctx.message.delete()
        except Exception:
            pass
    await ctx.send(msg, delete_after=12 if "privado" in msg else None)


@bot.command(name="notificacoes", aliases=["prefs", "alertas"])
async def cmd_notificacoes(ctx, tipo: str = None, estado: str = None):
    """Preferências de DM do !seguir: !notificacoes · !notificacoes noticias desligar"""
    msg = await _executar_notificacoes(ctx.author, tipo, estado)
    await ctx.send(msg)


@bot.command(name="deixar")
async def cmd_deixar(ctx, *, time: str = ""):
    """Para de seguir um time."""
    if not time:
        await _responder_seguindo(ctx.author, ctx=ctx)
        return
    await ctx.send(_executar_deixar(ctx.author, time.strip()))


@bot.command(name="seguindo")
async def cmd_seguindo(ctx):
    """Lista os times que você está seguindo."""
    await _responder_seguindo(ctx.author, ctx=ctx)


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


def _view_proximos(jogos: list) -> "SeguirView | None":
    """View com select + Detalhes / Player / Criar Evento para jogos futuros."""
    return SeguirView(jogos, "") if jogos else None


@bot.command(name="proximos")
async def cmd_proximos(ctx, primeiro: str = "", *, resto: str = ""):
    if not primeiro:
        ligas_disp = ", ".join(f"`{k}`" for k in LIGAS)
        await ctx.send(
            f"Uso: `!proximos [N] [time]` ou `!proximos [N] [liga] [time]`\n"
            f"Ex: `!proximos Flamengo` (5 jogos) · `!proximos 10 Flamengo`\n"
            f"· `!proximos brasil` · `!proximos 8 amistosos brasil`\n"
            f"Ligas: {ligas_disp}"
        )
        return

    limite, liga_key, nome_time = _parse_proximos_args(primeiro, resto)

    if not nome_time:
        await ctx.send(f"Informe o nome do time. Ex: `!proximos Flamengo` ou `!proximos 10 Flamengo`")
        return

    msg  = await ctx.send(f"🔍 Buscando os próximos **{limite}** jogos de **{nome_time}**...")
    loop = asyncio.get_event_loop()

    async def _enviar(jogos: list, nome_oficial: str, nome_liga: str):
        try:
            caminho = await gerar_proximos_png(jogos, nome_oficial, nome_liga)
        except Exception as e:
            await msg.edit(content=f"Erro ao gerar imagem: {e}")
            return
        await msg.delete()
        await ctx.send(file=discord.File(caminho), view=_view_proximos(jogos))

    eh_selecao = bool(_canonical_selecao(nome_time)) or (liga_key in _LIGAS_SELECAO)
    if eh_selecao:
        selecao_result = await loop.run_in_executor(
            None,
            functools.partial(buscar_proximos_selecao, nome_time, liga_key, limite=limite),
        )
        if selecao_result:
            jogos, nome_oficial = selecao_result
            if liga_key and liga_key in _LIGAS_SELECAO:
                meta      = LIGAS_META.get(liga_key, {"nome": liga_key.title(), "emoji": "🏆"})
                nome_liga = f"{meta['emoji']} {meta['nome']}"
            else:
                nome_liga = "🌍 Seleções"
            await _enviar(jogos, nome_oficial, nome_liga)
            return
        await msg.edit(
            content=f"Nenhum jogo futuro encontrado para a seleção **{nome_time}**.",
        )
        return

    # Prioridade: Bzzoiro (clubes)
    bz_league_filter = _BZ_LIGA_TO_ID.get(liga_key) if liga_key else None
    bz_result = await loop.run_in_executor(
        None,
        functools.partial(buscar_proximos_bzzoiro, nome_time, bz_league_filter, limite=limite),
    )
    if bz_result:
        jogos, nome_oficial = bz_result
        if liga_key:
            meta      = LIGAS_META.get(liga_key, {"nome": liga_key.title(), "emoji": "🏆"})
            nome_liga = f"{meta['emoji']} {meta['nome']}"
        else:
            nome_liga = ""  # liga badge por card
        await _enviar(jogos, nome_oficial, nome_liga)
        return

    # Fallback: ESPN (liga de clubes especificada)
    if not liga_key or liga_key in LIGAS_COPA or not LIGAS.get(liga_key):
        selecao_result = await loop.run_in_executor(
            None, functools.partial(buscar_proximos_selecao, nome_time, None, limite=limite),
        )
        if selecao_result:
            jogos, nome_oficial = selecao_result
            await _enviar(jogos, nome_oficial, "🌍 Seleções")
            return
        await msg.edit(content=f"Nenhum jogo futuro encontrado para **{nome_time}**.")
        return
    slug      = LIGAS[liga_key]
    resultado = await loop.run_in_executor(None, buscar_time_id, slug, nome_time)
    if not resultado:
        await msg.edit(content=f"Time `{nome_time}` não encontrado na liga `{liga_key}`.")
        return
    team_id, nome_oficial = resultado
    jogos = await loop.run_in_executor(
        None, functools.partial(buscar_proximos_espn_clube, slug, team_id, limite=limite),
    )
    if not jogos:
        await msg.edit(content=f"Nenhum jogo futuro encontrado para **{nome_oficial}**.")
        return
    meta      = LIGAS_META.get(liga_key, {"nome": liga_key.title(), "emoji": "🏆"})
    nome_liga = f"{meta['emoji']} {meta['nome']}"
    await _enviar(jogos, nome_oficial, nome_liga)


@bot.command(name="chaveamento")
async def cmd_chaveamento(ctx, *, nome_liga: str = "champions"):
    chave = nome_liga.lower().replace(" ", "")
    if chave not in LIGAS:
        ligas_disp = ", ".join(f"`{k}`" for k in LIGAS if k not in LIGAS_COPA)
        await ctx.send(f"❌ Liga inválida. Use: {ligas_disp}")
        return

    msg = await ctx.send(f"🏆 Buscando chaveamento — **{nome_liga.title()}**...")

    # Copa do Brasil: bracket estilo Champions (gerar_chaveamento_png)
    if chave in LIGAS_COPA:
        loop = asyncio.get_event_loop()
        rounds = await loop.run_in_executor(None, buscar_chaveamento_copa_brasil)
        if not rounds:
            await msg.edit(content="❌ Sem dados de chaveamento da Copa do Brasil.")
            return
        img = await gerar_chaveamento_png(rounds, "Copa do Brasil 🏆")
        if not img:
            await msg.edit(content="❌ Não foi possível gerar o chaveamento.")
            return
        await msg.delete()
        await ctx.send(file=discord.File(img))
        return

    slug = LIGAS[chave]
    loop = asyncio.get_event_loop()
    rounds = await loop.run_in_executor(None, buscar_chaveamento, slug)

    if not rounds:
        await msg.edit(content="❌ Chaveamento não disponível para esta liga no momento.")
        return

    meta = LIGAS_META.get(chave, {"nome": nome_liga.title(), "emoji": "🏆"})
    nome_display = f"{meta['emoji']} {meta['nome']}"
    try:
        img = await gerar_chaveamento_png(rounds, nome_display)
        await msg.delete()
        await ctx.send(file=discord.File(img))
    except Exception as e:
        print(f"[!chaveamento] Erro: {e}")
        import traceback; traceback.print_exc()
        await msg.edit(content=f"❌ Erro ao gerar chaveamento: `{e}`")


def _build_ajuda_embed() -> discord.Embed:
    embed = discord.Embed(
        title="⚽ Football Bot — Comandos",
        description=(
            "🎛 **Menu fixo** no canal de comandos (botões permanentes) ou `!menu` / `/menu`.\n"
            "Também: `!comando` ou `/comando` com autocomplete."
        ),
        color=0x3B82F6,
    )
    embed.add_field(
        name="📅  Jogos do Dia",
        value=(
            "`!menu` `/menu` — Painel interativo (botões)\n"
            "`!hoje` `/hoje` — Jogos do dia + select de partidas\n"
            "`!brasileirao` `!premierleague` `!champions` … — Jogos da liga hoje\n"
            "`!brasileirao 01/06/2026` — Jogos da liga em uma data específica\n"
            "`!calendario` `/calendario` — Botões **Hoje / Amanhã / Outra data** (+ select de jogos)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏆  Ligas & Tabelas",
        value=(
            "`!tabela [liga]` `/tabela` — Classificação da liga\n"
            "`!artilheiro [liga]` `/artilheiro` — Top artilheiros\n"
            "`!chaveamento [liga]` `/chaveamento` — Bracket mata-mata"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔵  Times",
        value=(
            "`!proximos [time]` — Próximos 5 jogos (padrão)\n"
            "`!proximos 10 Flamengo` — Quantidade opcional (máx. 25)\n"
            "`!proximos brasil` — Seleção Brasileira\n"
            "`!proximos [liga] [time]` ou `!proximos 10 [liga] [time]`\n"
            "`!partida [time]` `/partida` — Detalhes da partida mais recente\n"
            "`!historico [time]` `/historico` — Partidas encerradas (últimos 90 dias)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🔔  Seguir Times (DM privada)",
        value=(
            "`!seguir` — Menu para escolher o time (nome validado via Bzzoiro)\n"
            "`/seguir` — Mesmo com autocomplete de time\n"
            "`!notificacoes` `/notificacoes` — Ligar/desligar notícias, jogos ou lembrete\n"
            "`!deixar` `/deixar` ou `!seguindo` — Parar de seguir (select ou autocomplete)"
        ),
        inline=False,
    )
    embed.add_field(
        name="📡  Monitoramento ao Vivo (canal)",
        value=(
            "`!monitorando` — Jogos sendo monitorados neste canal\n"
            "`!parar [id]` — Parar monitoramento de um jogo"
        ),
        inline=False,
    )
    if IPTV_URL:
        embed.add_field(
            name="📺  IPTV",
            value=(
                "`!canais [busca]` — Listar canais disponíveis\n"
                "`!transmitir [canal]` — Canal para sintonizar\n"
                "Botão **📺 Abrir Player** nos jogos — player HLS ao vivo"
            ),
            inline=False,
        )
    ligas_str = "  ".join(f"`{k}`" for k in LIGAS.keys())
    embed.add_field(name="Ligas disponíveis", value=ligas_str, inline=False)
    embed.set_footer(text="Dados: ESPN · Bzzoiro · API-Football")
    return embed


@bot.command(name="ajuda")
async def cmd_ajuda(ctx):
    await ctx.send(embed=_build_ajuda_embed())


# ==========================================
# DETALHES DA PARTIDA (Bzzoiro)
# ==========================================

def buscar_detalhes_partida(event_id: int) -> dict | None:
    evento = _bzzoiro_get(f"events/{event_id}/")
    if not evento or evento.get("error"):
        return None
    lineups   = _bzzoiro_get(f"events/{event_id}/lineups/")
    stats_d   = _bzzoiro_get(f"events/{event_id}/stats/")
    incidents = _bzzoiro_get(f"events/{event_id}/incidents/")
    venue_id  = evento.get("venue_id")
    venue     = _bzzoiro_get(f"venues/{venue_id}/") if venue_id else None
    return {"evento": evento, "lineups": lineups or {}, "stats": stats_d or {},
            "incidents": incidents or {}, "venue": venue or {}}


def _bz_sv(side: dict, *keys) -> float:
    for k in keys:
        v = (side or {}).get(k)
        if isinstance(v, dict):
            v = v.get("actual") or v.get("value") or v.get("pct") or 0
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


async def _gerar_partida_html(dados: dict) -> str:
    evento       = dados.get("evento") or {}
    lineups_d    = dados.get("lineups") or {}
    stats_raw    = (dados.get("stats") or {}).get("stats") or {}
    incidents_l  = (dados.get("incidents") or {}).get("incidents") or []
    venue_info   = dados.get("venue") or {}

    home_name  = evento.get("home_team", "Casa")
    away_name  = evento.get("away_team", "Visitante")
    home_score = evento.get("home_score")
    away_score = evento.get("away_score")
    ht_home    = evento.get("home_score_ht")
    ht_away    = evento.get("away_score_ht")
    status     = evento.get("status", "")
    period     = (evento.get("period") or "").upper()
    cur_min    = evento.get("current_minute")
    league_id  = evento.get("league_id", 0)
    liga_nome  = _BZ_LEAGUE_NAMES.get(league_id, "")
    round_num  = evento.get("round_number")

    pen_info   = evento.get("penalty_shootout") or {}
    referee    = evento.get("referee", "")
    attendance = evento.get("attendance")

    try:
        dt       = datetime.fromisoformat(evento.get("event_date","").replace("Z","+00:00")).astimezone(BRT)
        data_fmt = dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        data_fmt = ""

    if status == "notstarted":
        badge_text, badge_color = "A INICIAR", "#8892a4"
    elif status == "finished":
        if pen_info:
            badge_text = "PÊNALTIS"
        elif period in ("ET", "AET"):
            badge_text = "PRORROGAÇÃO"
        else:
            badge_text = "ENCERRADO"
        badge_color = "#10b981"
    else:
        badge_text = f"{cur_min}'" if cur_min else "AO VIVO"
        badge_color = "#ef4444"

    has_score = status != "notstarted"
    score_txt = f"{home_score} · {away_score}" if has_score else "vs"
    ht_txt    = f"Intervalo {ht_home}–{ht_away}" if has_score and ht_home is not None else ""
    pen_txt   = f"Pênaltis {pen_info.get('home','?')}–{pen_info.get('away','?')}" if pen_info else ""

    comp_parts = [p for p in [liga_nome, f"Rodada {round_num}" if round_num else "", data_fmt] if p]
    comp_line  = " · ".join(comp_parts)

    venue_str  = " ".join(filter(None, [venue_info.get("name",""), venue_info.get("city","")])).strip(", ")

    # Logos
    logo_map  = _buscar_logos_brasileiros()
    home_logo = logo_map.get(home_name, "")
    away_logo = logo_map.get(away_name, "")
    logos_b64 = await _baixar_logos_paralelo([u for u in [home_logo, away_logo] if u])
    home_b64  = logos_b64.get(home_logo, "")
    away_b64  = logos_b64.get(away_logo, "")

    def logo_img(b64, size=64):
        if b64:
            return f'<img src="{b64}" width="{size}" height="{size}" style="object-fit:contain;display:block">'
        return f'<div style="width:{size}px;height:{size}px;background:#0f3460;border-radius:8px"></div>'

    # ── GOALSCORERS (extrair de incidents) ──
    def _gols_lado(is_home: bool) -> str:
        gols = []
        for inc in sorted(incidents_l, key=lambda i: (i.get("minute") or 0)):
            if inc.get("type") != "goal" or bool(inc.get("is_home", True)) != is_home:
                continue
            mn  = inc.get("minute", "")
            add = inc.get("added_time")
            mn_str = f"{mn}+{add}'" if add else f"{mn}'"
            own = inc.get("goal_type") == "own"
            gols.append(f'{inc.get("player","?")} {mn_str}{"  cg" if own else ""}')
        return " / ".join(gols)

    home_gols_str = _gols_lado(True)
    away_gols_str = _gols_lado(False)

    # ── HEADER ──
    meta_rows = ""
    if venue_str:
        meta_rows += f'<div class="meta-item">📍 {venue_str}</div>'
    if referee:
        meta_rows += f'<div class="meta-item">👨‍⚖️ {referee}</div>'
    if attendance:
        meta_rows += f'<div class="meta-item">👥 {int(attendance):,} pessoas'.replace(",",".")+'</div>'

    html_header = f"""<div class="header">
  <div class="comp-line">{comp_line}</div>
  <div class="score-row">
    <div class="team-box">
      {logo_img(home_b64)}
      <div class="tname">{home_name}</div>
      {"<div class='goal-scorers'>" + home_gols_str + "</div>" if home_gols_str else ""}
    </div>
    <div class="score-mid">
      <div class="score-big">{score_txt}</div>
      {"<div class='ht-txt'>" + ht_txt + "</div>" if ht_txt else ""}
      {"<div class='pen-txt'>" + pen_txt + "</div>" if pen_txt else ""}
      <span class="badge" style="background:{badge_color}">{badge_text}</span>
    </div>
    <div class="team-box">
      {logo_img(away_b64)}
      <div class="tname">{away_name}</div>
      {"<div class='goal-scorers'>" + away_gols_str + "</div>" if away_gols_str else ""}
    </div>
  </div>
  {"<div class='meta-row'>" + meta_rows + "</div>" if meta_rows else ""}
</div>"""

    # ── INCIDENTS ──
    def inc_min(inc):
        m, a = inc.get("minute", 0), inc.get("added_time")
        return f"{m}+{a}'" if a else f"{m}'"

    inc_rows = ""
    for inc in sorted(incidents_l, key=lambda i: (i.get("minute") or 0, i.get("added_time") or 0)):
        t       = inc.get("type", "")
        is_home = inc.get("is_home", True)
        mn      = inc_min(inc)

        if t == "period":
            if "HT" in (inc.get("text","")).upper() or "HALF" in (inc.get("text","")).upper():
                hs, as_ = inc.get("home_score",""), inc.get("away_score","")
                inc_rows += f'<div class="period-sep">── INTERVALO {hs}–{as_} ──</div>'
            continue

        if t == "injuryTime":
            inc_rows += (f'<div class="inc-row inc-sub"><div class="inc-h"></div>'
                         f'<div class="inc-m">+{inc.get("length","?")}\'</div>'
                         f'<div class="inc-a" style="font-size:10px;color:#6b7a99">acréscimos</div></div>')
            continue

        if t == "goal":
            player = inc.get("player",""); assist = inc.get("assist","")
            icon   = "⚽"
            own    = inc.get("goal_type") == "own"
            txt    = f'<b>{player}</b>' + (f' <span class="assist">({assist})</span>' if assist else "") + (' <span class="cg">CG</span>' if own else "")
            if is_home:
                inc_rows += f'<div class="inc-row"><div class="inc-h"><span class="ic">{icon}</span>{txt}</div><div class="inc-m">{mn}</div><div class="inc-a"></div></div>'
            else:
                inc_rows += f'<div class="inc-row"><div class="inc-h"></div><div class="inc-m">{mn}</div><div class="inc-a">{txt}<span class="ic">{icon}</span></div></div>'

        elif t == "card":
            player = inc.get("player",""); card = inc.get("card_type","yellow")
            icon   = "🟡" if card == "yellow" else "🟥"
            if is_home:
                inc_rows += f'<div class="inc-row inc-sub"><div class="inc-h"><span class="ic">{icon}</span>{player}</div><div class="inc-m">{mn}</div><div class="inc-a"></div></div>'
            else:
                inc_rows += f'<div class="inc-row inc-sub"><div class="inc-h"></div><div class="inc-m">{mn}</div><div class="inc-a">{player}<span class="ic">{icon}</span></div></div>'

        elif t == "substitution":
            p_in = inc.get("player_in",""); p_out = inc.get("player_out","")
            txt  = f'<span class="sub-in">↑ {p_in}</span> <span class="sub-out">↓ {p_out}</span>'
            if is_home:
                inc_rows += f'<div class="inc-row inc-sub"><div class="inc-h">{txt}</div><div class="inc-m">{mn}</div><div class="inc-a"></div></div>'
            else:
                inc_rows += f'<div class="inc-row inc-sub"><div class="inc-h"></div><div class="inc-m">{mn}</div><div class="inc-a">{txt}</div></div>'

    html_incidents = f"""<div class="section">
  <div class="sec-title">⏱ Lance a Lance</div>
  <div class="inc-hdr"><div class="inc-h" style="color:#8892a4;font-size:11px">{home_name}</div><div class="inc-m"></div><div class="inc-a" style="color:#8892a4;font-size:11px">{away_name}</div></div>
  {inc_rows}
</div>"""

    # ── STATS ──
    sh = stats_raw.get("home") or {}
    sa = stats_raw.get("away") or {}

    def stat_row(label, hv, av, fmt=".0f", pct=False):
        total = hv + av
        h_bar = round(hv) if pct else (round(hv / total * 100) if total > 0 else 50)
        a_bar = 100 - h_bar
        suf   = "%" if pct else ""
        hd    = (f"{hv:.2f}" if fmt == ".2f" else f"{hv:.0f}") + suf
        ad    = (f"{av:.2f}" if fmt == ".2f" else f"{av:.0f}") + suf
        return (f'<div class="stat-row">'
                f'<div class="sv sh">{hd}</div>'
                f'<div class="sbar"><div class="sb-h" style="width:{h_bar}%"></div></div>'
                f'<div class="slabel">{label}</div>'
                f'<div class="sbar"><div class="sb-a" style="width:{a_bar}%"></div></div>'
                f'<div class="sv sa">{ad}</div>'
                f'</div>')

    stats_rows = ""
    if sh or sa:
        stats_rows += stat_row("Posse de Bola",    _bz_sv(sh,"ball_possession"),               _bz_sv(sa,"ball_possession"),               pct=True)
        stats_rows += stat_row("xG",               _bz_sv(sh,"xg","expected_goals"),            _bz_sv(sa,"xg","expected_goals"),            fmt=".2f")
        stats_rows += stat_row("Finalizações",      _bz_sv(sh,"total_shots"),                    _bz_sv(sa,"total_shots"))
        stats_rows += stat_row("No Alvo",           _bz_sv(sh,"shots_on_target"),                _bz_sv(sa,"shots_on_target"))
        stats_rows += stat_row("Fora do Alvo",      _bz_sv(sh,"shots_off_target"),               _bz_sv(sa,"shots_off_target"))
        stats_rows += stat_row("Bloqueados",        _bz_sv(sh,"blocked_shots"),                  _bz_sv(sa,"blocked_shots"))
        stats_rows += stat_row("Dentro da Área",    _bz_sv(sh,"shots_inside_box"),               _bz_sv(sa,"shots_inside_box"))
        stats_rows += stat_row("Passes Totais",     _bz_sv(sh,"total_passes"),                   _bz_sv(sa,"total_passes"))
        stats_rows += stat_row("Precisão de Passe", _bz_sv(sh,"pass_accuracy_pct"),              _bz_sv(sa,"pass_accuracy_pct"),              pct=True)
        stats_rows += stat_row("Escanteios",        _bz_sv(sh,"corner_kicks"),                   _bz_sv(sa,"corner_kicks"))
        stats_rows += stat_row("Impedimentos",      _bz_sv(sh,"offsides"),                       _bz_sv(sa,"offsides"))
        stats_rows += stat_row("Faltas",            _bz_sv(sh,"fouls"),                          _bz_sv(sa,"fouls"))
        stats_rows += stat_row("Cart. Amarelos",    _bz_sv(sh,"yellow_cards"),                   _bz_sv(sa,"yellow_cards"))
        stats_rows += stat_row("Cart. Vermelhos",   _bz_sv(sh,"red_cards"),                      _bz_sv(sa,"red_cards"))
        stats_rows += stat_row("Defesas",           _bz_sv(sh,"goalkeeper_saves","total_saves"), _bz_sv(sa,"goalkeeper_saves","total_saves"))

    html_stats = (f'<div class="section"><div class="sec-title">📊 Estatísticas</div>'
                  f'<div class="stats-body">{stats_rows}</div></div>') if stats_rows else ""

    # ── LINEUPS ──
    lh = (lineups_d.get("lineups") or {}).get("home") or {}
    la = (lineups_d.get("lineups") or {}).get("away") or {}
    home_xi   = lh.get("players") or []
    away_xi   = la.get("players") or []
    home_form = lh.get("formation","")
    away_form = la.get("formation","")

    def pl_home(p):
        return (f'<div class="pl-row">'
                f'<span class="pl-jersey">{p.get("jersey_number","")}</span>'
                f'<span class="pl-pos">{p.get("position","")}</span>'
                f'<span class="pl-name">{p.get("name","")}</span>'
                f'</div>')

    def pl_away(p):
        return (f'<div class="pl-row pl-away">'
                f'<span class="pl-name">{p.get("name","")}</span>'
                f'<span class="pl-pos">{p.get("position","")}</span>'
                f'<span class="pl-jersey">{p.get("jersey_number","")}</span>'
                f'</div>')

    html_lineups = ""
    if home_xi or away_xi:
        home_header = f'{home_name}{"  (" + home_form + ")" if home_form else ""}'
        away_header = f'{away_name}{"  (" + away_form + ")" if away_form else ""}'
        html_lineups = (f'<div class="section"><div class="sec-title">👥 Escalações</div>'
                        f'<div class="lineups-row">'
                        f'<div class="lu-col"><div class="lu-head">{home_header}</div>{"".join(pl_home(p) for p in home_xi)}</div>'
                        f'<div class="lu-div"></div>'
                        f'<div class="lu-col lu-col-away"><div class="lu-head">{away_header}</div>{"".join(pl_away(p) for p in away_xi)}</div>'
                        f'</div></div>')

    # ── CSS ──
    css = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1b2a;color:#e0e0e0;font-family:'Segoe UI',Arial,sans-serif;min-height:100vh}
.wrap{max-width:820px;margin:0 auto;padding:16px}
.header{background:#16213e;border-radius:12px;padding:24px;text-align:center;margin-bottom:4px}
.comp-line{font-size:13px;color:#8892a4;margin-bottom:16px}
.score-row{display:flex;align-items:center;justify-content:center;gap:8px}
.team-box{flex:1;display:flex;flex-direction:column;align-items:center;gap:10px}
.tname{font-size:16px;font-weight:700;text-align:center}
.score-mid{min-width:180px;text-align:center;flex-shrink:0}
.score-big{font-size:60px;font-weight:800;color:#fff;letter-spacing:4px}
.ht-txt{font-size:13px;color:#8892a4;margin-top:4px}
.badge{display:inline-block;padding:4px 16px;border-radius:14px;font-size:12px;font-weight:700;color:#fff;margin-top:10px}
.goal-scorers{font-size:11px;color:#93c5fd;margin-top:6px;text-align:center;line-height:1.5;max-width:180px}
.pen-txt{font-size:13px;color:#fbbf24;margin-top:4px}
.meta-row{display:flex;flex-wrap:wrap;justify-content:center;gap:16px;margin-top:14px}
.meta-item{font-size:12px;color:#8892a4}
.section{border-radius:12px;overflow:hidden;margin-top:8px}
.sec-title{background:#1a2a5e;color:#93c5fd;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;padding:10px 20px}
.inc-hdr,.inc-row{display:flex;align-items:center;padding:0 16px;background:#16213e}
.inc-h{flex:1;display:flex;align-items:center;justify-content:flex-end;gap:5px;text-align:right;padding:8px 8px;font-size:14px}
.inc-a{flex:1;display:flex;align-items:center;gap:5px;text-align:left;padding:8px 8px;font-size:14px}
.inc-m{width:68px;text-align:center;color:#93c5fd;font-size:13px;font-weight:700;flex-shrink:0}
.inc-row{border-bottom:1px solid #111827}
.inc-sub .inc-h,.inc-sub .inc-a{font-size:12px;color:#8892a4}
.ic{font-size:18px;flex-shrink:0}
.assist{color:#8892a4;font-size:12px}
.cg{color:#ef4444;font-size:11px;font-weight:700}
.sub-in{color:#10b981}
.sub-out{color:#ef4444}
.period-sep{background:#0f1c33;color:#8892a4;text-align:center;font-size:12px;padding:8px;letter-spacing:1px}
.stats-body{background:#16213e;padding:8px 24px 14px}
.stat-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #0f1c33}
.sv{width:56px;font-size:14px;font-weight:700;flex-shrink:0}
.sh{text-align:right;color:#60a5fa}
.sa{text-align:left;color:#fbbf24}
.sbar{flex:1;height:6px;background:#0f3460;border-radius:3px;overflow:hidden}
.sb-h{background:#3b82f6;height:100%;float:right}
.sb-a{background:#f59e0b;height:100%;float:left}
.slabel{width:150px;text-align:center;font-size:12px;color:#8892a4;flex-shrink:0}
.lineups-row{display:flex;background:#16213e;padding:16px 0}
.lu-col{flex:1;padding:10px 20px}
.lu-col-away{text-align:right}
.lu-div{width:1px;background:#1e2f5e;flex-shrink:0}
.lu-head{font-size:13px;color:#93c5fd;font-weight:700;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid #1e2f5e}
.pl-row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #0f1c33;font-size:13px}
.pl-away{justify-content:flex-end}
.pl-jersey{width:26px;height:26px;background:#0f3460;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#93c5fd;flex-shrink:0;line-height:26px;text-align:center}
.pl-pos{font-size:10px;color:#8892a4;width:16px;flex-shrink:0;text-align:center}
.pl-name{font-size:13px;font-weight:500}
.footer{text-align:center;font-size:11px;color:#4b5563;padding:16px 0 8px}
"""

    html = (
        f'<!DOCTYPE html><html lang="pt-BR"><head>'
        f'<meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{home_name} × {away_name}</title>'
        f'<style>{css}</style></head><body>'
        f'<div class="wrap">'
        f'{html_header}{html_incidents}{html_stats}{html_lineups}'
        f'<div class="footer">Dados: Bzzoiro Sports Data</div>'
        f'</div></body></html>'
    )
    return html


def _buscar_partidas_passadas(nome_time: str, dias: int = 90) -> tuple[list[dict], str] | None:
    """Retorna (events, display_name) das últimas partidas encerradas de um time em todas as ligas."""
    team_map = _bz_build_team_map()
    busca    = nome_time.lower().strip()
    result   = team_map.get(busca)
    if not result:
        best, best_score = None, 0
        for norm, val in team_map.items():
            if busca in norm or norm in busca:
                score = len(set(busca) & set(norm))
                if score > best_score:
                    best_score, best = score, val
        result = best
    if not result:
        return None
    _, display_name = result

    hoje        = datetime.now(tz=BRT).date()
    amanha      = hoje + timedelta(days=1)
    date_from   = str(hoje - timedelta(days=dias))
    busca_norm  = _strip_accents(busca)

    def _match_team(ev: dict) -> bool:
        for key in ("home_team", "away_team"):
            n = _strip_accents((ev.get(key) or "").lower())
            if busca_norm in n or n in busca_norm:
                return True
        return False

    todos: list[dict] = []
    vistos: set       = set()
    leagues = [
        (_BZ_BRASILEIRAO_LEAGUE, {}),
        (_BZ_COPA_LEAGUE,        {"season_id": _BZ_COPA_SEASON}),
        (_BZ_LIBERTADORES_LEAGUE, {}),
        (_BZ_SULAMERICANA_LEAGUE, {}),
    ]
    for league_id, extra in leagues:
        params = {"league_id": league_id, "date_from": date_from,
                  "date_to": str(amanha), "limit": 100}
        params.update(extra)
        data = _bzzoiro_get("events/", params)
        for ev in (data or {}).get("results", []):
            eid = ev.get("id")
            if not eid or eid in vistos:
                continue
            if ev.get("status") == "finished" and _match_team(ev):
                todos.append(ev)
                vistos.add(eid)

    if not todos:
        return None
    todos.sort(key=lambda e: e.get("event_date", ""), reverse=True)
    return (todos[:10], display_name)


_BZ_LIGA_EMOJI = {
    9:  "🇧🇷",   # Brasileirão
    35: "🏆",    # Copa do Brasil
    32: "🌎",    # Libertadores
    33: "🌎",    # Sul-Americana
}


def _build_historico_options(events: list[dict]) -> list[discord.SelectOption]:
    """Constrói SelectOptions para histórico: label=times, description=data+placar."""
    opts: list[discord.SelectOption] = []
    for ev in events:
        eid = ev.get("id")
        if not eid:
            continue
        home    = ev.get("home_team", "?")
        away    = ev.get("away_team", "?")
        liga_id = ev.get("league_id", 0)
        emoji   = _BZ_LIGA_EMOJI.get(liga_id, "⚽")
        liga_nome = _BZ_LEAGUE_NAMES.get(liga_id, "")
        h_score = ev.get("home_score", "")
        a_score = ev.get("away_score", "")
        placar  = f"{h_score}-{a_score}" if h_score != "" and a_score != "" else "? - ?"
        pen     = ev.get("penalty_shootout") or {}
        if pen.get("home") is not None:
            placar += f" (pen {pen['home']}-{pen['away']})"
        try:
            dt       = datetime.fromisoformat(ev.get("event_date", "").replace("Z", "+00:00")).astimezone(BRT)
            data_str = dt.strftime("%d/%m/%Y")
        except Exception:
            data_str = ""
        label = f"{emoji} {home} × {away}"[:100]
        desc_parts = [p for p in [data_str, liga_nome, placar] if p]
        opts.append(discord.SelectOption(label=label, value=str(eid), description=" · ".join(desc_parts)[:100]))
    return opts[:25]


def _filtrar_eventos_data_brt(eventos: list[dict], dia: date | None = None) -> list[dict]:
    """Mantém só eventos cuja data do apito cai no dia informado (BRT)."""
    dia = dia or datetime.now(tz=BRT).date()
    out: list[dict] = []
    vistos: set = set()
    for ev in eventos:
        eid = ev.get("id")
        if eid in vistos:
            continue
        try:
            ev_brt = datetime.fromisoformat(
                (ev.get("event_date") or "").replace("Z", "+00:00")
            ).astimezone(BRT).date()
        except Exception:
            ev_brt = dia
        if ev_brt == dia:
            vistos.add(eid)
            out.append(ev)
    return out


def eventos_hoje_bzzoiro_todos() -> list[dict]:
    """Eventos de hoje (BRT) em todas as ligas Bzzoiro — uma consulta ampla."""
    hoje   = datetime.now(tz=BRT).date()
    amanha = hoje + timedelta(days=1)
    data   = _bzzoiro_get(
        "events/", {"date_from": str(hoje), "date_to": str(amanha), "limit": 300}
    )
    return _filtrar_eventos_data_brt((data or {}).get("results", []), hoje)


def buscar_eventos_hoje_bzzoiro() -> list[dict]:
    """Eventos de hoje (BRT) nas ligas principais do bot (select / calendário)."""
    hoje   = datetime.now(tz=BRT).date()
    amanha = hoje + timedelta(days=1)
    brutos: list[dict] = []
    leagues = [
        (_BZ_BRASILEIRAO_LEAGUE,  {}),
        (_BZ_COPA_LEAGUE,         {"season_id": _BZ_COPA_SEASON}),
        (_BZ_LIBERTADORES_LEAGUE, {}),
        (_BZ_SULAMERICANA_LEAGUE, {}),
    ]
    for league_id, extra in leagues:
        params = {"league_id": league_id, "date_from": str(hoje), "date_to": str(amanha), "limit": 50}
        params.update(extra)
        data = _bzzoiro_get("events/", params)
        brutos.extend((data or {}).get("results", []))
    return _filtrar_eventos_data_brt(brutos, hoje)


def _bz_resolve_team(nome_time: str) -> tuple[int, str] | None:
    """Resolve nome digitado para (team_id, nome_oficial) via mapa Bzzoiro."""
    team_map = _bz_build_team_map()
    if not team_map:
        return None
    busca  = nome_time.lower().strip()
    result = team_map.get(busca)
    if not result:
        best, best_score = None, 0
        for norm, val in team_map.items():
            if busca in norm or norm in busca:
                score = len(set(busca) & set(norm))
                if score > best_score:
                    best_score, best = score, val
        result = best
    return result


def _buscar_event_id_por_time(nome_time: str) -> int | None:
    """Retorna o event_id Bzzoiro mais recente (hoje ou últimos 14 dias) para um time."""
    result = _bz_resolve_team(nome_time)
    if not result:
        return None
    team_id, _ = result
    hoje = datetime.now(tz=BRT).date()

    # Hoje primeiro
    data = _bzzoiro_get("events/", {"date_from": str(hoje), "date_to": str(hoje), "limit": 100})
    for ev in (data or {}).get("results", []):
        if ev.get("home_team_id") == team_id or ev.get("away_team_id") == team_id:
            return ev["id"]

    # Últimos 14 dias
    data = _bzzoiro_get("events/", {
        "date_from": str(hoje - timedelta(days=14)),
        "date_to":   str(hoje - timedelta(days=1)),
        "limit": 300,
    })
    matches = [ev for ev in (data or {}).get("results", [])
               if ev.get("home_team_id") == team_id or ev.get("away_team_id") == team_id]
    if matches:
        matches.sort(key=lambda e: e.get("event_date", ""), reverse=True)
        return matches[0]["id"]
    return None


def _build_partida_options(events: list[dict]) -> list[discord.SelectOption]:
    """Constrói opções do Select Menu a partir de eventos Bzzoiro."""
    opts: list[discord.SelectOption] = []
    seen: set[int] = set()
    _emoji = {9: "🇧🇷", 35: "🏆"}
    for ev in events:
        eid = ev.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        home   = ev.get("home_team", "?")
        away   = ev.get("away_team", "?")
        emoji  = _emoji.get(ev.get("league_id", 0), "⚽")
        status = ev.get("status", "notstarted")
        try:
            dt  = datetime.fromisoformat(ev.get("event_date","").replace("Z","+00:00")).astimezone(BRT)
            tme = dt.strftime("%H:%M")
        except Exception:
            tme = ""
        label = f"{emoji} {home} × {away}"[:95] + (f" · {tme}" if tme and tme != "00:00" else "")
        desc  = "🔴 Ao Vivo" if status not in ("notstarted","finished") else ("✅ Encerrado" if status == "finished" else f"🕐 {tme}")
        opts.append(discord.SelectOption(label=label[:100], value=str(eid), description=desc))
    return opts[:25]


class PartidaSelectView(discord.ui.View):
    def __init__(self, opcoes: list[discord.SelectOption], permitir_evento: bool = False):
        super().__init__(timeout=300)
        self.permitir_evento = permitir_evento
        self.selected_jogo: dict | None = None
        self.selected_slug: str = ""
        self._times_seguir: list[str] = []

        sel = discord.ui.Select(
            placeholder="🔍 Ver detalhes de uma partida...",
            options=opcoes,
            row=0,
        )
        sel.callback = self._on_select
        self.add_item(sel)

        self.btn_seguir = discord.ui.Button(
            label="⭐ Seguir", style=discord.ButtonStyle.success,
            disabled=True, row=1,
        )
        self.btn_seguir.callback = self._on_seguir_time
        self.add_item(self.btn_seguir)

        if permitir_evento:
            self.btn_evento = discord.ui.Button(
                label="📅 Criar Evento", style=discord.ButtonStyle.success,
                disabled=True, row=1,
            )
            self.btn_evento.callback = self._on_criar_evento
            self.add_item(self.btn_evento)

    async def _on_select(self, interaction: discord.Interaction):
        eid   = int(interaction.data["values"][0])
        # Loader imediato para o usuário saber que está carregando
        await interaction.response.send_message("⏳ Carregando dados da partida...")
        loop  = asyncio.get_event_loop()
        dados = await loop.run_in_executor(None, buscar_detalhes_partida, eid)
        if not dados:
            await interaction.edit_original_response(content="❌ Detalhes não disponíveis.")
            return
        try:
            html = await _gerar_partida_html(dados)
            url  = _publicar_partida_web(html)
            ev   = dados.get("evento") or {}
            nome_casa = ev.get("home_team", "?")
            nome_fora = ev.get("away_team", "?")
            titulo    = f"{nome_casa} × {nome_fora}"

            partes = [f"📄 **{titulo}**", f"🔗 {url}"]

            status  = (ev.get("status") or "").lower()
            ao_vivo = status not in ("notstarted", "finished", "")
            if ao_vivo and IPTV_URL:
                await interaction.edit_original_response(
                    content=f"⏳ Carregando player de **{titulo}**..."
                )
                player_url = await self._gerar_player_url(loop, ev, eid, nome_casa, nome_fora)
                if player_url:
                    partes.append(f"📺 **Player ao vivo:** {player_url}")

            self._times_seguir = [nome_casa, nome_fora]
            self.btn_seguir.disabled = False

            # Habilita o botão de criar evento para a partida selecionada
            if self.permitir_evento and status != "finished":
                logo_map = await loop.run_in_executor(None, _buscar_logos_brasileiros)
                self.selected_jogo = _bzzoiro_ev_to_fixture(ev, logo_map)
                self.selected_slug = self.selected_jogo.get("_slug", "")
                self.btn_evento.disabled = False
                self.btn_evento.label    = "📅 Criar Evento"
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

            await interaction.edit_original_response(content="\n".join(partes))
        except Exception as e:
            await interaction.edit_original_response(content=f"Erro: {e}")

    async def _on_criar_evento(self, interaction: discord.Interaction):
        if not self.selected_jogo:
            await interaction.response.send_message("Selecione uma partida primeiro.", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("Use este botão em um servidor.", ephemeral=True)
            return

        self.btn_evento.disabled = True
        self.btn_evento.label    = "⏳ Criando..."
        await interaction.response.edit_message(view=self)

        ok, msg = await _criar_evento_voz(interaction.guild, self.selected_jogo, self.selected_slug)

        if ok:
            self.btn_evento.label = "✅ Evento criado"
        else:
            self.btn_evento.disabled = False
            self.btn_evento.label    = "📅 Criar Evento"
        await interaction.message.edit(view=self)
        await interaction.followup.send(msg, ephemeral=True)

    async def _on_seguir_time(self, interaction: discord.Interaction):
        if not self._times_seguir:
            await interaction.response.send_message(
                "Selecione uma partida primeiro.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Qual time seguir?",
            view=SeguirEscolhaTimeView(self._times_seguir),
            ephemeral=True,
        )

    async def _gerar_player_url(self, loop, ev: dict, eid: int,
                                nome_casa: str, nome_fora: str) -> str | None:
        """Cria sessão de player HLS para a partida ao vivo (mesma página do !brasileirao)."""
        slug  = _BZ_ID_TO_SLUG.get(ev.get("league_id") or 0, "")
        canal = await loop.run_in_executor(None, _iptv_buscar_jogo, nome_casa, nome_fora)
        if not canal:
            candidatos = list(_TV_POR_LIGA.get(slug, [])) + ["ESPN FHD", "ESPN", "SporTV"]
            vistos: set[str] = set()
            for nome in candidatos:
                if nome in vistos:
                    continue
                vistos.add(nome)
                canal = await loop.run_in_executor(None, _iptv_buscar_canal, nome)
                if canal:
                    break
        if not canal:
            return None
        token = _criar_sessao(
            canal["url"], f"{nome_casa} × {nome_fora}", str(eid), slug,
            eid, nome_casa, nome_fora,
        )
        return f"{SERVER_URL}/player/{token}"


@bot.command(name="partida")
async def cmd_partida(ctx, *, query: str = ""):
    if not query:
        await ctx.send("Uso: `!partida [time]`\nEx: `!partida Flamengo`")
        return
    msg  = await ctx.send(f"🔍 Buscando partida de **{query}**...")
    loop = asyncio.get_event_loop()
    event_id = await loop.run_in_executor(None, _buscar_event_id_por_time, query.strip())
    if not event_id:
        await msg.edit(content=f"❌ Nenhuma partida encontrada para **{query}** nos últimos 14 dias.")
        return
    dados = await loop.run_in_executor(None, buscar_detalhes_partida, event_id)
    if not dados:
        await msg.edit(content="❌ Detalhes não disponíveis para esta partida.")
        return
    try:
        html   = await _gerar_partida_html(dados)
        url    = _publicar_partida_web(html)
        ev     = dados.get("evento") or {}
        titulo = f"{ev.get('home_team','?')} × {ev.get('away_team','?')}"
        await msg.edit(content=f"📄 **{titulo}**\n🔗 {url}")
    except Exception as e:
        await msg.edit(content=f"Erro ao gerar página: {e}")


@bot.command(name="historico")
async def cmd_historico(ctx, *, query: str = ""):
    if not query:
        await ctx.send("Uso: `!historico [time]`\nEx: `!historico Flamengo`")
        return
    msg  = await ctx.send(f"🔍 Buscando histórico de **{query}**...")
    loop = asyncio.get_event_loop()
    res  = await loop.run_in_executor(None, _buscar_partidas_passadas, query.strip())
    if not res:
        await msg.edit(content=f"❌ Nenhuma partida encerrada encontrada para **{query}** nos últimos 90 dias.")
        return
    events, nome_oficial = res
    opcoes = _build_historico_options(events)
    if not opcoes:
        await msg.edit(content=f"❌ Nenhuma partida disponível para **{nome_oficial}**.")
        return
    view = PartidaSelectView(opcoes)
    await msg.edit(
        content=f"📋 **Histórico de {nome_oficial}** — {len(opcoes)} partidas (últimos 90 dias)\nSelecione uma partida para ver os detalhes:",
        view=view,
    )


# ==========================================
# SLASH COMMANDS — autocomplete nativo do Discord
# ==========================================

_SELECOES_AUTOCOMPLETE = [
    "Brazil", "Argentina", "Uruguay", "Paraguay", "Chile", "Colombia",
    "France", "Germany", "Spain", "England", "Portugal", "Italy",
    "Netherlands", "Belgium", "Croatia", "Japan", "Mexico", "United States",
]

_TIMES_BR_AUTOCOMPLETE = [
    "Flamengo", "Fluminense", "Vasco da Gama", "Botafogo",
    "São Paulo", "Corinthians", "Palmeiras", "Santos",
    "Grêmio", "Internacional", "Atlético Mineiro", "Cruzeiro",
    "Athletico", "Bahia", "Ceará", "Fortaleza", "Sport",
    "Chapecoense", "Cuiabá", "Goiás", "Red Bull Bragantino",
    "América Mineiro", "Coritiba", "Avaí", "Juventude",
    "Mirassol", "Vitória", "Ferroviária", "Novorizontino",
]


async def _liga_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    busca = current.lower()
    return [
        discord.app_commands.Choice(
            name=f"{LIGAS_META.get(k, {}).get('emoji', '🏆')} {LIGAS_META.get(k, {}).get('nome', k.title())}",
            value=k,
        )
        for k in LIGAS
        if not busca or busca in k or busca in LIGAS_META.get(k, {}).get("nome", "").lower()
    ][:25]


async def _liga_sem_copa_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    busca = current.lower()
    return [
        discord.app_commands.Choice(
            name=f"{LIGAS_META.get(k, {}).get('emoji', '🏆')} {LIGAS_META.get(k, {}).get('nome', k.title())}",
            value=k,
        )
        for k in LIGAS
        if k not in LIGAS_COPA
        and (not busca or busca in k or busca in LIGAS_META.get(k, {}).get("nome", "").lower())
    ][:25]


async def _times_seguidos_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[discord.app_commands.Choice[str]]:
    times = _SEGUINDO.get(str(interaction.user.id), {}).get("times", [])
    busca = (current or "").lower()
    return [
        discord.app_commands.Choice(name=t[:100], value=t)
        for t in times
        if not busca or busca in t.lower()
    ][:25]


async def _time_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    busca = current.lower()
    if _bz_team_map_cache:
        nomes = sorted({display for _, display in _bz_team_map_cache.values()})
    else:
        nomes = _TIMES_BR_AUTOCOMPLETE
    nomes = list(dict.fromkeys(_SELECOES_AUTOCOMPLETE + nomes))
    return [
        discord.app_commands.Choice(name=t, value=t)
        for t in nomes
        if not busca or busca in t.lower()
    ][:25]


@bot.tree.command(name="ajuda", description="Lista todos os comandos do bot")
async def slash_ajuda(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_build_ajuda_embed())


@bot.tree.command(name="menu", description="Painel interativo — navegue por botões")
async def slash_menu(interaction: discord.Interaction):
    await interaction.response.send_message(embed=_embed_menu(), view=MenuPrincipalView())


@bot.tree.command(name="seguir", description="Seguir um time (notificações no privado)")
@discord.app_commands.describe(time="Nome do time (opcional — abre menu se vazio)")
@discord.app_commands.autocomplete(time=_time_autocomplete)
async def slash_seguir(interaction: discord.Interaction, time: str = ""):
    if not time.strip():
        view = SeguirTimeSelectView(modo="seguir")
        if view.children:
            await interaction.response.send_message(
                "⭐ Escolha o time:", view=view, ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Informe o time ou use `!menu`.", ephemeral=True,
            )
        return
    msg = await _executar_seguir(interaction.user, time.strip())
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="deixar", description="Deixar de seguir um time")
@discord.app_commands.describe(time="Time (vazio = lista com select)")
@discord.app_commands.autocomplete(time=_times_seguidos_autocomplete)
async def slash_deixar(interaction: discord.Interaction, time: str = ""):
    if not time.strip():
        await _responder_seguindo(interaction.user, interaction=interaction)
        return
    msg = _executar_deixar(interaction.user, time.strip())
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="seguindo", description="Times que você segue")
async def slash_seguindo(interaction: discord.Interaction):
    await _responder_seguindo(interaction.user, interaction=interaction)


@bot.tree.command(name="hoje", description="Jogos de hoje em todas as ligas monitoradas")
async def slash_hoje(interaction: discord.Interaction):
    await _responder_jogos_hoje(interaction=interaction)


@bot.tree.command(name="calendario", description="Calendário de jogos — hoje, amanhã ou outra data")
@discord.app_commands.describe(data="Data dd/mm/aaaa (opcional)")
async def slash_calendario(interaction: discord.Interaction, data: str = None):
    if not data:
        await interaction.response.send_message(
            "📅 **Calendário** — escolha a data:",
            view=CalendarioDataView(),
            ephemeral=True,
        )
        return
    try:
        dt = datetime.strptime(data.strip(), "%d/%m/%Y").date()
    except ValueError:
        await interaction.response.send_message(
            "❌ Data inválida. Use `dd/mm/aaaa`.", ephemeral=True,
        )
        return
    await _responder_calendario_data(dt, interaction=interaction)


@bot.tree.command(name="notificacoes", description="Preferências de alertas do !seguir")
@discord.app_commands.describe(
    tipo="noticias, jogos ou lembrete",
    estado="ligar ou desligar (vazio = ver status)",
)
async def slash_notificacoes(
    interaction: discord.Interaction,
    tipo: str = None,
    estado: str = None,
):
    msg = await _executar_notificacoes(interaction.user, tipo, estado)
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="tabela", description="Classificação de uma liga")
@discord.app_commands.describe(liga="Escolha a liga")
@discord.app_commands.autocomplete(liga=_liga_autocomplete)
async def slash_tabela(interaction: discord.Interaction, liga: str = "brasileirao"):
    chave = liga.lower().replace(" ", "")
    if chave not in LIGAS:
        await interaction.response.send_message("Liga inválida.", ephemeral=True)
        return
    await interaction.response.defer()
    loop = asyncio.get_event_loop()
    meta      = LIGAS_META.get(chave, {"nome": chave.title(), "emoji": "🏆"})
    nome_liga = f"{meta['emoji']} {meta['nome']}"
    if chave in LIGAS_COPA:
        nome_rodada, jogos = await loop.run_in_executor(None, buscar_rodada_copa_brasil)
        if not jogos:
            await interaction.followup.send("Dados da Copa do Brasil indisponíveis.")
            return
        caminho = await gerar_mata_mata_png(jogos, f"{nome_liga} — {nome_rodada}")
        await interaction.followup.send(file=discord.File(caminho))
        return
    dados = await loop.run_in_executor(None, buscar_tabela_por_chave, chave)
    if not dados:
        await interaction.followup.send(f"Tabela indisponível para {nome_liga}.")
        return
    try:
        slug = LIGAS.get(chave, "")
        if dados["type"] == "groups":
            caminho = await gerar_tabela_grupos_png(dados["groups"], nome_liga, slug=slug)
        else:
            caminho = await gerar_tabela_png(dados["teams"], nome_liga, slug=slug)
        await interaction.followup.send(file=discord.File(caminho))
    except Exception as e:
        await interaction.followup.send(f"Erro ao gerar imagem: {e}")


@bot.tree.command(name="artilheiro", description="Top artilheiros de uma liga")
@discord.app_commands.describe(liga="Escolha a liga")
@discord.app_commands.autocomplete(liga=_liga_sem_copa_autocomplete)
async def slash_artilheiro(interaction: discord.Interaction, liga: str = "brasileirao"):
    chave = liga.lower()
    if chave not in LIGAS or chave in LIGAS_COPA:
        await interaction.response.send_message("Liga inválida para artilheiros.", ephemeral=True)
        return
    await interaction.response.defer()
    loop = asyncio.get_event_loop()
    artilheiros = await loop.run_in_executor(None, buscar_artilheiros, LIGAS[chave])
    if not artilheiros:
        await interaction.followup.send("Nenhum artilheiro encontrado.")
        return
    meta = LIGAS_META.get(chave, {"nome": chave.title(), "emoji": "🏆"})
    try:
        caminho = await gerar_artilheiro_png(artilheiros, f"{meta['emoji']} {meta['nome']}")
        await interaction.followup.send(file=discord.File(caminho))
    except Exception as e:
        await interaction.followup.send(f"Erro ao gerar imagem: {e}")


@bot.tree.command(name="chaveamento", description="Bracket mata-mata de uma competição")
@discord.app_commands.describe(liga="Escolha a competição")
@discord.app_commands.autocomplete(liga=_liga_autocomplete)
async def slash_chaveamento(interaction: discord.Interaction, liga: str = "champions"):
    chave = liga.lower().replace(" ", "")
    if chave not in LIGAS:
        await interaction.response.send_message("Liga inválida.", ephemeral=True)
        return
    await interaction.response.defer()
    loop = asyncio.get_event_loop()
    meta      = LIGAS_META.get(chave, {"nome": chave.title(), "emoji": "🏆"})
    nome_liga = f"{meta['emoji']} {meta['nome']}"
    if chave in LIGAS_COPA:
        rounds = await loop.run_in_executor(None, buscar_chaveamento_copa_brasil)
        if not rounds:
            await interaction.followup.send("❌ Sem dados de chaveamento da Copa do Brasil.")
            return
        img = await gerar_chaveamento_png(rounds, "Copa do Brasil 🏆")
        await interaction.followup.send(file=discord.File(img))
        return
    rounds = await loop.run_in_executor(None, buscar_chaveamento, LIGAS[chave])
    if not rounds:
        await interaction.followup.send(f"Chaveamento não disponível para {nome_liga}.")
        return
    try:
        img = await gerar_chaveamento_png(rounds, nome_liga)
        await interaction.followup.send(file=discord.File(img))
    except Exception as e:
        await interaction.followup.send(f"Erro ao gerar imagem: {e}")


@bot.tree.command(name="proximos", description="Próximos jogos de um time")
@discord.app_commands.describe(
    time="Nome do time",
    liga="Liga (opcional — filtra por competição)",
    quantidade="Quantos jogos listar (padrão: 5, máx.: 25)",
)
@discord.app_commands.autocomplete(liga=_liga_autocomplete, time=_time_autocomplete)
async def slash_proximos(
    interaction: discord.Interaction,
    time: str,
    liga: str = "",
    quantidade: int = PROXIMOS_PADRAO,
):
    await interaction.response.defer()
    loop     = asyncio.get_event_loop()
    limite   = _normalizar_limite_proximos(quantidade)
    liga_key = liga.lower() if liga.lower() in LIGAS else None
    if liga_key:
        meta      = LIGAS_META.get(liga_key, {"nome": liga_key.title(), "emoji": "🏆"})
        nome_liga = f"{meta['emoji']} {meta['nome']}"
    else:
        nome_liga = ""
    async def _enviar(jogos: list, nome_oficial: str, nl: str):
        try:
            caminho = await gerar_proximos_png(jogos, nome_oficial, nl)
        except Exception as e:
            await interaction.followup.send(f"Erro ao gerar imagem: {e}")
            return
        await interaction.followup.send(file=discord.File(caminho), view=_view_proximos(jogos))

    eh_selecao = bool(_canonical_selecao(time)) or (liga_key in _LIGAS_SELECAO)
    if eh_selecao:
        selecao_result = await loop.run_in_executor(
            None, functools.partial(buscar_proximos_selecao, time, liga_key, limite=limite),
        )
        if selecao_result:
            jogos, nome_oficial = selecao_result
            await _enviar(jogos, nome_oficial, nome_liga or "🌍 Seleções")
            return
        await interaction.followup.send(f"Nenhum jogo futuro encontrado para a seleção **{time}**.")
        return

    bz_league_filter = _BZ_LIGA_TO_ID.get(liga_key) if liga_key else None
    bz_result = await loop.run_in_executor(
        None, functools.partial(buscar_proximos_bzzoiro, time, bz_league_filter, limite=limite),
    )
    if bz_result:
        jogos, nome_oficial = bz_result
        await _enviar(jogos, nome_oficial, nome_liga)
        return
    if not liga_key or liga_key in LIGAS_COPA or not LIGAS.get(liga_key):
        selecao_result = await loop.run_in_executor(
            None, functools.partial(buscar_proximos_selecao, time, None, limite=limite),
        )
        if selecao_result:
            jogos, nome_oficial = selecao_result
            await _enviar(jogos, nome_oficial, "🌍 Seleções")
            return
        await interaction.followup.send(f"Nenhum jogo futuro encontrado para **{time}**.")
        return
    resultado = await loop.run_in_executor(None, buscar_time_id, LIGAS[liga_key], time)
    if not resultado:
        await interaction.followup.send(f"Time `{time}` não encontrado.")
        return
    team_id, nome_oficial = resultado
    jogos = await loop.run_in_executor(
        None,
        functools.partial(buscar_proximos_espn_clube, LIGAS[liga_key], team_id, limite=limite),
    )
    if not jogos:
        await interaction.followup.send(f"Nenhum jogo futuro encontrado para **{nome_oficial}**.")
        return
    await _enviar(jogos, nome_oficial, nome_liga)


@bot.tree.command(name="partida", description="Detalhes da partida mais recente de um time")
@discord.app_commands.describe(time="Nome do time (ex: Flamengo)")
@discord.app_commands.autocomplete(time=_time_autocomplete)
async def slash_partida(interaction: discord.Interaction, time: str):
    await interaction.response.defer()
    loop     = asyncio.get_event_loop()
    event_id = await loop.run_in_executor(None, _buscar_event_id_por_time, time.strip())
    if not event_id:
        await interaction.followup.send(f"❌ Nenhuma partida encontrada para **{time}** nos últimos 14 dias.")
        return
    dados = await loop.run_in_executor(None, buscar_detalhes_partida, event_id)
    if not dados:
        await interaction.followup.send("❌ Detalhes não disponíveis.")
        return
    try:
        html   = await _gerar_partida_html(dados)
        url    = _publicar_partida_web(html)
        ev     = dados.get("evento") or {}
        titulo = f"{ev.get('home_team','?')} × {ev.get('away_team','?')}"
        await interaction.followup.send(f"📄 **{titulo}**\n🔗 {url}")
    except Exception as e:
        await interaction.followup.send(f"Erro: {e}")


@bot.tree.command(name="historico", description="Ver partidas passadas de um time e seus detalhes")
@discord.app_commands.describe(time="Nome do time (ex: Flamengo)")
@discord.app_commands.autocomplete(time=_time_autocomplete)
async def slash_historico(interaction: discord.Interaction, time: str):
    await interaction.response.defer()
    loop = asyncio.get_event_loop()
    res  = await loop.run_in_executor(None, _buscar_partidas_passadas, time.strip())
    if not res:
        await interaction.followup.send(f"❌ Nenhuma partida encerrada encontrada para **{time}** nos últimos 90 dias.")
        return
    events, nome_oficial = res
    opcoes = _build_historico_options(events)
    if not opcoes:
        await interaction.followup.send(f"❌ Nenhuma partida disponível para **{nome_oficial}**.")
        return
    view = PartidaSelectView(opcoes)
    await interaction.followup.send(
        content=f"📋 **Histórico de {nome_oficial}** — {len(opcoes)} partidas (últimos 90 dias)\nSelecione uma partida para ver os detalhes:",
        view=view,
    )


if not TOKEN_DO_DISCORD:
    raise SystemExit("ERRO: TOKEN_DISCORD não encontrado. Verifique o nome exato da variável no Railway.")

bot.run(TOKEN_DO_DISCORD)
