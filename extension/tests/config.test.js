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
  DEFAULTS,
  getRuns,
  getSettings,
  normalizeChatUrl,
  originPattern,
  RUNS_KEY,
  slugify,
  timestamp,
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

describe("timestamp", () => {
  it("renders a sortable zero-padded local stamp", () => {
    expect(timestamp(new Date(2026, 0, 5, 9, 7))).toBe("2026-01-05-09-07");
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
