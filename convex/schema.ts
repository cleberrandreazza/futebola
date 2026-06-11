import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  // Um documento por usuário do Discord que segue times.
  // Espelha a estrutura do antigo seguindo.json:
  //   { userId: { times: [...], noticiasVistas: { time: [urls] } } }
  seguidores: defineTable({
    userId: v.string(),
    times: v.array(v.string()),
    noticiasVistas: v.record(v.string(), v.array(v.string())),
    prefs: v.optional(
      v.object({
        noticias: v.boolean(),
        jogos: v.boolean(),
        lembrete: v.boolean(),
      })
    ),
    lembretesEnviados: v.optional(v.array(v.string())),
  }).index("by_user", ["userId"]),

  apostadores: defineTable({
    userId: v.string(),
    displayName: v.string(),
    saldo: v.number(),
    totalApostado: v.number(),
    totalGanho: v.number(),
    apostasGanhas: v.number(),
    apostasPerdidas: v.number(),
    ultimoCreditoSemanal: v.optional(v.string()),
    criadoEm: v.number(),
  }).index("by_user", ["userId"]),

  apostas: defineTable({
    userId: v.string(),
    eventId: v.string(),
    home: v.string(),
    away: v.string(),
    palpite: v.union(v.literal("1"), v.literal("X"), v.literal("2")),
    odd: v.number(),
    valor: v.number(),
    retornoPotencial: v.number(),
    status: v.union(
      v.literal("aberta"),
      v.literal("ganhou"),
      v.literal("perdeu"),
      v.literal("cancelada")
    ),
    criadaEm: v.number(),
    liquidadaEm: v.optional(v.number()),
  })
    .index("by_user", ["userId"])
    .index("by_status", ["status"])
    .index("by_event", ["eventId"]),
});
