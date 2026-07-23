/**
 * Reads the conversation out of an AI chat page.
 *
 * Injected on demand by the service worker with `chrome.scripting.executeScript`
 * (never declared in the manifest — the extension only touches a page when the
 * user opens the popup on it, under `activeTab`). The value of the last statement
 * is what `executeScript` hands back, so this file ends in a call expression.
 *
 * ## Why everything is inside an IIFE
 *
 * The same frame is injected more than once — once when the popup opens to preview
 * the chat, again when it sends — and `executeScript` evaluates this file as a
 * classic script in the extension's isolated world, whose global scope *survives*
 * between injections and is only reset when the frame navigates. Declaring
 * anything at top level therefore works exactly once: the second injection dies on
 * `SyntaxError: Identifier 'ADAPTERS' has already been declared` before running a
 * line, the popup gets no result, and capture appears broken until a page reload.
 * The wrapper keeps the world clean, so injection is repeatable. Do not unwrap it.
 *
 * ## Maintaining the adapters
 *
 * The selectors below are the only part of the extension coupled to somebody
 * else's markup, and these vendors reskin their apps often. Each role lists
 * *candidate* selectors tried in order, so an obsolete one can stay while a new
 * one is added above it — capture keeps working through a redesign as long as one
 * candidate still matches. When a platform stops capturing, open its chat, find
 * the element wrapping a single message bubble, and add its selector here.
 *
 * Three optional per-adapter fields go beyond the message bubbles:
 *
 * - `content` — selectors for the text container *inside* a matched bubble, for
 *   apps whose role selector lands on a wrapper rather than on the text itself
 *   (see the Gemini Enterprise adapter, where the text sits one shadow root
 *   deeper). Absent, the bubble is read directly.
 * - `citations` — where the source chips of a grounded answer keep their URLs.
 *   What they yield becomes the message's `retrieved_context`, which is what the
 *   `contextual_precision` and `hallucination` metrics rank and check against. An
 *   adapter without this field simply reports no context. See `readCitations`.
 * - `chrome` — selectors for interface furniture *inside* a bubble whose text is
 *   not part of the message (accessible headings, the agent-name badge, the
 *   feedback prompt, a reasoning pill, an inline source chip). These are hidden
 *   while the message text is read and their lines dropped from it; see `readText`
 *   and `hideChrome`. Listing an element here that also appears under `citations`
 *   is the normal way to keep a chip's URL while dropping its label from the prose.
 *
 * Every selector is matched across the *composed* tree: `deepQueryAll` descends
 * into open shadow roots, so an app that renders entirely inside web components
 * (again, Gemini Enterprise) is reachable at all. A selector still cannot cross a
 * shadow boundary *within itself* — `a b` only matches when `b` is a light-DOM
 * descendant of `a` — which is why `content` exists.
 */

(() => {
  /** Per-platform selectors. `platform` is what gets sent to Scorekeeper. */
  const ADAPTERS = [
    {
      id: "claude",
      label: "Claude",
      platform: "claude",
      host: /(^|\.)claude\.ai$/,
      roles: {
        // `assistant-message` is gone from the current build — an answer is only
        // reachable by class now. Bare `.font-claude-response` will not do: Claude
        // reuses it as a *typography* class outside the transcript (the project name
        // in the collapsed sidebar carries it), which capture would report as a
        // one-word assistant turn. Both candidates below anchor it to a real turn —
        // `data-is-streaming` marks an answer specifically, `role="article"` wraps
        // every message — and the sidebar is inside neither.
        //
        // The node they land on spans the whole turn (thinking pill included, see
        // `chrome`). The `.standard-markdown` body below it is deliberately not the
        // target: one answer can hold several of those blocks, and matching the
        // outer node is what keeps them in one message.
        user: ['[data-testid="user-message"]', ".font-user-message", "[data-user-message-bubble]"],
        model: [
          "[data-is-streaming] .font-claude-response",
          '[role="article"] .font-claude-response',
          '[data-testid="assistant-message"]',
          ".font-claude-message",
        ],
      },
      // A web-grounded answer cites inline: every source is its own chip anchored at
      // the end of the sentence it supports, so there is no chip *group* to open —
      // the link is the citation. `.standard-markdown` is the answer body, which is
      // where chips live; scoping to it keeps the disclaimer link in the turn footer
      // out. The chip's own text ("Infobae", "Cadena Politica") is the source name —
      // it carries no `aria-label`, so `readCitations` falls back to reading it.
      citations: {
        containers: [".standard-markdown"],
        links: ['a[class*="group/tag"][href]'],
      },
      chrome: [
        // Extended thinking renders inside the answer as a collapsed "Thought for 2s"
        // pill plus an aria-live twin of the same words. Claude flags that whole
        // subtree `data-find-omitted` — its own marker for "not part of the message"
        // — which also covers the reasoning text once the pill is expanded.
        "[data-find-omitted]",
        ".sr-only",
        // The source chips again: their labels sit *inside* the sentence they cite
        // ("...5,000 MW adicionales. Cadena Politica"), so without this the model
        // reads as if it wrote the publisher's name. The URLs are already safe in
        // `retrieved_context` by the time the text is read.
        'a[class*="group/tag"]',
      ],
    },
    {
      id: "gemini",
      label: "Gemini",
      platform: "gemini",
      host: /(^|\.)gemini\.google\.com$/,
      roles: {
        user: ["user-query .query-text", "user-query"],
        model: ["model-response message-content", "model-response"],
      },
      // Angular CDK hides these from sight but not from `innerText`. The first sits
      // inside `.query-text`, so it leaks on the primary selector; the rest only
      // surface if a reskin drops us to the fallback candidates.
      chrome: [
        ".screen-reader-user-query-label", // "Tú dijiste"
        ".screen-reader-model-response-label", // "Gemini dijo"
        "model-response-disclaimers", // "Gemini es una IA y puede cometer errores."
        ".luminous-actions-container", // "Copiar instrucción" / edit bar
      ],
    },
    {
      id: "gemini-business",
      label: "Gemini Enterprise",
      // Same bucket as consumer Gemini; edit it in the popup to split them apart.
      platform: "gemini",
      host: /(^|\.)business\.gemini\.google$/,
      // Nothing here is reachable without `deepQueryAll`: this app renders inside
      // open shadow roots all the way down, and the conversation itself sits two
      // boundaries deep (ucs-standalone-app → ucs-results → ucs-conversation).
      // Both roles live in one `div.turn`, question first, so document order is
      // still turn order.
      roles: {
        user: [".question-block", ".question-wrapper"],
        model: ["ucs-response-markdown", "ucs-summary"],
      },
      // Those selectors match wrappers whose text is one shadow root further in
      // (`ucs-fast-markdown`), and `innerText` on a shadow host reads empty. The
      // markdown document holds the whole message including tables — a `<table>` is
      // a *light* child of `ucs-markdown-table`, so it comes along, while that
      // element's copy/download bar stays behind its shadow boundary. Landing here
      // also skips the "Copy prompt" button, the agent-thoughts header and the
      // answer footer, none of which are inside it.
      content: [".markdown-document"],
      // Each chip group hides its sources in a popover: one `single-popover-link`,
      // or an `md-menu-item` per source once the group carries a "+N" badge.
      citations: {
        containers: ["ucs-citation-chips"],
        links: ["a.single-popover-link[href]", "md-menu-item[href]"],
        label: ["aria-label", "data-aria-label"],
      },
      // Both are inert while `content` resolves: the sr-only labels ("Gemini
      // replied", "Response complete") sit outside the markdown document, and the
      // chips are slotted *into* it from light DOM, which `innerText` does not
      // collect. Declared for the day a reskin makes `content` miss and a whole
      // bubble gets read instead.
      chrome: [".sr-only", ".citation-slot"],
    },
    {
      id: "copilot",
      label: "Microsoft Copilot",
      platform: "copilot",
      host: /(^|\.)copilot\.microsoft\.com$|(^|\.)m365\.cloud\.microsoft$/,
      roles: {
        // M365 Copilot nests `chatOutput` inside `chatQuestion`, and
        // `copilot-message-div` around `copilot-message-reply-div`. Selecting either
        // outer node would swallow the other role's bubble, so match the inner ones:
        // these two never contain each other, which keeps DOM order == turn order.
        user: ['[data-testid="chatQuestion"]', '[data-content="user-message"]', '[data-testid="user-message"]'],
        model: [
          '[data-testid="copilot-message-reply-div"]',
          '[data-content="ai-message"]',
          '[data-testid="ai-message"]',
        ],
      },
      // The "Sources" flyout is collapsed until clicked and holds only that word;
      // the URLs live on the inline chips instead, as a JSON attribute.
      citations: { containers: ["[data-grouped-citations]"], json: "groupedCitations" },
      chrome: [
        ".fai-UserMessage__accessibleHeading", // "You said:"
        ".fai-CopilotMessage__accessibleHeading", // "<agent> said:"
        ".fai-CopilotMessage__name", // agent-name badge above the answer
        ".fai-CopilotMessage__actions", // copy / feedback / sources bar below it
        '[data-testid="foot-note-div"]',
      ],
    },
  ];

  /**
   * Interface chrome that `innerText` picks up inside a message bubble (copy
   * buttons, feedback prompts, timestamps). Matched against a whole trimmed line,
   * case-insensitively, so it only ever drops a line that is *nothing but* chrome.
   */
  const UI_NOISE = [
    /^copiar?$/i,
    /^copy( code)?$/i,
    /^editar?$/i,
    /^edit$/i,
    /^reintentar$/i,
    /^retry$/i,
    /^regenerar( respuesta)?$/i,
    /^regenerate( response)?$/i,
    /^compartir$/i,
    /^share$/i,
    /^me gusta$/i,
    /^no me gusta$/i,
    /^good response$/i,
    /^bad response$/i,
    /^mostrar (más|menos)$/i,
    /^show (more|less)$/i,
    /^ver (código|razonamiento)$/i,
    /^\d+ fuentes?$/i,
    /^\d+ sources?$/i,
    // Claude's extended-thinking pill ("Thought for 2s"), for the day it stops
    // being marked `data-find-omitted`. The duration is spelled out rather than
    // left as `.+`: a line that ends in a bare number and one word cannot be prose,
    // whereas `.+` would eat any sentence that happens to open the same way.
    /^thought for \d+\s*\w+$/i,
    /^pensó durante \d+\s*\w+$/i,
  ];

  function captureConversation() {
    try {
      const adapter = findAdapter(location);
      if (!adapter) {
        return {
          ok: false,
          error: "Esta página no es un chat compatible (Copilot, Gemini o Claude).",
          url: location.href,
          title: document.title,
        };
      }

      const messages = collectMessages(adapter);
      return {
        ok: true,
        adapter: { id: adapter.id, label: adapter.label, platform: adapter.platform },
        url: location.href,
        title: document.title,
        messages,
      };
    } catch (error) {
      return { ok: false, error: String(error?.message ?? error), url: location.href };
    }
  }

  /** The adapter whose host matches `location`. */
  function findAdapter(location) {
    return ADAPTERS.find((adapter) => adapter.host.test(location.hostname)) ?? null;
  }

  /**
   * Collect the conversation in the order it appears on screen: `{role, content}`
   * per message, plus `retrieved_context` on the ones that cite sources.
   *
   * Both roles are queried in one pass so DOM order *is* conversation order; each
   * node is then classified by which role's selector it matches.
   */
  function collectMessages(adapter) {
    const userSelector = firstMatching(adapter.roles.user);
    const modelSelector = firstMatching(adapter.roles.model);
    if (!userSelector && !modelSelector) return [];

    const combined = [userSelector, modelSelector].filter(Boolean).join(", ");
    const nodes = deepQueryAll(combined, document);
    // A bubble can contain another matching node (e.g. a quoted message); keep only
    // the outermost ones so nothing is captured twice.
    const outermost = nodes.filter((node) => !nodes.some((other) => other !== node && deepContains(other, node)));

    return outermost
      .map((node) => {
        const message = {
          role: userSelector && node.matches(userSelector) ? "user" : "model",
          content: readText(node, adapter),
        };
        // Absent rather than empty when the answer cited nothing, so the API sees the
        // same shape an .xlsx without a context column produces.
        const context = readCitations(node, adapter);
        if (context) message.retrieved_context = context;
        return message;
      })
      .filter((message) => message.content);
  }

  /**
   * The sources one message cites, as the `retrieved_context` value Scorekeeper stores.
   *
   * Serialized as a JSON array of `{name, url}` records — one element per retrieved
   * source — so the backend persists structured JSON in the column and splits it
   * into one document per element (`split_context_docs`), which is what
   * `contextual_precision` ranks. `name` is omitted when a source has no title.
   *
   * Only what was actually *retrieved* is recorded, never the sentence the chip is
   * anchored to: that text is the model's own output, and grounding a response
   * against itself would score every model as perfectly faithful.
   */
  function readCitations(node, adapter) {
    if (!adapter.citations) return "";
    const { containers, json, links, label } = adapter.citations;

    const seen = new Map(); // url -> display name, insertion-ordered.
    for (const selector of containers) {
      for (const chip of deepQueryAll(selector, node)) {
        // Two shapes in the wild: the sources encoded as JSON on the chip itself
        // (Microsoft Copilot), or one link per source in the popover the chip opens
        // (Gemini Enterprise). Both reduce to the same url -> name pairs. Unlike a
        // role's candidates, every `links` selector is tried — a page mixes chips
        // that cite one source with chips that cite several, and each shape has its
        // own markup.
        const citations = json
          ? parseCitations(chip, json)
          : links.flatMap((linkSelector) =>
              deepQueryAll(linkSelector, chip).map((link) => ({
                url: link.getAttribute("href"),
                // `label` is optional: Claude's chips spell the publisher out as
                // their own text rather than hiding it in an attribute. Reading the
                // link works whether or not it is displayed, so it is unaffected by
                // the same chips being hidden while the message text is read.
                name:
                  (label ?? []).map((attribute) => link.getAttribute(attribute)).find(Boolean) ??
                  (link.textContent ?? "").trim(),
              })),
            );
        for (const citation of citations) {
          const url = citation?.url;
          if (url && !seen.has(url)) seen.set(url, citation.name ?? "");
        }
      }
    }

    if (seen.size === 0) return "";
    const sources = Array.from(seen, ([url, name]) => (name ? { name, url } : { url }));
    return JSON.stringify(sources);
  }

  /** The citation records on one chip; a chip we cannot parse contributes nothing. */
  function parseCitations(chip, key) {
    try {
      const parsed = JSON.parse(chip.dataset[key] ?? "[]");
      return Array.isArray(parsed) ? parsed : [parsed];
    } catch {
      return []; // A reskin changed the payload — capture the text, skip the sources.
    }
  }

  /** The first selector in `candidates` that matches anything under `root`. */
  function firstMatching(candidates, root = document) {
    for (const selector of candidates) {
      if (deepFirst(selector, root)) return selector;
    }
    return null;
  }

  /** The first element under `root` matching `selector`, or `null`. */
  function deepFirst(selector, root) {
    return deepQueryAll(selector, root, 1)[0] ?? null;
  }

  /**
   * Every element under `root` matching `selector`, *including* those inside open
   * shadow roots — the only way to see a page built out of web components, where
   * `document.querySelectorAll` returns nothing at all.
   *
   * One depth-first pass over the whole tree, entering a host's shadow root before
   * its light children, so the order is stable and close enough to reading order
   * that a conversation comes back in turn order. On a page with no shadow roots
   * this returns exactly what `querySelectorAll` would have.
   */
  function deepQueryAll(selector, root, limit = Infinity) {
    try {
      document.createDocumentFragment().querySelector(selector);
    } catch {
      return []; // A selector Chrome cannot parse (a stale candidate) matches nothing.
    }

    const found = [];
    const visit = (node) => {
      // `root` itself may be a shadow host — a message bubble whose whole content
      // lives in its own shadow root is the normal case on such a page.
      if (node.shadowRoot) visit(node.shadowRoot);
      for (const element of node.children) {
        if (found.length >= limit) return;
        if (element.matches(selector)) found.push(element);
        visit(element);
      }
    };
    visit(root);
    return found;
  }

  /** Like `Node.contains`, but crossing shadow boundaries via each root's host. */
  function deepContains(ancestor, node) {
    for (let current = node; current; current = current.parentNode ?? current.host) {
      if (current === ancestor) return true;
    }
    return false;
  }

  /**
   * The visible text of one message bubble, with interface chrome removed.
   *
   * When the adapter declares a `content` selector the text is read from the first
   * match inside the bubble instead: some apps match a role on a wrapper whose text
   * lives one shadow root deeper, where `innerText` on the wrapper reads empty.
   *
   * `innerText` (not `textContent`) because it respects layout: block elements,
   * list items and code blocks keep their line breaks, which matters for a judge
   * reading the response back.
   *
   * The adapter's `chrome` elements are taken out two ways. They are hidden for the
   * duration of the read (see `hideChrome`), which is what removes furniture sitting
   * *inline* in a sentence — Claude's source chips end a paragraph mid-line, where
   * no line-level filter could reach them without taking the sentence too. Lines
   * equal to their text are then dropped as well, which costs nothing when hiding
   * already worked and still catches the element if some stylesheet outranked it.
   * Whole-line matches only, so a label can never eat a line of real conversation
   * that merely resembles it.
   *
   * `UI_NOISE` is the third filter, matching chrome by its wording rather than its
   * markup — for furniture we can only recognize by what it says.
   */
  function readText(bubble, adapter) {
    const node = contentNode(bubble, adapter);
    // Read what the chrome says before hiding it: once hidden it has no `innerText`.
    const chrome = chromeLines(node, adapter);
    const restore = hideChrome(node, adapter);
    let raw;
    try {
      raw = node.innerText ?? node.textContent ?? "";
    } finally {
      restore();
    }
    return raw
      .split("\n")
      .filter((line) => {
        const trimmed = line.trim();
        if (!trimmed) return true; // Keep blank lines; paragraph breaks collapse below.
        return !chrome.has(trimmed) && !UI_NOISE.some((pattern) => pattern.test(trimmed));
      })
      .join("\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  /**
   * The element inside `bubble` holding the message text — the bubble by default.
   *
   * The first *non-empty* match, not simply the first: a component that renders its
   * markdown incrementally can leave empty stand-ins of the same shape next to the
   * real one (Gemini Enterprise ships one before every rendered document).
   */
  function contentNode(bubble, adapter) {
    for (const selector of adapter.content ?? []) {
      for (const found of deepQueryAll(selector, bubble)) {
        if ((found.innerText ?? found.textContent ?? "").trim()) return found;
      }
    }
    return bubble;
  }

  /**
   * Take this adapter's chrome out of the page for the length of one text read, and
   * return the undo.
   *
   * `innerText` reports what is *rendered*, which is the only lever that reaches
   * furniture sitting inline in a sentence: a source chip closing a paragraph shares
   * its line with the prose it cites, so dropping the line would drop the prose.
   * Taking the element out of layout subtracts exactly its own text instead.
   *
   * The page is left as it was found — capture only ever reads. Nothing is painted
   * in between (reading `innerText` forces layout, not a repaint) and the undo runs
   * in a `finally`, so a throw mid-read cannot leave a bubble collapsed on screen.
   * `important` because these apps style with utility classes and one may carry
   * `!important`; whatever inline value was there before is put back verbatim,
   * including the usual case of there having been none.
   */
  function hideChrome(node, adapter) {
    const hidden = [];
    for (const selector of adapter.chrome ?? []) {
      for (const element of deepQueryAll(selector, node)) {
        const style = element.style;
        // Whether the element had a `style` attribute at all, not just what was in
        // it: writing to `.style` creates an empty one, and leaving that behind on
        // an element styled purely by class would be a change we could not undo.
        hidden.push([element, element.getAttribute("style"), style.getPropertyValue("display"), style.getPropertyPriority("display")]);
        style.setProperty("display", "none", "important");
      }
    }
    return () => {
      for (const [element, attribute, value, priority] of hidden) {
        if (value) element.style.setProperty("display", value, priority);
        else element.style.removeProperty("display");
        if (attribute === null) element.removeAttribute("style");
      }
    };
  }

  /** Every line of text contributed by this adapter's declared chrome elements. */
  function chromeLines(node, adapter) {
    const lines = new Set();
    for (const selector of adapter.chrome ?? []) {
      for (const element of deepQueryAll(selector, node)) {
        const text = element.innerText ?? element.textContent ?? "";
        for (const line of text.split("\n")) {
          const trimmed = line.trim();
          if (trimmed) lines.add(trimmed);
        }
      }
    }
    return lines;
  }

  return captureConversation();
})();
