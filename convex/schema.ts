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
  }).index("by_user", ["userId"]),
});
