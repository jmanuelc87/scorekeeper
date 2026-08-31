/**
 * Reads the conversation out of an AI chat page.
 *
 * Injected on demand by the service worker with `chrome.scripting.executeScript`
 * (never declared in the manifest — the extension only touches a page when the
 * user opens the popup on it, under `activeTab`). The value of the last statement
 * is what `executeScript` hands back, so this file ends in a call expression.
 *
 * The same injection first loads `src/vendor/turndown.js` and its GFM plugin, whose
 * globals this file serializes messages with — see `createTurndown`.
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
 * Four optional per-adapter fields go beyond the message bubbles:
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
 * - `model` — where the app names the model answering, so the capture can report it
 *   ("Claude Opus 4.5", "2.5 Pro"). Alone among these it is resolved against
 *   `document` rather than inside a bubble: the model picker is page furniture next
 *   to the composer, outside the transcript entirely. An adapter may leave it out —
 *   the popup's field is editable and blank is a valid answer, which beats guessing
 *   at a label that means something else (see `readModel`). Paired with
 *   `modelAttribute`, the name is read off an attribute instead of the element's
 *   text, for an app that records the model in its markup but never prints it
 *   (see the ChatGPT adapter).
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
      // The picker below the composer, whose label is the model in force for the
      // thread ("Opus 4.5"). It reads the *current* selection, not what answered an
      // older turn — switching models mid-conversation reports the one now selected.
      model: ['[data-testid="model-selector-dropdown"]', 'button[data-testid*="model-selector"]'],
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
      // The mode pill in the header, whose label is the model ("2.5 Pro", "2.5 Flash").
      // Beware `roles.model` above — same word, unrelated: that one matches answer
      // bubbles, this one the picker naming the model behind them.
      model: [
        "bard-mode-switcher .logo-pill-label-container",
        "bard-mode-switcher button",
        ".gds-mode-switch-button",
      ],
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
      // The `md-text-button` model picker. Its label is *light* DOM slotted into the
      // button's shadow root, so `deepQueryAll` reaches it by walking children —
      // no `content` indirection needed here.
      //
      // Both candidates land on `.model-selector-label`, the innermost div wrapping
      // just "2.5 Pro", rather than on the button or the label *container*: those two
      // also enclose the trailing `md-icon`, whose ligature text ("keyboard_arrow_down")
      // renders as a chevron but reads back as those literal words. The scoped
      // candidate is first so a dropdown listing every model cannot win over the
      // button showing the current one.
      model: [".action-model-selector .model-selector-label", ".model-selector-label"],
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
        user: [
          '[data-testid="chatQuestion"]',
          '[data-content="user-message"]',
          '[data-testid="user-message"]',
        ],
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
      // The model switcher by the composer, whose text is the model ("Opus"). Not to
      // be confused with `.fai-CopilotMessage__name` above an answer: that badge is
      // the *agent* persona, which is why it stays in `chrome` and not here.
      //
      // The id first because it is the only part of that button that is neither
      // hashed nor translated — Fluent generates every class on it (`f1c21dwh`, …)
      // and those change on any restyle, while `aria-label` is user-visible text that
      // a Spanish UI renders as "Selector de modelo". The label is still worth keeping
      // below it for the day the id changes.
      model: ["#gptModeSwitcher", '[aria-label="Model Selector"]'],
    },
    {
      id: "chatgpt",
      label: "ChatGPT",
      platform: "chatgpt",
      // `chat.openai.com` still resolves and redirects here, so a thread bookmarked
      // before the rename lands on the old host; matching both costs nothing.
      host: /(^|\.)chatgpt\.com$|(^|\.)chat\.openai\.com$/,
      roles: {
        // One attribute carries the role for both sides, and it is the most stable
        // handle this page offers: everything around it is either a Tailwind utility
        // (`whitespace-pre-wrap` on a question, `markdown prose` on an answer) or a
        // hashed class, both of which change on any restyle.
        //
        // The node it lands on holds the message and nothing else — the accessible
        // heading and the copy/feedback bar are siblings of it, not children, so
        // neither reaches the text. The `.markdown` body one level in is deliberately
        // not the target anyway: matching the outer node is what keeps an answer
        // rendered as several blocks in one message. Neither role ever contains the
        // other, so DOM order is turn order.
        user: ['[data-message-author-role="user"]'],
        model: ['[data-message-author-role="assistant"]'],
      },
      // Read off the answer itself, not off a picker — the one adapter here that can.
      // ChatGPT stamps every assistant message with the slug of the model that
      // produced it (`gpt-5-6-thinking`), while its switcher is an unlabelled icon
      // button whose only text is the product name in the header. So the usual target
      // does not exist, and this one is *better* than the usual target: the other four
      // adapters report whatever the picker shows at capture time, whereas this is the
      // model that actually answered.
      //
      // A slug, not a display name ("gpt-5-6-thinking", not "ChatGPT 5.1 Thinking"),
      // which is the more precise of the two and survives a marketing rename. The
      // popup's field is editable if you would rather store the label.
      //
      // Only assistant messages carry the attribute, so the selector cannot land on a
      // question. `readModel` takes the first match, i.e. the model behind the *first*
      // answer in the thread; a conversation whose model was switched halfway reports
      // the one it started with.
      model: ["[data-message-model-slug]"],
      modelAttribute: "data-message-model-slug",
      // The turn's accessible heading ("Tú dijiste:", "ChatGPT dijo:"), and with it
      // the copy/feedback bar's labels. Both render *beside* the message node rather
      // than inside it, so neither reaches `innerText` from there today; declared for
      // the day a reskin moves them in, where it costs nothing — an element that
      // contributes no text removes no lines.
      chrome: [".sr-only"],
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
          error: "Esta página no es un chat compatible (Copilot, Gemini, Claude o ChatGPT).",
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
        // "" when this adapter declares no `model` or nothing matched; the popup
        // treats that as "unknown" and lets the user fill it in.
        model: readModel(adapter),
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
   * The model named by this adapter's `model` selectors, or `""`.
   *
   * Searched across the whole `document`, unlike every other selector here: the model
   * picker sits by the composer, outside the transcript. Candidates are tried in
   * order and the first *non-empty* match wins, so a stale candidate that still
   * matches an empty placeholder does not shadow a working one below it.
   *
   * With `modelAttribute` the name is taken from that attribute rather than from the
   * element's text — the only way to read an app that knows which model answered but
   * never prints it (ChatGPT tags the answer `data-message-model-slug` and leaves its
   * switcher an unlabelled icon). Everything below applies the same either way.
   *
   * Only the first line is kept and its whitespace collapsed — these pickers stack a
   * chevron glyph and often a subtitle ("Modelo más capaz") under the name, none of
   * which belongs in a model label. Truncated to the column width the API stores.
   */
  function readModel(adapter) {
    for (const selector of adapter.model ?? []) {
      for (const element of deepQueryAll(selector, document)) {
        const source = adapter.modelAttribute
          ? element.getAttribute(adapter.modelAttribute)
          : (element.innerText ?? element.textContent);
        const text = (source ?? "").split("\n")[0].replace(/\s+/g, " ").trim();
        if (text) return text.slice(0, 128);
      }
    }
    return "";
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
    const outermost = nodes.filter(
      (node) => !nodes.some((other) => other !== node && deepContains(other, node)),
    );

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
   * `contextual_precision` ranks. `name` is omitted when a source has no title and
   * capped at 100 characters when it has a long one.
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
          const url = webUrl(citation?.url);
          if (url && !seen.has(url)) seen.set(url, citation.name ?? "");
        }
      }
    }

    if (seen.size === 0) return "";
    const sources = Array.from(seen, ([url, name]) => {
      const title = capName(name);
      return title ? { name: title, url } : { url };
    });
    return JSON.stringify(sources);
  }

  /** The longest `name` a citation record may carry, ellipsis included. */
  const MAX_NAME_LENGTH = 100;

  /**
   * A citation `name` capped at `MAX_NAME_LENGTH`, ending in `…` when it was cut.
   *
   * Some chips spell out a whole headline (or the first sentence of the source) as
   * their label; the column only needs enough to recognise the source, and the URL
   * identifies it either way. The ellipsis keeps the truncation visible to whoever
   * reads the turn.
   */
  function capName(name) {
    if (name.length <= MAX_NAME_LENGTH) return name;
    return `${name.slice(0, MAX_NAME_LENGTH - 1)}…`;
  }

  /**
   * A citation `href` as the absolute http(s) URL the backend can retrieve, or `""`.
   *
   * A chip's href is whatever the page put there: a relative path, an in-page `#anchor`
   * (which resolves to the chat page itself, not a source), or a `javascript:`/`mailto:`
   * link the retrieval pipeline has nothing to fetch from. Resolving against the page and
   * keeping only web URLs means the column holds sources, not markup artifacts.
   */
  function webUrl(href) {
    if (!href || href.startsWith("#")) return "";
    try {
      const { protocol, href: absolute } = new URL(href, document.baseURI);
      return protocol === "https:" || protocol === "http:" ? absolute : "";
    } catch {
      return ""; // Not a resolvable URL.
    }
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
   * The message in one bubble as Markdown, with interface chrome removed.
   *
   * When the adapter declares a `content` selector the text is read from the first
   * match inside the bubble instead: some apps match a role on a wrapper whose text
   * lives one shadow root deeper, where `innerText` on the wrapper reads empty.
   *
   * The bubble is serialized with Turndown (see `createTurndown`) rather than read
   * with `innerText`: these apps render an answer as real markup — headings, lists,
   * tables, fenced code, links — and `innerText` flattens all of it into lines, so a
   * judge reading the response back cannot tell a heading from a sentence or a table
   * from a run of words.
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
   *
   * `markHidden` runs between the two: Turndown reads markup, not layout, so unlike
   * `innerText` it would otherwise serialize everything the page keeps out of sight,
   * the elements `hideChrome` just hid included.
   */
  function readText(bubble, adapter) {
    const node = contentNode(bubble, adapter);
    // Read what the chrome says before hiding it: once hidden it has no `innerText`.
    const chrome = chromeLines(node, adapter);
    const restore = hideChrome(node, adapter);
    const unmark = markHidden(node);
    let raw;
    try {
      raw = TURNDOWN.turndown(node);
    } finally {
      unmark();
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

  /** Marks an element the page is not rendering, for the length of one read. */
  const HIDDEN_ATTRIBUTE = "data-scorekeeper-hidden";

  /**
   * The Markdown serializer, one per injection.
   *
   * The bundles it comes from are injected by the service worker just before this
   * file (see `CAPTURE_FILES` in `background.js`), which is what puts
   * `TurndownService` and `turndownPluginGfm` on the isolated world. The GFM plugin
   * is what teaches it tables and strikethrough, neither of which Turndown handles
   * on its own — and these apps answer with tables constantly.
   *
   * Two rules of our own: elements `markHidden` flagged are dropped, and a link is
   * kept only when its href is something the stored turn can be followed to.
   */
  function createTurndown() {
    const service = new TurndownService({
      headingStyle: "atx",
      hr: "---",
      bulletListMarker: "-",
      codeBlockStyle: "fenced",
      emDelimiter: "*",
    });
    service.use(turndownPluginGfm.gfm);
    // Turndown would emit whatever the page put in `href` — a relative path, an
    // in-page `#anchor`, a `javascript:` handler — none of which means anything once
    // the turn is a row in the database. `webUrl` keeps the absolute http(s) ones and
    // the rest degrade to their own text, which is still part of the sentence.
    service.addRule("webLink", {
      filter: (node) => node.nodeName === "A" && node.getAttribute("href"),
      replacement: (content, node) => {
        if (!content.trim()) return "";
        const url = webUrl(node.getAttribute("href"));
        return url ? `[${content}](${url})` : content;
      },
    });
    // Added last on purpose: `addRule` puts a rule *in front* of the ones already
    // there, so this is the one consulted first and a hidden link never reaches the
    // rule above. Not `service.remove()`, which loses to the built-in rules — those
    // are matched before the removal list, so a hidden `<p>` would still be a
    // paragraph.
    service.addRule("hidden", {
      filter: (node) => node.hasAttribute(HIDDEN_ATTRIBUTE),
      replacement: () => "",
    });
    return service;
  }

  const TURNDOWN = createTurndown();

  /**
   * Flag every element under `node` the page is not rendering, and return the undo.
   *
   * Turndown serializes a *clone* of the subtree, and a clone is outside the document
   * where `getComputedStyle` has nothing to report — so the question has to be asked
   * here, on the live nodes, and the answer carried across as an attribute the
   * serializer's `remove` rule can see. This is what keeps `hideChrome` working and
   * what stops the sr-only labels, collapsed menus and offscreen scaffolding these
   * apps are full of from being read as part of the answer.
   */
  function markHidden(node) {
    const marked = [];
    for (const element of node.querySelectorAll("*")) {
      const style = getComputedStyle(element);
      if (element.hidden || style.display === "none" || style.visibility === "hidden") {
        element.setAttribute(HIDDEN_ATTRIBUTE, "");
        marked.push(element);
      }
    }
    return () => {
      for (const element of marked) element.removeAttribute(HIDDEN_ATTRIBUTE);
    };
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
   * Hiding is the only lever that reaches furniture sitting inline in a sentence: a
   * source chip closing a paragraph shares its line with the prose it cites, so
   * dropping the line would drop the prose. Taking the element out of layout — where
   * `markHidden` then flags it and the serializer drops it — subtracts exactly its
   * own text instead.
   *
   * The page is left as it was found — capture only ever reads. Nothing is painted
   * in between (reading computed styles forces layout, not a repaint) and the undo runs
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
        hidden.push([
          element,
          element.getAttribute("style"),
          style.getPropertyValue("display"),
          style.getPropertyPriority("display"),
        ]);
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
