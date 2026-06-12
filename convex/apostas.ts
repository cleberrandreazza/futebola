import { query, mutation } from "./_generated/server";
import type { MutationCtx, QueryCtx } from "./_generated/server";
import { v } from "convex/values";

const palpiteValidator = v.union(
  v.literal("1"),
  v.literal("X"),
  v.literal("2")
);

const statusValidator = v.union(
  v.literal("aberta"),
  v.literal("ganhou"),
  v.literal("perdeu"),
  v.literal("cancelada")
);

const apostadorValidator = v.object({
  userId: v.string(),
  displayName: v.string(),
  saldo: v.number(),
  totalApostado: v.number(),
  totalGanho: v.number(),
  apostasGanhas: v.number(),
  apostasPerdidas: v.number(),
  ultimoCreditoSemanal: v.optional(v.string()),
  criadoEm: v.number(),
});

const apostaValidator = v.object({
  _id: v.id("apostas"),
  userId: v.string(),
  eventId: v.string(),
  matchKey: v.optional(v.string()),
  home: v.string(),
  away: v.string(),
  palpite: palpiteValidator,
  odd: v.number(),
  valor: v.number(),
  retornoPotencial: v.number(),
  status: statusValidator,
  criadaEm: v.number(),
  liquidadaEm: v.optional(v.number()),
});

const placeBetResultValidator = v.object({
  ok: v.boolean(),
  error: v.optional(v.string()),
  apostaId: v.optional(v.id("apostas")),
  novoSaldo: v.optional(v.number()),
});

const settleResultValidator = v.object({
  ok: v.boolean(),
  error: v.optional(v.string()),
});

function checkSecret(secret?: string): void {
  const expected = process.env.BOT_SHARED_SECRET;
  if (expected && secret !== expected) {
    throw new Error("Unauthorized: segredo inválido");
  }
}

/** IDs numéricos de snowflake do Discord (17–20 dígitos). */
function isDiscordUserId(userId: string): boolean {
  return /^\d{17,20}$/.test(userId);
}

async function getApostadorDoc(
  ctx: QueryCtx | MutationCtx,
  userId: string
) {
  return await ctx.db
    .query("apostadores")
    .withIndex("by_user", (q) => q.eq("userId", userId))
    .unique();
}

export const getSaldo = query({
  args: {
    secret: v.optional(v.string()),
    userId: v.string(),
  },
  returns: v.union(apostadorValidator, v.null()),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const doc = await getApostadorDoc(ctx, args.userId);
    if (!doc) {
      return null;
    }
    return {
      userId: doc.userId,
      displayName: doc.displayName,
      saldo: doc.saldo,
      totalApostado: doc.totalApostado,
      totalGanho: doc.totalGanho,
      apostasGanhas: doc.apostasGanhas,
      apostasPerdidas: doc.apostasPerdidas,
      ultimoCreditoSemanal: doc.ultimoCreditoSemanal,
      criadoEm: doc.criadoEm,
    };
  },
});

export const ensureApostador = mutation({
  args: {
    secret: v.optional(v.string()),
    userId: v.string(),
    displayName: v.string(),
    creditoInicial: v.number(),
  },
  returns: apostadorValidator,
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const existing = await getApostadorDoc(ctx, args.userId);
    if (existing) {
      if (existing.displayName !== args.displayName) {
        await ctx.db.patch(existing._id, { displayName: args.displayName });
      }
      return {
        userId: existing.userId,
        displayName: args.displayName,
        saldo: existing.saldo,
        totalApostado: existing.totalApostado,
        totalGanho: existing.totalGanho,
        apostasGanhas: existing.apostasGanhas,
        apostasPerdidas: existing.apostasPerdidas,
        ultimoCreditoSemanal: existing.ultimoCreditoSemanal,
        criadoEm: existing.criadoEm,
      };
    }
    const now = Date.now();
    await ctx.db.insert("apostadores", {
      userId: args.userId,
      displayName: args.displayName,
      saldo: args.creditoInicial,
      totalApostado: 0,
      totalGanho: 0,
      apostasGanhas: 0,
      apostasPerdidas: 0,
      criadoEm: now,
    });
    return {
      userId: args.userId,
      displayName: args.displayName,
      saldo: args.creditoInicial,
      totalApostado: 0,
      totalGanho: 0,
      apostasGanhas: 0,
      apostasPerdidas: 0,
      ultimoCreditoSemanal: undefined,
      criadoEm: now,
    };
  },
});

export const placeBet = mutation({
  args: {
    secret: v.optional(v.string()),
    userId: v.string(),
    displayName: v.string(),
    eventId: v.string(),
    matchKey: v.string(),
    home: v.string(),
    away: v.string(),
    palpite: palpiteValidator,
    valor: v.number(),
    odd: v.number(),
    apostaMinima: v.number(),
    creditoInicial: v.number(),
  },
  returns: placeBetResultValidator,
  handler: async (ctx, args) => {
    checkSecret(args.secret);

    if (args.valor < args.apostaMinima) {
      return {
        ok: false,
        error: `Aposta mínima: ${args.apostaMinima} créditos.`,
      };
    }
    if (!Number.isFinite(args.valor) || args.valor <= 0) {
      return { ok: false, error: "Valor inválido." };
    }

    let apostador = await getApostadorDoc(ctx, args.userId);
    if (!apostador) {
      const now = Date.now();
      const id = await ctx.db.insert("apostadores", {
        userId: args.userId,
        displayName: args.displayName,
        saldo: args.creditoInicial,
        totalApostado: 0,
        totalGanho: 0,
        apostasGanhas: 0,
        apostasPerdidas: 0,
        criadoEm: now,
      });
      apostador = await ctx.db.get(id);
      if (!apostador) {
        return { ok: false, error: "Erro ao criar conta de apostador." };
      }
    } else if (apostador.displayName !== args.displayName) {
      await ctx.db.patch(apostador._id, { displayName: args.displayName });
    }

    if (apostador.saldo < args.valor) {
      return {
        ok: false,
        error: `Saldo insuficiente (${apostador.saldo} créditos).`,
      };
    }

    const abertas = await ctx.db
      .query("apostas")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    const duplicada = abertas.some(
      (a) =>
        a.status === "aberta" &&
        (a.matchKey === args.matchKey || a.eventId === args.eventId)
    );
    if (duplicada) {
      return {
        ok: false,
        error: "Você já tem uma aposta aberta nesta partida.",
      };
    }

    const retornoPotencial = Math.round(args.valor * args.odd);
    const novoSaldo = apostador.saldo - args.valor;
    const now = Date.now();

    const apostaId = await ctx.db.insert("apostas", {
      userId: args.userId,
      eventId: args.eventId,
      matchKey: args.matchKey,
      home: args.home,
      away: args.away,
      palpite: args.palpite,
      odd: args.odd,
      valor: args.valor,
      retornoPotencial,
      status: "aberta",
      criadaEm: now,
    });

    await ctx.db.patch(apostador._id, {
      saldo: novoSaldo,
      totalApostado: apostador.totalApostado + args.valor,
      displayName: args.displayName,
    });

    return { ok: true, apostaId, novoSaldo };
  },
});

export const listOpen = query({
  args: { secret: v.optional(v.string()) },
  returns: v.array(apostaValidator),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const docs = await ctx.db
      .query("apostas")
      .withIndex("by_status", (q) => q.eq("status", "aberta"))
      .collect();
    return docs.map((d) => ({
      _id: d._id,
      userId: d.userId,
      eventId: d.eventId,
      matchKey: d.matchKey,
      home: d.home,
      away: d.away,
      palpite: d.palpite,
      odd: d.odd,
      valor: d.valor,
      retornoPotencial: d.retornoPotencial,
      status: d.status,
      criadaEm: d.criadaEm,
      liquidadaEm: d.liquidadaEm,
    }));
  },
});

export const listByUser = query({
  args: {
    secret: v.optional(v.string()),
    userId: v.string(),
    limit: v.optional(v.number()),
  },
  returns: v.array(apostaValidator),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const lim = Math.min(Math.max(args.limit ?? 10, 1), 50);
    const docs = await ctx.db
      .query("apostas")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    docs.sort((a, b) => b.criadaEm - a.criadaEm);
    return docs.slice(0, lim).map((d) => ({
      _id: d._id,
      userId: d.userId,
      eventId: d.eventId,
      matchKey: d.matchKey,
      home: d.home,
      away: d.away,
      palpite: d.palpite,
      odd: d.odd,
      valor: d.valor,
      retornoPotencial: d.retornoPotencial,
      status: d.status,
      criadaEm: d.criadaEm,
      liquidadaEm: d.liquidadaEm,
    }));
  },
});

export const settle = mutation({
  args: {
    secret: v.optional(v.string()),
    apostaId: v.id("apostas"),
    resultado: v.union(v.literal("ganhou"), v.literal("perdeu"), v.literal("cancelada")),
  },
  returns: settleResultValidator,
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const aposta = await ctx.db.get(args.apostaId);
    if (!aposta || aposta.status !== "aberta") {
      return { ok: false, error: "Aposta não encontrada ou já liquidada." };
    }

    const apostador = await getApostadorDoc(ctx, aposta.userId);
    if (!apostador) {
      return { ok: false, error: "Apostador não encontrado." };
    }

    const now = Date.now();
    let saldo = apostador.saldo;
    let totalGanho = apostador.totalGanho;
    let apostasGanhas = apostador.apostasGanhas;
    let apostasPerdidas = apostador.apostasPerdidas;

    if (args.resultado === "ganhou") {
      saldo += aposta.retornoPotencial;
      totalGanho += aposta.retornoPotencial - aposta.valor;
      apostasGanhas += 1;
    } else if (args.resultado === "perdeu") {
      totalGanho -= aposta.valor;
      apostasPerdidas += 1;
    } else {
      saldo += aposta.valor;
    }

    await ctx.db.patch(args.apostaId, {
      status: args.resultado,
      liquidadaEm: now,
    });
    await ctx.db.patch(apostador._id, {
      saldo,
      totalGanho,
      apostasGanhas,
      apostasPerdidas,
    });

    return { ok: true };
  },
});

export const getRanking = query({
  args: {
    secret: v.optional(v.string()),
    criterio: v.union(
      v.literal("vitorias"),
      v.literal("saldo"),
      v.literal("lucro")
    ),
    limit: v.optional(v.number()),
  },
  returns: v.array(
    v.object({
      userId: v.string(),
      displayName: v.string(),
      saldo: v.number(),
      totalGanho: v.number(),
      apostasGanhas: v.number(),
      apostasPerdidas: v.number(),
    })
  ),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const lim = Math.min(Math.max(args.limit ?? 15, 1), 50);
    const docs = (await ctx.db.query("apostadores").collect()).filter((d) =>
      isDiscordUserId(d.userId)
    );
    docs.sort((a, b) => {
      if (args.criterio === "lucro") {
        return b.totalGanho - a.totalGanho;
      }
      if (args.criterio === "saldo") {
        return b.saldo - a.saldo;
      }
      if (b.apostasGanhas !== a.apostasGanhas) {
        return b.apostasGanhas - a.apostasGanhas;
      }
      if (b.saldo !== a.saldo) {
        return b.saldo - a.saldo;
      }
      return a.apostasPerdidas - b.apostasPerdidas;
    });
    return docs.slice(0, lim).map((d) => ({
      userId: d.userId,
      displayName: d.displayName,
      saldo: d.saldo,
      totalGanho: d.totalGanho,
      apostasGanhas: d.apostasGanhas,
      apostasPerdidas: d.apostasPerdidas,
    }));
  },
});

export const creditoSemanal = mutation({
  args: {
    secret: v.optional(v.string()),
    weekKey: v.string(),
    credito: v.number(),
  },
  returns: v.object({ creditados: v.number() }),
  handler: async (ctx, args) => {
    checkSecret(args.secret);
    const docs = await ctx.db.query("apostadores").collect();
    let creditados = 0;

    for (const doc of docs) {
      if (!isDiscordUserId(doc.userId)) {
        continue;
      }
      if (doc.ultimoCreditoSemanal === args.weekKey) {
        continue;
      }
      const novoSaldo =
        doc.saldo === 0 ? args.credito : doc.saldo + args.credito;
      await ctx.db.patch(doc._id, {
        saldo: novoSaldo,
        ultimoCreditoSemanal: args.weekKey,
      });
      creditados += 1;
    }

    return { creditados };
  },
});

export const purgeTestApostadores = mutation({
  args: { secret: v.optional(v.string()) },
  returns: v.object({
    apostadoresRemovidos: v.number(),
    apostasRemovidas: v.number(),
  }),
  handler: async (ctx, args) => {
    checkSecret(args.secret);

    let apostasRemovidas = 0;
    const apostas = await ctx.db.query("apostas").collect();
    for (const aposta of apostas) {
      if (!isDiscordUserId(aposta.userId)) {
        await ctx.db.delete(aposta._id);
        apostasRemovidas += 1;
      }
    }

    let apostadoresRemovidos = 0;
    const apostadores = await ctx.db.query("apostadores").collect();
    for (const doc of apostadores) {
      if (!isDiscordUserId(doc.userId)) {
        await ctx.db.delete(doc._id);
        apostadoresRemovidos += 1;
      }
    }

    return { apostadoresRemovidos, apostasRemovidas };
  },
});
