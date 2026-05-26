import os
import discord
from discord.ext import commands, tasks
import requests
from datetime import datetime
from playwright.async_api import async_playwright

# ==========================================
# CONFIGURAÇÕES INICIAIS (PREENCHA AQUI)
# ==========================================
TOKEN_DO_DISCORD = "SEU_TOKEN_DO_DISCORD_AQUI"
API_KEY_DA_RAPIDAPI = "SUA_API_KEY_DA_RAPIDAPI_AQUI"

URL_BASE = "https://v3.football.api-sports.io"
HEADERS = {
    'x-rapidapi-host': "v3.football.api-sports.io",
    'x-rapidapi-key': API_KEY_DA_RAPIDAPI
}

# Dicionário com os IDs das ligas na API-Football
LIGAS = {
    "brasileirao": 71,
    "premierleague": 39,
    "champions": 2,
    "copadobrasil": 73,
    "sulamericana": 11
}

# Banco de dados na memória para rastrear jogos ao vivo
# Estrutura: { id_do_jogo: { "canal_id": 123, "eventos_processados": [], "status": "1H" } }
JOGOS_MONITORADOS = {}

# ==========================================
# CONFIGURAÇÃO DO BOT DO DISCORD
# ==========================================
class FootballBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def setup_hook(self):
        # Inicia os loops em segundo plano assim que o bot liga
        self.checar_jogos_ao_vivo.start()
        self.enviar_jogos_diarios.start()

intents = discord.Intents.default()
intents.message_content = True
bot = FootballBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'🤖 Bot conectado com sucesso como {bot.user.name}!')

# ==========================================
# MOTOR DE RENDERIZAÇÃO DE IMAGENS (PLAYWRIGHT)
# ==========================================
async def gerar_print_tabela(dados_tabela, nome_liga):
    """Gera o HTML da tabela estilo Google e tira um screenshot"""
    linhas_html = ""
    for time in dados_tabela:
        linhas_html += f"""
        <tr>
            <td class="pos">{time['rank']}</td>
            <td class="time">
                <img src="{time['team']['logo']}" width="20" height="20"> {time['team']['name']}
            </td>
            <td>{time['all']['played']}</td>
            <td>{time['all']['win']}</td>
            <td>{time['all']['draw']}</td>
            <td>{time['all']['lose']}</td>
            <td>{time['goalsDiff']}</td>
            <td class="pontos">{time['points']}</td>
        </tr>
        """

    html_conteudo = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                background-color: #202124;
                color: #e8eaed;
                font-family: Roboto, Helvetica, Arial, sans-serif;
                margin: 0; padding: 20px;
                width: 600px;
            }}
            .titulo {{ font-size: 20px; margin-bottom: 15px; font-weight: 400; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
            th {{ text-align: left; color: #9aa0a6; font-weight: normal; padding: 8px; border-bottom: 1px solid #3c4043; }}
            td {{ padding: 10px 8px; border-bottom: 1px solid #3c4043; vertical-align: middle; }}
            .pos {{ color: #9aa0a6; width: 30px; text-align: center; }}
            .time {{ display: flex; align-items: center; gap: 10px; font-weight: 500; }}
            .pontos {{ font-weight: bold; color: #fff; }}
            tr:hover {{ background-color: #303134; }}
        </style>
    </head>
    <body>
        <div class="titulo">Classificação: {nome_liga}</div>
        <table>
            <thead>
                <tr>
                    <th class="pos">#</th>
                    <th>Clube</th>
                    <th>PJ</th>
                    <th>V</th>
                    <th>E</th>
                    <th>D</th>
                    <th>SG</th>
                    <th>PTS</th>
                </tr>
            </thead>
            <tbody>
                {linhas_html}
            </tbody>
        </table>
    </body>
    </html>
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_viewport_size({"width": 640, "height": 1000})
        await page.set_content(html_conteudo)
        await page.wait_for_timeout(1000)  # Espera carregar escudos
        
        elemento_tabela = await page.query_selector("body")
        caminho_imagem = f"tabela_{nome_liga.lower().replace(' ', '')}.png"
        await elemento_tabela.screenshot(path=caminho_imagem)
        await browser.close()
        return caminho_imagem

async def gerar_print_jogos(jogos, titulo):
    """Gera o HTML com a lista de jogos do dia e tira um screenshot"""
    linhas_html = ""
    for jogo in jogos:
        status = jogo['fixture']['status']['short']
        tempo = jogo['fixture']['status']['elapsed']
        horario = datetime.fromisoformat(jogo['fixture']['date']).strftime("%H:%M")
        
        # Define o texto do placar ou horário
        if status in ["NS", "TBD"]:
            placar = f"<span class='hora'>{horario}</span>"
        elif status in ["FT", "AET", "PEN"]:
            placar = f"<span class='placar-final'>{jogo['goals']['home']} - {jogo['goals']['away']}</span>"
        else:
            placar = f"<span class='placar-vivo'>{jogo['goals']['home']} - {jogo['goals']['away']}</span><br><small class='tempo'>{tempo}'</small>"

        linhas_html += f"""
        <div class="jogo">
            <div class="time time-home">
                <span>{jogo['teams']['home']['name']}</span>
                <img src="{jogo['teams']['home']['logo']}" width="24" height="24">
            </div>
            <div class="status">
                {placar}
            </div>
            <div class="time time-away">
                <img src="{jogo['teams']['away']['logo']}" width="24" height="24">
                <span>{jogo['teams']['away']['name']}</span>
            </div>
            <div class="id-jogo">ID: {jogo['fixture']['id']}</div>
        </div>
        """

    html_conteudo = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                background-color: #202124; color: #e8eaed;
                font-family: Roboto, Arial, sans-serif; width: 550px; padding: 20px; margin: 0;
            }}
            .titulo {{ font-size: 18px; margin-bottom: 15px; color: #fff; border-bottom: 1px solid #3c4043; padding-bottom: 10px; }}
            .jogo {{
                display: flex; align-items: center; justify-content: space-between;
                background: #303134; margin-bottom: 8px; padding: 12px; border-radius: 8px; position: relative;
            }}
            .time {{ display: flex; align-items: center; gap: 10px; width: 40%; }}
            .time-home {{ justify-content: flex-end; text-align: right; }}
            .time-away {{ justify-content: flex-start; text-align: left; }}
            .status {{ width: 20%; text-align: center; font-weight: bold; font-size: 16px; }}
            .hora {{ color: #8ab4f8; font-weight: normal; font-size: 14px; }}
            .placar-vivo {{ color: #f28b82; }}
            .tempo {{ color: #f28b82; font-size: 11px; }}
            .id-jogo {{ position: absolute; bottom: 2px; right: 8px; font-size: 9px; color: #9aa0a6; }}
        </style>
    </head>
    <body>
        <div class="titulo">{titulo}</div>
        {linhas_html if linhas_html else '<div style="color:#9aa0a6;">Nenhum jogo agendado para hoje.</div>'}
    </body>
    </html>
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_viewport_size({"width": 580, "height": 1200})
        await page.set_content(html_conteudo)
        await page.wait_for_timeout(1000)
        
        elemento = await page.query_selector("body")
        caminho_imagem = "jogos_temp.png"
        await elemento.screenshot(path=caminho_imagem)
        await browser.close()
        return caminho_imagem

# ==========================================
# FUNÇÕES DE CONSUMO DA API DE FUTEBOL
# ==========================================
def buscar_tabela_api(id_liga: int):
    url = f"{URL_BASE}/standings"
    querystring = {"league": str(id_liga), "season": str(datetime.now().year)}
    resposta = requests.get(url, headers=HEADERS, params=querystring)
    if resposta.status_code == 200:
        try:
            return resposta.json()['response'][0]['league']['standings'][0]
        except (IndexError, KeyError):
            return None
    return None

def buscar_jogos_do_dia_api(id_liga: int):
    url = f"{URL_BASE}/fixtures"
    data_hoje = datetime.now().strftime("%Y-%m-%d")
    querystring = {"league": str(id_liga), "date": data_hoje}
    resposta = requests.get(url, headers=HEADERS, params=querystring)
    if resposta.status_code == 200:
        return resposta.json().get('response', [])
    return []

# ==========================================
# TAREFAS AGENDADAS E EM SEGUNDO PLANO
# ==========================================
@tasks.loop(hours=24)
async def enviar_jogos_diarios():
    """Roda uma vez por dia enviando o consolidado de todas as ligas (Ex: Todo dia às 08h)"""
    # Para fins de teste, você pode disparar manualmente ou criar canais específicos
    pass

@tasks.loop(seconds=30)
async def checar_jogos_ao_vivo():
    """Varre jogos salvos na memória procurando por gols e cartões na API"""
    if not JOGOS_MONITORADOS:
        return

    for id_jogo, dados_jogo in list(JOGOS_MONITORADOS.items()):
        canal = bot.get_channel(dados_jogo["canal_id"])
        if not canal:
            continue

        url = f"{URL_BASE}/fixtures"
        try:
            resposta = requests.get(url, headers=HEADERS, params={"id": str(id_jogo)})
            if resposta.status_code != 200 or not resposta.json().get('response'):
                continue
                
            partida = resposta.json()['response'][0]
            status_atual = partida['fixture']['status']['short']
            eventos = partida.get('events', [])
            
            for evento in eventos:
                id_evento = f"{evento['time']['elapsed']}_{evento['type']}_{evento['team']['id']}_{evento['player']['id']}"
                
                if id_evento not in dados_jogo["eventos_processados"]:
                    dados_jogo["eventos_processados"].append(id_evento)
                    
                    tempo = evento['time']['elapsed']
                    time_nome = evento['team']['name']
                    jogador = evento['player']['name']
                    tipo = evento['type']

                    if tipo == "Goal":
                        detalhe = evento.get('detail', 'Normal')
                        emoji = "⚽" if detalhe != "Own Goal" else "❌ (Contra)"
                        
                        gols_casa = partida['goals']['home']
                        gols_fora = partida['goals']['away']
                        time_casa = partida['teams']['home']['name']
                        time_fora = partida['teams']['away']['name']
                        
                        msg = f"{emoji} **GOL!** aos {tempo}' - **{jogador}** marca para o **{time_nome}**!\n" \
                              f"📊 **Placar Atual:** {time_casa} {gols_casa} x {gols_fora} {time_fora}"
                        await canal.send(msg)

                    elif tipo == "Card":
                        cor_cartao = "🟨" if "Yellow" in evento['detail'] else "🟥"
                        msg = f"{cor_cartao} **Cartão!** aos {tempo}' para **{jogador}** ({time_nome})."
                        await canal.send(msg)

            if status_atual in ["FT", "AET", "PEN"]:
                gols_casa = partida['goals']['home']
                gols_fora = partida['goals']['away']
                await canal.send(f"🏁 **FIM DE JOGO!**\nResultado final: **{partida['teams']['home']['name']} {gols_casa} x {gols_fora} {partida['teams']['away']['name']}**")
                del JOGOS_MONITORADOS[id_jogo]

        except Exception as e:
            print(f"Erro no monitoramento do jogo {id_jogo}: {e}")

# ==========================================
# COMANDOS DO BOT
# ==========================================
@bot.command(name='tabela_brasileirao')
async def tabela_brasileirao(ctx):
    """Atalho direto para a tabela do brasileirão"""
    await ctx.invoke(bot.get_command('tabela'), nome_liga="brasileirao")

@bot.command(name='tabela')
async def tabela(ctx, *, nome_liga: str):
    """Gera o print estilo Google com todos os dados da liga selecionada"""
    liga_formatada = nome_liga.lower().replace(" ", "")
    if liga_formatada not in LIGAS:
        await ctx.send(f"❌ Liga inválida. Escolha entre: {', '.join(LIGAS.keys())}")
        return

    await ctx.send(f"📊 Buscando dados e gerando imagem da tabela do **{nome_liga.title()}**...")
    dados = buscar_tabela_api(LIGAS[liga_formatada])
    
    if not dados:
        await ctx.send("❌ Não foi possível carregar a tabela da API.")
        return

    caminho_img = await gerar_print_tabela(dados, nome_liga.title())
    await ctx.send(file=discord.File(caminho_img))

@bot.command(name='liga')
async def liga(ctx, *, nome_liga: str):
    """Mostra os jogos do dia da liga selecionada com imagem contendo os IDs das partidas"""
    liga_formatada = nome_liga.lower().replace(" ", "")
    if liga_formatada not in LIGAS:
        await ctx.send(f"❌ Liga inválida. Escolha entre: {', '.join(LIGAS.keys())}")
        return

    await ctx.send(f"📅 Buscando jogos de hoje para **{nome_liga.title()}**...")
    jogos = buscar_jogos_do_dia_api(LIGAS[liga_formatada])
    
    caminho_img = await gerar_print_jogos(jogos, f"Jogos de Hoje: {nome_liga.title()}")
    await ctx.send(file=discord.File(caminho_img))

@bot.command(name='seguir')
async def seguir(ctx, id_jogo: int):
    """Ativa alertas de gols, cartões, fim de jogo e exibe onde assistir"""
    if id_jogo in JOGOS_MONITORADOS:
        await ctx.send("📌 Este jogo já está sendo monitorado por aqui.")
        return

    resposta = requests.get(f"{URL_BASE}/fixtures", headers=HEADERS, params={"id": str(id_jogo)})
    if resposta.status_code == 200 and resposta.json().get('response'):
        partida = resposta.json()['response'][0]
        time_casa = partida['teams']['home']['name']
        time_fora = partida['teams']['away']['name']
        
        # Procura transmissões se disponíveis na API ou define padrão informativo
        onde_assistir = "📺 Transmissão: Verifique na ESPN, Sportv, TNT/Max ou Premiere."

        JOGOS_MONITORADOS[id_jogo] = {
            "canal_id": ctx.channel.id,
            "eventos_processados": [],
            "status": partida['fixture']['status']['short']
        }

        # Evita alarmar retroativamente eventos antigos
        for evento in partida.get('events', []):
            id_evento = f"{evento['time']['elapsed']}_{evento['type']}_{evento['team']['id']}_{evento['player']['id']}"
            JOGOS_MONITORADOS[id_jogo]["eventos_processados"].append(id_evento)

        mensagem_sucesso = f"""
✅ **Monitoramento Ativado!**
⚽ **{time_casa} x {time_fora}**
{onde_assistir}
*Avisarei neste canal quando houver gols, cartões e ao fim da partida!*
"""
        await ctx.send(mensagem_sucesso)
    else:
        await ctx.send("❌ ID do jogo inválido ou não encontrado na API de futebol.")

# Inicialização do Bot
bot.run(TOKEN_DO_DISCORD)