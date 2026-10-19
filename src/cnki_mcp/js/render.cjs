"use strict";
const fs = require("node:fs");
const path = require("node:path");
const CSL = require("citeproc");

try {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const records = new Map(input.items.map(item => [item.id, item]));
  const localeRoot = path.join(__dirname, "..", "styles");
  const system = {
    retrieveLocale(language) {
      const locale = language.toLowerCase().startsWith("zh") ? "zh-CN" : "en-US";
      return fs.readFileSync(path.join(localeRoot, `locales-${locale}.xml`), "utf8");
    },
    retrieveItem(id) { return records.get(id); }
  };
  const engine = new CSL.Engine(system, input.style, "zh-CN");
  engine.setOutputFormat("text");
  engine.updateItems(input.items.map(item => item.id));
  const bibliography = engine.makeBibliography();
  if (!bibliography || !Array.isArray(bibliography[1])) throw new Error("No bibliography");
  const citations = (input.citations || []).map(id => engine.makeCitationCluster([{id}]));
  process.stdout.write(JSON.stringify({text: bibliography[1].map(line => line.trim()).join("\n"),
    citations, entry_ids: bibliography[0].entry_ids}));
} catch (_) {
  // Never echo records, paths, style content or processor errors to MCP/logs.
  process.stderr.write("CSL_RENDER_FAILED\n");
  process.exitCode = 2;
}
