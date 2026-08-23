/**
 * Tests for the helpers in `src/config.js`, the module every extension surface imports.
 *
 * The pure helpers need nothing; the storage ones run against the in-memory `chrome`
 * stub below, which mimics the two `chrome.storage.local.get` call shapes config.js
 * actually uses — an object of defaults (every key comes back) and an array of keys
 * (only the present ones do). Getting that distinction wrong is what the run-history
 * migration depends on.
 */

import { beforeEach, describe, expect, it } from "vitest";
import {
  captureLabels,
  DEFAULTS,
  getRuns,
  getSettings,
  newestRun,
  normalizeChatUrl,
  originPattern,
  RUNS_KEY,
  runCaptures,
  runScenarioIds,
  slugify,
  trimSlash,
  upsertRun,
} from "../src/config.js";

/** Install a fake `chrome.storage.local` backed by `initial`, and return its store. */
function stubChrome(initial = {}) {
  const store = { ...initial };
  globalThis.chrome = {
    storage: {
      local: {
        async get(query) {
          if (Array.isArray(query)) {
            return Object.fromEntries(
              query.filter((key) => key in store).map((key) => [key, store[key]]),
            );
          }
          return Object.fromEntries(
            Object.entries(query).map(([key, fallback]) => [
              key,
              key in store ? store[key] : fallback,
            ]),
          );
        },
        async set(patch) {
          Object.assign(store, patch);
        },
        async remove(key) {
          delete store[key];
        },
      },
    },
  };
  return store;
}

describe("trimSlash", () => {
  it("drops trailing slashes so the API path never doubles up", () => {
    expect(trimSlash("http://localhost:8001/")).toBe("http://localhost:8001");
    expect(trimSlash("http://localhost:8001///")).toBe("http://localhost:8001");
  });

  it("trims surrounding whitespace and leaves a clean URL alone", () => {
    expect(trimSlash("  http://localhost:8001  ")).toBe("http://localhost:8001");
    expect(trimSlash("http://localhost:8001")).toBe("http://localhost:8001");
  });
});

describe("normalizeChatUrl", () => {
  it("keeps origin + pathname and drops the volatile query and hash", () => {
    expect(normalizeChatUrl("https://claude.ai/chat/abc?ref=x#msg-3")).toBe(
      "https://claude.ai/chat/abc",
    );
  });

  it("matches two URLs for the same chat that differ only after the path", () => {
    expect(normalizeChatUrl("https://claude.ai/chat/abc?a=1")).toBe(
      normalizeChatUrl("https://claude.ai/chat/abc#bottom"),
    );
  });

  it("returns null when the URL is unparseable", () => {
    expect(normalizeChatUrl("chrome://extensions is not a chat")).toBeNull();
  });
});

describe("originPattern", () => {
  it("reduces an API URL to the origin match pattern Chrome grants", () => {
    expect(originPattern("http://localhost:8001/api/v1/")).toBe("http://localhost:8001/*");
    expect(originPattern("https://scorekeeper.example.com")).toBe(
      "https://scorekeeper.example.com/*",
    );
  });

  it("returns null when the URL is unparseable", () => {
    expect(originPattern("scorekeeper")).toBeNull();
  });
});

describe("slugify", () => {
  it("folds accents and lowercases into a filename-safe slug", () => {
    expect(slugify("Diseño de Producto")).toBe("diseno-de-producto");
  });

  it("never leaves a dash at either end, including after truncation", () => {
    expect(slugify("!! hola !!")).toBe("hola");
    expect(slugify("aaaa bbbb cccc", 10)).toBe("aaaa-bbbb");
  });

  it("returns an empty string when nothing survives", () => {
    expect(slugify("¿?¡!")).toBe("");
  });
});

describe("captureLabels", () => {
  it("leaves a platform captured once as its bare name", () => {
    expect(captureLabels([{ platform: "copilot" }, { platform: "gemini" }])).toEqual([
      "copilot",
      "gemini",
    ]);
  });

  it("numbers a platform the run holds twice, since the API appends both", () => {
    expect(
      captureLabels([{ platform: "copilot" }, { platform: "gemini" }, { platform: "copilot" }]),
    ).toEqual(["copilot #1", "gemini", "copilot #2"]);
  });

  it("has one label per capture, in order", () => {
    const captures = [{ platform: "claude" }, { platform: "claude" }];
    expect(captureLabels(captures)).toHaveLength(captures.length);
  });
});

describe("runCaptures", () => {
  it("returns the captures of a run that holds several platform executions", () => {
    const captures = [{ platform: "copilot" }, { platform: "gemini" }];
    expect(runCaptures({ runId: "r1", captures })).toBe(captures);
  });

  it("reads a pre-multi-capture entry back as a single capture", () => {
    expect(
      runCaptures({
        runId: "r1",
        platform: "claude",
        model: "Opus",
        turns: 4,
        sourceUrl: "https://claude.ai/chat/abc",
      }),
    ).toEqual([
      { platform: "claude", model: "Opus", turns: 4, sourceUrl: "https://claude.ai/chat/abc" },
    ]);
  });

  it("is empty when the run names no capture at all", () => {
    expect(runCaptures({ runId: "r1" })).toEqual([]);
    expect(runCaptures(null)).toEqual([]);
  });
});

describe("runScenarioIds", () => {
  it("lists every scenario a lote grouped under one run, in capture order", () => {
    expect(
      runScenarioIds({
        runId: "r1",
        scenarioId: "s-01",
        captures: [{ scenarioId: "s-01" }, { scenarioId: "s-02" }],
      }),
    ).toEqual(["s-01", "s-02"]);
  });

  it("dedupes the scenario captured from several platforms", () => {
    expect(
      runScenarioIds({ runId: "r1", captures: [{ scenarioId: "s-01" }, { scenarioId: "s-01" }] }),
    ).toEqual(["s-01"]);
  });

  it("falls back to the run's own id for captures written before it moved", () => {
    expect(
      runScenarioIds({ runId: "r1", scenarioId: "s-01", captures: [{ platform: "copilot" }] }),
    ).toEqual(["s-01"]);
  });

  it("reads a pre-captures entry, and is empty when no scenario is named", () => {
    expect(runScenarioIds({ runId: "r1", scenarioId: "s-01", platform: "claude" })).toEqual([
      "s-01",
    ]);
    expect(runScenarioIds({ runId: "r1" })).toEqual([]);
    expect(runScenarioIds(null)).toEqual([]);
  });
});

describe("newestRun", () => {
  it("picks the most recently updated run, not the first stored", () => {
    const runs = [
      { runId: "a", updatedAt: "2026-08-20T10:00:00.000Z" },
      { runId: "b", updatedAt: "2026-08-20T12:00:00.000Z" },
    ];
    expect(newestRun(runs).runId).toBe("b");
  });

  it("tolerates an entry with no updatedAt rather than preferring it", () => {
    const runs = [{ runId: "a" }, { runId: "b", updatedAt: "2026-08-20T12:00:00.000Z" }];
    expect(newestRun(runs).runId).toBe("b");
  });

  it("is null with no runs", () => {
    expect(newestRun([])).toBeNull();
    expect(newestRun(undefined)).toBeNull();
  });
});

describe("getSettings", () => {
  beforeEach(() => stubChrome());

  it("falls back to the defaults when nothing is stored", async () => {
    expect(await getSettings()).toEqual(DEFAULTS);
  });

  it("trims the stored API URL", async () => {
    stubChrome({ apiUrl: "http://host:9000/" });
    expect(await getSettings()).toEqual({ ...DEFAULTS, apiUrl: "http://host:9000" });
  });

  it("ignores an empty stored API URL", async () => {
    stubChrome({ apiUrl: "" });
    expect((await getSettings()).apiUrl).toBe(DEFAULTS.apiUrl);
  });
});

describe("getRuns", () => {
  it("is empty before anything has been sent", async () => {
    stubChrome();
    expect(await getRuns()).toEqual([]);
  });

  it("migrates a pre-history lastRun exactly once, then drops the legacy key", async () => {
    const legacy = { runId: "r1", status: "en_cola" };
    const store = stubChrome({ lastRun: legacy });

    expect(await getRuns()).toEqual([legacy]);
    expect(store[RUNS_KEY]).toEqual([legacy]);
    expect("lastRun" in store).toBe(false);

    // A second read goes straight to the migrated list — the legacy value is gone,
    // so a re-run would silently resurrect it as a duplicate.
    expect(await getRuns()).toEqual([legacy]);
  });
});

describe("upsertRun", () => {
  beforeEach(() => stubChrome());

  it("puts the newest run first", async () => {
    await upsertRun({ runId: "a" });
    const runs = await upsertRun({ runId: "b" });
    expect(runs.map((run) => run.runId)).toEqual(["b", "a"]);
  });

  it("replaces the entry with the same runId instead of duplicating it", async () => {
    await upsertRun({ runId: "a", status: "en_cola" });
    await upsertRun({ runId: "b" });
    const runs = await upsertRun({ runId: "a", status: "completado" });

    expect(runs.map((run) => run.runId)).toEqual(["a", "b"]);
    expect(runs[0].status).toBe("completado");
  });

  it("caps the history and drops the oldest", async () => {
    for (let index = 0; index < 26; index += 1) {
      await upsertRun({ runId: `r${index}` });
    }
    const runs = await getRuns();

    expect(runs).toHaveLength(25);
    expect(runs[0].runId).toBe("r25");
    expect(runs.some((run) => run.runId === "r0")).toBe(false);
  });

  it("persists, so the next read sees the same list", async () => {
    await upsertRun({ runId: "a" });
    expect(await getRuns()).toEqual([{ runId: "a" }]);
  });
});
