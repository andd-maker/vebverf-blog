// Проверка .pages.yml схемой самого Pages CMS (lib/config-schema.ts, коммит 6f4e860).
// Повод: 16.10.2026 правка конфига сломала раздел «Темы», а админка молча его спрятала.
// Запуск — в CI (ci.yml): схема скачивается в tools/.pcms/, реестр типов полей подставляется списком.
import fs from "fs"; import * as yaml from "js-yaml";
const { ConfigSchema } = await import("./.pcms/config-schema.ts");
const cfg = yaml.load(fs.readFileSync(process.argv[2], "utf8"));
const r = ConfigSchema.safeParse(cfg);
const walk = (iss, d = 0) => {{ for (const i of iss) {{ console.log(" ".repeat(d) + i.path.join(".") + " → " + i.message); if (i.unionErrors) i.unionErrors.forEach(u => walk(u.issues, d + 2)); }} }};
if (r.success) console.log("Pages CMS: .pages.yml OK"); else {{ walk(r.error.issues); process.exit(1); }}
