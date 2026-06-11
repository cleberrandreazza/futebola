# Roadmap: bot multi-guild (convidar e self-service)

Documento de referência para transformar o **Futebola** de bot single-tenant (um servidor, `.env` fixo) em bot disponível para **qualquer Discord**, onde o admin só convida o bot e ele se configura sozinho.

**Estado atual (jun/2026):** uma instância Railway, Convex compartilhado, IDs de canal/role/guild no `.env`.

---

## Diagnóstico — o que está amarrado hoje

### Configuração via `.env` (servidor único)

| Variável | Uso no bot |
|---|---|
| `CANAL_JOGOS_DO_DIA` | Task `resumo_diario` — posta imagem às 09:00 BRT |
| `CANAL_COMANDOS` | Painel fixo com botões (`menu_canal.json`) |
| `CANAL_EVENTO` | Canal de voz para eventos agendados (`_criar_evento_voz`) |
| `DISCORD_GUILD_ID` | Sync de slash commands só nesse guild |
| `BOLEIRO_ROLE_ID` / `ROLE_BOLEIRO` | Permissão para `/saldo`, `/apostar`, etc. |

Defaults hardcoded em `bot.py` (ex.: `CANAL_EVENTO` fallback `1510344579927249017`, `BOLEIRO_ROLE_ID` fallback `1510359568058679386`).

### Conteúdo hardcoded no código

- `LIGAS` — mapa chave → slug ESPN / Bzzoiro
- `LIGAS_RESUMO` — ligas do `/hoje` e resumo diário
- `LIGAS_APOSTAS` — mesmo escopo do `/hoje` para `/apostar`
- IDs Bzzoiro (`_BZ_COPA_LEAGUE`, `_BZ_LIGA_TO_ID`, etc.)
- Fuso `BRT`, `HORARIO_RESUMO`, regras de apostas (`CREDITO_*`, `APOSTA_*`)

### Dados globais (sem `guildId`)

**Convex (`convex/schema.ts`):**

- `seguidores` — chave só `userId`
- `apostadores` / `apostas` — chave só `userId`

**Efeito:** `/rank-apostas` mistura todos os servidores; saldo é global por usuário Discord.

**Arquivos locais (fallback / efêmeros no Railway):**

- `menu_canal.json` — um par channel/message
- `seguindo.json`, `apostas.json`

### Infraestrutura compartilhada (ok manter global)

- `TOKEN_DISCORD`, `CONVEX_URL`, `BOT_SHARED_SECRET`
- `BZZOIRO_TOKEN`, APIs ESPN
- `SERVER_URL` — player web + proxy IPTV
- `IPTV_*` — credencial única; **não** expor a servidores de terceiros sem modelo premium

### O que já funciona parcialmente multi-guild

- Slash commands interativos (`/hoje`, `/tabela`, …) em qualquer canal
- DMs para seguir time / notícias / lembretes
- `PLAYERS_ATIVOS` indexado por `guild_id`
- Eventos de voz usam `interaction.guild`, mas o **canal de voz** ainda é global (`CANAL_EVENTO_ID`)

---

## Objetivo final

1. Admin convida o bot (scope `applications.commands`).
2. Bot entra → onboarding automático ou `/setup`.
3. Cada servidor define canais, role de apostas, ligas (opcional), fuso.
4. Sem editar `.env` por cliente.
5. Dados de apostas/ranking **isolados por servidor** (recomendado).

---

## Fase 1 — Multi-guild mínimo (prioridade)

**Meta:** bot funcional em N servidores com config por guild.

### 1.1 Schema Convex — tabela `guilds`

```typescript
// convex/schema.ts
guilds: defineTable({
  guildId: v.string(),
  resumoChannelId: v.optional(v.string()),
  comandosChannelId: v.optional(v.string()),
  eventoVoiceChannelId: v.optional(v.string()),
  boleiroRoleId: v.optional(v.string()),
  timezone: v.string(),              // default "America/Sao_Paulo"
  ligasResumo: v.array(v.string()),  // default = LIGAS_RESUMO atual
  apostasEnabled: v.boolean(),
  setupComplete: v.boolean(),
  menuMessageId: v.optional(v.string()),
  criadoEm: v.number(),
  atualizadoEm: v.number(),
}).index("by_guild", ["guildId"]),
```

**Arquivo sugerido:** `convex/guilds.ts` — `get`, `upsert`, `listConfigured` (guilds com resumo ativo).

### 1.2 Helpers no `bot.py`

- `_guild_config(guild_id) -> dict` — cache em memória + Convex
- Substituir leituras de `CANAL_RESUMO_ID`, `CANAL_COMANDOS_ID`, `CANAL_EVENTO_ID`, `BOLEIRO_ROLE_ID` por config do guild
- Manter `.env` como **fallback** para o servidor legado durante migração

### 1.3 Comando `/setup` (admin only)

Fluxo sugerido:

1. Verificar `interaction.user.guild_permissions.administrator`
2. Select de canal para resumo diário
3. Select de canal para painel de comandos (opcional)
4. Select de canal de voz para eventos (opcional)
5. Select de role “Boleiros” ou criar role automaticamente
6. Botão “Usar padrão” — ligas e horário default
7. Salvar em Convex → `setupComplete: true`
8. Publicar menu no canal escolhido

### 1.4 Eventos Discord

- `on_guild_join` — criar registro `guilds` vazio; DM ao owner ou mensagem em canal com instrução `/setup`
- `on_guild_remove` — opcional: marcar guild inativo (não apagar dados de apostas)

### 1.5 Slash sync global

- Remover dependência de `DISCORD_GUILD_ID` para sync
- Em `_startup_pos_ready`: `await bot.tree.sync()` **global** (pode levar até ~1h na primeira vez no Discord)
- Remover `copy_global_to(guild=...)` exceto se necessário para dev rápido

### 1.6 Tasks agendadas por guild

**`resumo_diario`:**

- Loop: `for guild in list_guilds_with_resumo_channel()`
- Para cada guild: buscar jogos (`ligasResumo` da config), postar no canal dela
- Respeitar `timezone` do guild (fase 1 pode manter BRT para todos)

**`_publicar_menu_canal`:**

- Renomear para `_publicar_menu_guild(guild_id)` ou loop no startup
- Persistir `menuMessageId` em Convex, não só `menu_canal.json`

### 1.7 Migração do servidor atual

Script one-shot ou mutation:

- Ler IDs do `.env` atuais
- Inserir documento `guilds` para `DISCORD_GUILD_ID` com canais/role existentes
- `setupComplete: true`

---

## Fase 2 — Apostas e ranking por servidor

**Decisão:** ranking **por guild** (recomendado para produto público).

### 2.1 Schema

```typescript
apostadores: defineTable({
  guildId: v.string(),
  userId: v.string(),
  // ... campos atuais
}).index("by_guild_and_user", ["guildId", "userId"]),

apostas: defineTable({
  guildId: v.string(),
  userId: v.string(),
  // ... campos atuais
}).index("by_guild", ["guildId"]),
```

### 2.2 API Convex

- Todas as mutations/queries de `apostas.ts` recebem `guildId`
- `getRanking` filtra por `guildId`
- `creditoSemanal` itera apostadores **por guild** ou global com `guildId` no loop

### 2.3 Bot

- Passar `str(interaction.guild_id)` em `_apostas_ensure`, `_apostas_place`, `_apostas_ranking`, etc.
- `_pode_apostar` usa `boleiroRoleId` da config do guild
- Desabilitar apostas se `apostasEnabled: false` no guild

### 2.4 Migração de dados

- Apostadores existentes sem `guildId` → associar ao `DISCORD_GUILD_ID` legado ou descartar

---

## Fase 3 — Defaults “zero config”

- Botão **“Configuração rápida”** no `/setup`:
  - Criar canal `#futebol-bot` (permissão bot)
  - Criar role `@Boleiros`
  - Publicar menu
  - Ativar resumo 09:00
- `/config` para alterar depois (subcomandos: `canais`, `ligas`, `apostas`, `timezone`)
- Ligas: default global; admin escolhe subset

---

## Fase 4 — Produto público (opcional)

- [ ] Bot verificado no Discord Developer Portal
- [ ] Política de privacidade + termos (dados Convex, DMs)
- [ ] Rate limit por guild (`@convex-dev/ratelimiter` ou similar)
- [ ] IPTV/player: **premium** ou desligado fora do servidor owner
- [ ] Monitoramento / Sentry por guild
- [ ] Documentação de invite link com scopes corretos

**Invite URL mínima:**

```
https://discord.com/api/oauth2/authorize?client_id=APP_ID&permissions=...&scope=bot%20applications.commands
```

Permissões úteis: `Manage Events`, `Send Messages`, `Embed Links`, `Attach Files`, `Use External Emojis`, `Manage Roles` (se criar Boleiros auto).

---

## Checklist de arquivos a tocar

| Arquivo | Mudança |
|---|---|
| `convex/schema.ts` | Tabela `guilds`; `guildId` em apostas |
| `convex/guilds.ts` | **Novo** — CRUD config |
| `convex/apostas.ts` | Scope por `guildId` |
| `bot.py` | `_guild_config`, `/setup`, `/config`, tasks multi-guild, `on_guild_join` |
| `scripts/test_apostas.py` | Passar `guildId` nos testes |
| `.env` | Manter só secrets globais; deprecar IDs de canal |
| `menu_canal.json` | Migrar para Convex |

---

## Ordem de implementação sugerida (PRs)

1. **PR1:** Schema `guilds` + queries + migração servidor legado
2. **PR2:** `_guild_config` + fallback `.env` + `/setup` básico
3. **PR3:** Resumo diário + menu por guild
4. **PR4:** Eventos de voz + role apostas por guild
5. **PR5:** Apostas/ranking scoped por `guildId` + migração
6. **PR6:** Setup rápido + `/config` + sync global
7. **PR7:** Hardening (rate limit, docs, invite público)

---

## Riscos e notas

- **Sync global** de slash commands pode demorar; em dev usar guild sync temporário.
- **Convex `collect()`** em muitos guilds no resumo diário — considerar paginação ou índice `setupComplete`.
- **Memória:** `JOGOS_MONITORADOS`, `_player_sessions` — ok em uma instância; multi-instância Railway exigiria Redis ou stateless.
- **Testes:** nunca rodar `test_apostas.py` com `CONVEX_INTEGRATION=1` em prod sem teardown (já corrigido com `purgeTestApostadores`).

---

## Referências no código atual

- Config env: `bot.py` linhas ~28–86
- Ligas: `bot.py` `LIGAS`, `LIGAS_RESUMO`, `LIGAS_APOSTAS`
- Startup/sync: `_startup_pos_ready`
- Resumo diário: task `resumo_diario`
- Menu fixo: `_publicar_menu_canal`, `menu_canal.json`
- Apostas Convex: `convex/apostas.ts`
- Permissão apostas: `_pode_apostar`

---

*Criado em jun/2026 — aplicar quando for priorizar multi-guild.*
