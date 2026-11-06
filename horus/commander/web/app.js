"use strict";
// HORUS control app. Talks only to the AI pilot service that served this page.
// Every drone or AI string is shown with textContent, never parsed as HTML.

const TOKEN_KEY = "horus.token";
const PHONE_TOKEN_KEY = "horus.phone.token";
const FEED_LIMIT = 600;
const RECONNECT_MS = 2000;

const $ = (id) => document.getElementById(id);

// --- Phone view: `?view=phone` or the installed home-screen app. Laptop view = no class. ----
const phoneView = new URLSearchParams(location.search).get("view") === "phone"
  || window.matchMedia("(display-mode: standalone)").matches;
if (phoneView) document.body.classList.add("phone");

// The home-screen app starts at /?view=phone with no fragment, so the phone view also keeps
// its token in localStorage. Storage can be refused (private mode): then it lives for the tab.
const phoneStore = {
  get() {
    try { return localStorage.getItem(PHONE_TOKEN_KEY); } catch { return null; }
  },
  set(value) {
    try { localStorage.setItem(PHONE_TOKEN_KEY, value); } catch { /* tab only */ }
  },
  remove() {
    try { localStorage.removeItem(PHONE_TOKEN_KEY); } catch { /* nothing stored */ }
  },
};

function storeToken(value) {
  sessionStorage.setItem(TOKEN_KEY, value);
  if (phoneView) phoneStore.set(value);
}

// --- Login: the service prints http://127.0.0.1:<port>/#token=<token>. ---------------------
function tokenInHash() {
  const match = /(?:^#|&)token=([^&]*)/.exec(location.hash);
  return match ? decodeURIComponent(match[1]) : null;
}

function takeToken() {
  const fresh = tokenInHash();
  if (fresh !== null) {
    storeToken(fresh);
    // Keep the token out of the address bar, history and screenshots.
    history.replaceState(null, "", location.pathname + location.search);
  }
  const kept = sessionStorage.getItem(TOKEN_KEY);
  return kept === null && phoneView ? phoneStore.get() : kept;
}

// Opening the printed address in a tab that already shows the page only changes the hash, so
// the page would not start again: store the new token and start over with it.
window.addEventListener("hashchange", () => {
  const fresh = tokenInHash();
  if (fresh === null) return;
  storeToken(fresh);
  history.replaceState(null, "", location.pathname + location.search);
  location.reload();
});

const token = takeToken();
let stopped = false;

function setLink(text, state) {
  const link = $("link");
  link.textContent = text;
  link.className = `pill pill-${state}`;
}

function showNotice(text) {
  const notice = $("notice");
  notice.textContent = text;
  notice.hidden = false;
}

function unauthorized() {
  stopped = true;
  sessionStorage.removeItem(TOKEN_KEY);
  setLink("unauthorized", "bad");
  if (phoneView) {
    // A phone token the service no longer accepts was revoked (restarted without it).
    phoneStore.remove();
    showNotice(
      "Unauthorized: this phone's token does not match the running service " +
      "(it was restarted without the phone token, or with a new one). " +
      "Open the phone address again (\"HORUS phone app: https://…#token=…\", printed by scripts/phone_serve.sh).",
    );
    return;
  }
  showNotice(
    "Unauthorized: this page's token does not match the running service. " +
    "Open the address the service printed at start-up (\"HORUS control app: …\") again.",
  );
}

class Unauthorized extends Error {}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  if (response.status === 401) {
    unauthorized();
    throw new Unauthorized();
  }
  return response;
}

// --- News feed: /events is server-sent events; EventSource cannot send the token header, ---
// --- so read the stream with fetch and parse the frames here. ------------------------------
let lastEventId = 0;

function shortTool(name) {
  return String(name || "").replace(/^mcp__horus__/, "");
}

function compact(value, limit = 160) {
  let text = typeof value === "string" ? value : JSON.stringify(value);
  if (text === undefined) text = "";
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

// Tool summaries often arrive as FastMCP's {"result": "<json text>"}; show the inner JSON.
// The AI pilot cuts summaries at 500 characters, so the wrapper may no longer parse: then
// strip it and its escaping by hand.
const RESULT_WRAPPER = '{"result":"';

function readableResult(summary) {
  let value = summary;
  for (let depth = 0; depth < 2 && typeof value === "string"; depth += 1) {
    try {
      value = JSON.parse(value);
    } catch {
      if (value.startsWith(RESULT_WRAPPER)) {
        value = value.slice(RESULT_WRAPPER.length).replace(/\\(["\\])/g, "$1");
      }
      break;
    }
    if (value && typeof value === "object" && !Array.isArray(value)
      && Object.keys(value).length === 1 && "result" in value) {
      value = value.result;
    }
  }
  return compact(value, 240);
}

// Returns [css class, who, text] for one event, or null to leave it out of the feed.
function describe(event) {
  const kind = event.kind;
  if (event.source === "mcp-event") {
    const alarm = ["emergency", "auto_rth", "link_lost", "pilot_override"].includes(kind);
    return [alarm ? "drone alarm" : "drone", "Drone", `${kind}: ${event.summary || ""}`];
  }
  switch (kind) {
    case "task_accepted":
      return ["you", "You", event.text];
    case "task_completed":
      return ["system", "Done", "turn finished"];
    case "task_error":
      return ["error", "Failed", event.error];
    case "emergency": {
      const status = event.status === "executed" ? "done" : event.status;
      const detail = event.error ? ` (${event.error})` : "";
      return ["alarm", "EMERGENCY", `${String(event.action).toUpperCase()} ${status}${detail}`];
    }
    case "assistant_text":
      return event.text ? ["ai", "AI", event.text] : null;
    case "final_result":
      return null; // the same text already arrived as assistant_text
    case "tool_use":
      return ["tool", "AI →", `${shortTool(event.tool)} ${compact(event.args || {})}`];
    case "tool_result":
      return [event.is_error ? "error" : "tool", "←", readableResult(event.summary || "")];
    case "assistant_delta":
    case "thinking":
    case "mcp_init":
      return null;
    case "session_reset_requested":
      return ["system", "New mission", "starting"];
    case "session_reset":
      return ["system", "New mission", "ready"];
    default: {
      const problem = event.error ? ` ${event.error}` : "";
      return [event.error ? "error" : "system", kind || "event", problem.trim()];
    }
  }
}

function clock(event) {
  const when = event.time ? new Date(event.time) : new Date();
  return Number.isNaN(when.getTime()) ? "" : when.toLocaleTimeString();
}

function feedEntry(event, [cls, who, text]) {
  const item = document.createElement("li");
  item.className = cls;
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = clock(event);
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who;
  const body = cls === "ai" ? markdownNodes(String(text ?? "")) : [document.createTextNode(String(text ?? ""))];
  item.append(time, label, ...body);
  return item;
}

// The AI writes markdown. A small part of it shows as such, built from DOM nodes and text, never
// parsed as HTML: **bold**, *italic*, `code`, "- " and "1. " lists and "#" headings (bold).
// Anything else stays as written.
function markdownNodes(text) {
  const nodes = [];
  let paragraph = [];
  let list = null; // { element, ordered }
  const flush = () => {
    const joined = paragraph.join("\n").replace(/^\n+|\n+$/g, "");
    if (joined) nodes.push(...inlineMarkdown(joined));
    paragraph = [];
  };
  for (const line of text.replace(/\r\n?/g, "\n").split("\n")) {
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\s*(\d{1,4})[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      flush();
      if (!list || list.ordered !== ordered) {
        list = { element: document.createElement(ordered ? "ol" : "ul"), ordered };
        list.element.className = "md-list";
        if (ordered && numbered[1] !== "1") list.element.start = Number(numbered[1]);
        nodes.push(list.element);
      }
      const entry = document.createElement("li");
      entry.className = "md-item";
      entry.append(...inlineMarkdown(ordered ? numbered[2] : bullet[1]));
      list.element.append(entry);
      continue;
    }
    if (list && !line.trim()) continue; // a blank line between items keeps the list going
    list = null;
    const heading = /^\s*#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      paragraph.push(`**${heading[1].replace(/\*\*/g, "")}**`);
      continue;
    }
    paragraph.push(line);
  }
  flush();
  return nodes;
}

function inlineMarkdown(text) {
  const nodes = [];
  const pattern = /\*\*([^\n]+?)\*\*|`([^`\n]+)`|(?<![\w*])\*(?![\s*])([^*\n]*?[^\s*])\*(?![\w*])/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) nodes.push(document.createTextNode(text.slice(last, match.index)));
    const [tag, inner] = match[1] !== undefined ? ["strong", match[1]] : match[2] !== undefined ? ["code", match[2]] : ["em", match[3]];
    const element = document.createElement(tag);
    element.textContent = inner;
    nodes.push(element);
    last = match.index + match[0].length;
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)));
  return nodes;
}

// Runs a change to the feed and keeps it scrolled to the newest entry if it was there.
function changeFeed(change) {
  const feed = $("feed");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 40;
  change(feed);
  while (feed.children.length > FEED_LIMIT) {
    const thumb = feed.firstElementChild.querySelector(".view-thumb");
    if (thumb) thumbObserver.unobserve(thumb);
    feed.firstElementChild.remove();
  }
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
}

function addToFeed(event) {
  if (event.kind === "ai_view") {
    // Outside the phone's folded commands block, which stays open for the turn's next calls.
    if (event.view_id) changeFeed((feed) => feed.append(viewEntry(event)));
    return;
  }
  if (phoneView) {
    addToPhoneFeed(event);
    return;
  }
  const described = describe(event);
  if (!described) return;
  changeFeed((feed) => feed.append(feedEntry(event, described)));
}

// --- Phone feed: one AI turn's tool calls and results fold into one "Show commands" block. ---
// Any other visible event (or the turn's final result) closes the block; the hidden kinds do
// not. Trimming at FEED_LIMIT removes whole blocks, since a block is a single feed entry.
let commandBlock = null; // the open block: { item, label, errors, calls, failed }
let waitingCommands = []; // command lines still waiting for their result, oldest first
const WAITING_LIMIT = 50;

function resetFeed() {
  thumbObserver.disconnect();
  $("feed").replaceChildren();
  commandBlock = null;
  waitingCommands = [];
  forgetAiViews();
}

function isCommandEvent(event) {
  return event.source !== "mcp-event" && (event.kind === "tool_use" || event.kind === "tool_result");
}

function addToPhoneFeed(event) {
  if (isCommandEvent(event)) {
    changeFeed(() => addCommand(event));
    return;
  }
  const described = describe(event);
  if (!described) {
    if (event.kind === "final_result") commandBlock = null;
    return;
  }
  commandBlock = null;
  changeFeed((feed) => feed.append(feedEntry(event, described)));
}

function labelCommandBlock(block) {
  const verb = block.details.open ? "Hide" : "Show";
  block.label.textContent = `${verb} commands (${block.calls})`;
  block.errors.textContent = block.failed ? ` · ${block.failed} failed` : "";
}

function openCommandBlock(event) {
  const item = document.createElement("li");
  item.className = "commands";
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = clock(event);
  const label = document.createElement("span");
  label.className = "commands-label";
  const errors = document.createElement("span");
  errors.className = "commands-errors";
  summary.append(time, label, errors);
  const list = document.createElement("ol");
  list.className = "command-list";
  details.append(summary, list);
  item.append(details);
  const block = { item, details, label, errors, list, calls: 0, failed: 0 };
  details.addEventListener("toggle", () => labelCommandBlock(block));
  $("feed").append(item);
  commandBlock = block;
  return block;
}

function rawText(title, text) {
  const part = document.createElement("div");
  part.className = "command-raw";
  const heading = document.createElement("span");
  heading.className = "command-raw-title";
  heading.textContent = title;
  const body = document.createElement("pre");
  body.textContent = text;
  part.append(heading, body);
  return part;
}

function commandLine(block, event) {
  const item = document.createElement("li");
  item.className = "command";
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const name = document.createElement("span");
  name.className = "command-name";
  const args = document.createElement("span");
  args.className = "command-args";
  const status = document.createElement("span");
  status.className = "command-status";
  summary.append(name, args, status);
  details.append(summary);
  item.append(details);
  block.list.append(item);
  return { block, details, name, args, status, id: event.tool_use_id || null };
}

function addCommand(event) {
  if (event.kind === "tool_use") {
    const block = commandBlock || openCommandBlock(event);
    const line = commandLine(block, event);
    line.name.textContent = shortTool(event.tool) || "tool";
    line.args.textContent = ` ${compact(event.args || {}, 80)}`;
    line.status.textContent = " · running";
    line.details.append(rawText("Request", `${event.tool || ""} ${JSON.stringify(event.args ?? {}, null, 2)}`));
    block.calls += 1;
    labelCommandBlock(block);
    waitingCommands.push(line);
    if (waitingCommands.length > WAITING_LIMIT) waitingCommands.shift();
    return;
  }
  // tool_result: the backend's tool_use_id when it sends one, else the oldest waiting call.
  const id = event.tool_use_id || null;
  let index = id ? waitingCommands.findIndex((line) => line.id === id) : -1;
  if (index < 0 && waitingCommands.length && (!id || !waitingCommands[0].id)) index = 0;
  let line;
  if (index >= 0) {
    [line] = waitingCommands.splice(index, 1);
  } else {
    line = commandLine(commandBlock || openCommandBlock(event), event);
    line.name.textContent = "result";
  }
  const failed = Boolean(event.is_error);
  line.status.textContent = failed ? " · error" : " · ok";
  line.status.className = failed ? "command-status bad" : "command-status ok";
  line.details.append(rawText("Result", String(event.summary ?? "")));
  if (failed) line.block.failed += 1;
  labelCommandBlock(line.block);
}

function handleFrame(frame) {
  let id = null;
  const data = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) id = Number(line.slice(3).trim());
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (Number.isInteger(id)) lastEventId = Math.max(lastEventId, id);
  if (!data.length) return; // keep-alive comment
  let event;
  try {
    event = JSON.parse(data.join("\n"));
  } catch {
    addToFeed({ kind: "unreadable_event", error: data.join(" ").slice(0, 200) });
    return;
  }
  noteEmergencyResult(event);
  if (event.kind === "ai_view" && event.view_id) noteAiView(event);
  addToFeed(event);
}

// --- What the AI saw: the camera pictures the AI's tools returned to the AI model. The feed ----
// --- carries only their ids (ai_view events); the page fetches a picture, with the token, only --
// --- when it shows it: a feed thumbnail scrolled into view, the viewer, the phone's Camera tab. --
const AI_VIEWS_KEPT = 30; // the AI pilot service keeps the newest 30 too
const VIEW_URLS_KEPT = 40; // pictures held as object URLs at once; the least recently used go
const viewUrls = new Map(); // view id -> Promise of an object URL, or null when no longer kept

function viewUrl(id) {
  let pending = viewUrls.get(id);
  if (pending) {
    viewUrls.delete(id); // most recently used last
    viewUrls.set(id, pending);
    return pending;
  }
  pending = (async () => {
    // An id always names the same picture: the browser may answer from its own cache.
    const response = await fetch(`/api/ai-view/${encodeURIComponent(id)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (response.status === 401) {
      unauthorized();
      throw new Unauthorized();
    }
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return URL.createObjectURL(await response.blob());
  })();
  pending.catch(() => {
    if (viewUrls.get(id) === pending) viewUrls.delete(id); // a failed fetch may be tried again
  });
  viewUrls.set(id, pending);
  while (viewUrls.size > VIEW_URLS_KEPT) {
    const [oldest, url] = viewUrls.entries().next().value;
    viewUrls.delete(oldest);
    url.then((value) => value && URL.revokeObjectURL(value), () => {});
  }
  return pending;
}

function ageText(time) {
  const seconds = Math.max(0, (Date.now() - Date.parse(time)) / 1000);
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 2) return "just now";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

// The feed entry has its own clock; the Camera tab and the viewer add the time and the age.
function viewCaption(event, withTime = false) {
  const parts = [shortTool(event.tool) || "camera"];
  if (withTime) parts.push(clock(event), `AI looked ${ageText(event.time)}`);
  if (Number.isFinite(Number(event.bytes))) parts.push(sizeText(Number(event.bytes)));
  return parts.filter(Boolean).join(" · ");
}

// Feed thumbnails: a placeholder until it scrolls near the visible part of the feed; only then
// is the picture fetched and its image made. A hidden feed (another phone tab) loads nothing.
const thumbEvents = new WeakMap(); // thumbnail button -> its ai_view event
const thumbObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    thumbObserver.unobserve(entry.target);
    fillThumb(entry.target);
  }
}, { root: $("feed"), rootMargin: "300px 0px" });

function viewEntry(event) {
  const item = feedEntry(event, ["view", "AI saw", viewCaption(event)]);
  const thumb = document.createElement("button");
  thumb.type = "button";
  thumb.className = "view-thumb";
  thumb.setAttribute("aria-label", `Show what the AI saw with ${shortTool(event.tool) || "the camera"}, full screen`);
  thumbEvents.set(thumb, event);
  thumb.addEventListener("click", () => {
    if (thumb.dataset.state === "failed") fillThumb(thumb);
    else if (thumb.dataset.state !== "gone") openViewer(event);
  });
  item.append(thumb);
  thumbObserver.observe(thumb);
  return item;
}

async function fillThumb(thumb) {
  const event = thumbEvents.get(thumb);
  thumb.dataset.state = "loading";
  thumb.textContent = "loading…";
  try {
    const url = await viewUrl(event.view_id);
    if (!url) {
      thumb.dataset.state = "gone";
      thumb.textContent = "no longer kept";
      return;
    }
    const image = document.createElement("img");
    image.loading = "lazy";
    image.decoding = "async";
    image.alt = "";
    image.src = url;
    thumb.dataset.state = "shown";
    thumb.replaceChildren(image);
  } catch (error) {
    if (error instanceof Unauthorized) return;
    thumb.dataset.state = "failed";
    thumb.textContent = "not loaded: tap to retry";
  }
}

// Full-screen viewer, from a feed thumbnail or the Camera tab's picture.
let viewerEvent = null;

async function openViewer(event) {
  viewerEvent = event;
  const image = $("viewer-image");
  image.removeAttribute("src");
  image.hidden = true;
  $("viewer-caption").textContent = viewCaption(event, true);
  $("viewer").hidden = false;
  $("viewer-close").focus();
  try {
    const url = await viewUrl(event.view_id);
    if (viewerEvent !== event) return;
    if (!url) {
      $("viewer-caption").textContent = `${viewCaption(event, true)} · no longer kept`;
      return;
    }
    image.src = url;
    image.hidden = false;
  } catch (error) {
    if (error instanceof Unauthorized || viewerEvent !== event) return;
    $("viewer-caption").textContent = `${viewCaption(event, true)} · could not load the picture`;
  }
}

function closeViewer() {
  viewerEvent = null;
  $("viewer").hidden = true;
  $("viewer-image").removeAttribute("src");
}

$("viewer-close").addEventListener("click", closeViewer);
$("viewer").addEventListener("click", (event) => {
  if (event.target === $("viewer")) closeViewer(); // a tap beside the picture
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("viewer").hidden) closeViewer();
});

// Phone Camera tab: the newest AI view large, older ones by swipe or the arrows, and an optional
// live view of the drone camera (off by default; it costs a picture a second).
const aiViews = []; // ai_view events, oldest first
let aiViewIndex = -1; // the one shown
let latestAsked = false;
const cameraOnScreen = () => phoneView && phoneTab === "camera" && !document.hidden;

function noteAiView(event) {
  if (aiViews.some((known) => known.view_id === event.view_id)) return;
  const following = aiViewIndex === aiViews.length - 1;
  aiViews.push(event);
  if (aiViews.length > AI_VIEWS_KEPT) {
    aiViews.shift();
    aiViewIndex = Math.max(0, aiViewIndex - 1);
  }
  if (following) aiViewIndex = aiViews.length - 1;
  if (cameraOnScreen()) showAiView();
}

function forgetAiViews() {
  aiViews.length = 0;
  aiViewIndex = -1;
  latestAsked = false;
  $("aiview-image").dataset.viewId = "";
  if (cameraOnScreen()) showAiView();
}

// A page opened after the service's event history moved past the last view still shows it.
async function askLatestAiView() {
  if (stopped || latestAsked || aiViews.length) return;
  latestAsked = true;
  try {
    const response = await api("/api/ai-view/latest");
    if (response.ok) noteAiView({ kind: "ai_view", ...(await response.json()) });
  } catch {
    latestAsked = false; // try again when the tab is next opened
  }
}

function showAiViewText() {
  const event = aiViews[aiViewIndex];
  $("aiview-nav-row").hidden = liveOn || aiViews.length < 2;
  $("aiview-count").textContent = aiViews.length ? `${aiViewIndex + 1} / ${aiViews.length}` : "";
  $("aiview-older").disabled = aiViewIndex <= 0;
  $("aiview-newer").disabled = aiViewIndex >= aiViews.length - 1;
  if (liveOn) return; // the live loop writes the caption
  $("aiview-caption").className = "aiview-caption";
  $("aiview-caption").textContent = event ? viewCaption(event, true) : "";
}

async function showAiView() {
  showAiViewText();
  if (liveOn) return; // the live picture has the slot
  const event = aiViews[aiViewIndex];
  const image = $("aiview-image");
  const empty = $("aiview-empty");
  if (!event) {
    image.hidden = true;
    empty.hidden = false;
    empty.className = "camera-empty";
    empty.textContent = "The AI has not looked at the camera yet.";
    return;
  }
  if (image.dataset.viewId === event.view_id && !image.hidden) return;
  try {
    const url = await viewUrl(event.view_id);
    if (aiViews[aiViewIndex] !== event || liveOn || !cameraOnScreen()) return;
    if (!url) {
      image.hidden = true;
      image.dataset.viewId = "";
      empty.hidden = false;
      empty.className = "camera-empty";
      empty.textContent = `This view is no longer kept: the AI pilot keeps the newest ${AI_VIEWS_KEPT}.`;
      return;
    }
    image.src = url;
    image.dataset.viewId = event.view_id;
    image.hidden = false;
    empty.hidden = true;
  } catch (error) {
    if (error instanceof Unauthorized || aiViews[aiViewIndex] !== event) return;
    image.hidden = true;
    image.dataset.viewId = "";
    empty.hidden = false;
    empty.className = "camera-empty bad";
    empty.textContent = `Could not load the picture: ${error.message || "AI pilot service unreachable"}`;
  }
}

function stepAiView(delta) {
  if (liveOn || !aiViews.length) return;
  aiViewIndex = Math.min(aiViews.length - 1, Math.max(0, aiViewIndex + delta));
  showAiView();
}

$("aiview-older").addEventListener("click", () => stepAiView(-1));
$("aiview-newer").addEventListener("click", () => stepAiView(1));
setInterval(() => {
  if (cameraOnScreen() && !liveOn) showAiViewText(); // "AI looked 12 s ago" keeps counting
}, 1000);

// Swipe right for an older view, left for a newer one; a tap opens it full screen.
let swipeStart = null;
$("aiview-image").addEventListener("pointerdown", (event) => {
  swipeStart = [event.clientX, event.clientY];
});
$("aiview-image").addEventListener("pointercancel", () => {
  swipeStart = null;
});
$("aiview-image").addEventListener("pointerup", (event) => {
  if (!swipeStart) return;
  const dx = event.clientX - swipeStart[0];
  const dy = event.clientY - swipeStart[1];
  swipeStart = null;
  if (Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) stepAiView(dx > 0 ? -1 : 1);
  else if (Math.hypot(dx, dy) < TAP_SLOP_PX && !liveOn && aiViews[aiViewIndex]) openViewer(aiViews[aiViewIndex]);
});

// Live: the drone board's current numbered frame through /api/frame, at most once a second, only
// while the Camera tab is on screen. Leaving the tab turns it off; each picture's size is shown.
const LIVE_INTERVAL_MS = 1000;
let liveOn = false;
let liveRunning = false;
let liveBytes = null; // the last live picture's download size

function setLive(on) {
  liveOn = on;
  const button = $("live-toggle");
  button.textContent = on ? "Live: on" : "Live: off";
  button.setAttribute("aria-pressed", String(on));
  showLiveNote();
  showAiViewText(); // no arrows while live
  if (on) {
    runLive();
  } else {
    $("aiview-image").dataset.viewId = ""; // put the AI's view back
    showAiView();
  }
}

function showLiveNote() {
  const measured = liveBytes === null ? "" : ` Last picture: ${sizeText(liveBytes)}.`;
  $("live-note").textContent = liveOn
    ? `Live uses mobile data: about ${liveBytes === null ? "…" : sizeText(liveBytes * 60)} a minute.`
    : `Live shows the drone camera now, 1 picture a second. It uses mobile data.${measured}`;
}

function showLiveFrame(frame, bytes) {
  liveBytes = bytes;
  const image = $("aiview-image");
  image.src = `data:${frame.mime_type || "image/jpeg"};base64,${frame.image_base64}`;
  image.dataset.viewId = "";
  image.hidden = false;
  $("aiview-empty").hidden = true;
  const age = Number(frame.observation_age_s);
  const caption = $("aiview-caption");
  caption.className = Number.isFinite(age) && age <= STALE_AFTER_S ? "aiview-caption live" : "aiview-caption live stale";
  caption.textContent = [
    "LIVE",
    `frame ${frame.frame_id}`,
    Number.isFinite(age) ? `${age.toFixed(1)} s old` : "age unknown",
    `${sizeText(bytes)} per picture`,
  ].join(" · ");
  showLiveNote();
}

function showLiveProblem(reason) {
  $("aiview-image").hidden = true;
  const empty = $("aiview-empty");
  empty.hidden = false;
  empty.className = "camera-empty bad";
  empty.textContent = `No live camera: ${reason}`;
  $("aiview-caption").textContent = "";
}

async function runLive() {
  if (liveRunning || stopped || !liveOn || !cameraOnScreen()) return;
  liveRunning = true;
  try {
    while (!stopped && liveOn && cameraOnScreen()) {
      const started = Date.now();
      try {
        const response = await api("/api/frame");
        const text = await response.text();
        const bytes = Number(response.headers.get("content-length")) || text.length;
        let payload = {};
        try {
          payload = JSON.parse(text);
        } catch {
          payload = {};
        }
        if (!liveOn || !cameraOnScreen()) break;
        if (response.ok) showLiveFrame(payload, bytes);
        else showLiveProblem(payload.error || `HTTP ${response.status}`);
      } catch (error) {
        if (error instanceof Unauthorized) return;
        if (liveOn) showLiveProblem("AI pilot service unreachable");
      }
      await sleep(Math.max(0, LIVE_INTERVAL_MS - (Date.now() - started)));
    }
  } finally {
    liveRunning = false;
  }
}

$("live-toggle").addEventListener("click", () => setLive(!liveOn));
document.addEventListener("visibilitychange", runLive);

let serviceRun = null;

async function followEvents() {
  while (!stopped) {
    try {
      const headers = lastEventId ? { "Last-Event-ID": String(lastEventId) } : {};
      const response = await api("/events", { headers });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      const run = response.headers.get("x-horus-run");
      if (serviceRun !== null && run !== serviceRun) {
        // The service restarted and its event ids started again at 1: replay its history.
        response.body.cancel();
        serviceRun = run;
        lastEventId = 0;
        // A press sent to the old run can no longer be confirmed; its red warning stays shown.
        pendingEmergencies.clear();
        resetFeed();
        continue;
      }
      serviceRun = run;
      setLink("live", "ok");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          handleFrame(buffer.slice(0, cut));
          buffer = buffer.slice(cut + 2);
        }
      }
      throw new Error("stream ended");
    } catch (error) {
      if (error instanceof Unauthorized || stopped) return;
      setLink("reconnecting", "wait");
      warnUnconfirmedEmergencies();
      await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS));
    }
  }
}

// --- Mission box: free text goes to POST /task; the AI pilot answers in the news feed. -----
// Successes fade after STATUS_CLEAR_MS on the phone, where every line takes room from the feed and
// the map; failures and anything still waiting (sticky) stay until replaced.
const STATUS_CLEAR_MS = 6000;
const statusTimers = new Map();
function showStatus(id, base, text, bad, sticky = false) {
  const status = $(id);
  status.textContent = text;
  status.className = bad ? `${base} bad` : base;
  clearTimeout(statusTimers.get(id));
  if (phoneView && !bad && !sticky) {
    statusTimers.set(id, setTimeout(() => { if (status.textContent === text) status.textContent = ""; }, STATUS_CLEAR_MS));
  }
}

function missionStatus(text, bad = false) {
  showStatus("mission-status", "mission-status", text, bad);
}

async function postJson(path, body) {
  const response = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    // an empty or non-JSON body still has a status code
  }
  return { ok: response.ok, status: response.status, payload };
}

// Posts a mission, with the coordinates of any map selection it names (P1, A1, ...); true when sent.
async function postMission(text) {
  if (stopped) return false;
  try {
    const { ok, status, payload } = await postJson("/task", { text: withSelections(text) });
    if (ok) {
      missionStatus(phoneView ? `Sent ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
        : `Sent at ${new Date().toLocaleTimeString()}; the AI's answer appears in the news feed.`);
    }
    else missionStatus(`Not sent (HTTP ${status}): ${payload.error || "unknown error"}`, true);
    return ok;
  } catch (error) {
    if (!(error instanceof Unauthorized)) missionStatus(`Not sent: ${error.message}`, true);
    return false;
  }
}

async function sendMission(event) {
  event.preventDefault();
  const box = $("mission-text");
  const text = box.value.trim();
  if (!text || stopped) return;
  const send = $("send");
  send.disabled = true; // only while this request is on its way; the AI's turn shows in the feed
  try {
    if (await postMission(text)) box.value = "";
  } finally {
    send.disabled = false;
    if (!phoneView) box.focus(); // on a phone it would bring the keyboard back over the feed
  }
}

async function newMission() {
  if (stopped) return;
  if (!confirm("Start a new mission? The AI forgets this conversation; the drone keeps doing what it does.")) return;
  const button = $("new-mission");
  button.disabled = true;
  try {
    const { ok, status, payload } = await postJson("/reset");
    if (ok) missionStatus("New mission: the AI starts a fresh conversation.");
    else missionStatus(`New mission failed (HTTP ${status}): ${payload.error || "unknown error"}`, true);
  } catch (error) {
    if (!(error instanceof Unauthorized)) missionStatus(`New mission failed: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

$("mission").addEventListener("submit", sendMission);
$("mission-text").addEventListener("keydown", (event) => {
  // Enter sends, Shift+Enter starts a new line.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("mission").requestSubmit();
  }
});
$("new-mission").addEventListener("click", newMission);

// --- Emergency buttons: POST /emergency, straight to the drone, never through the AI. -------
// They are never disabled: a running mission, a lost feed or a pending press must not stop the
// next one. The service answers 202 at once; the outcome arrives on /events with the task id.
const ACTION_NAMES = { hold: "HOLD", land: "LAND", rth: "HOME" };
const pendingEmergencies = new Map(); // task id -> action

function emergencyStatus(text, bad = false, sticky = false) {
  showStatus("emergency-status", "emergency-status", text, bad, sticky);
}

// The feed carries each press's outcome. If it drops while a press is unconfirmed, say so: the
// press may or may not have reached the drone. A late "executed" from the same run still clears it.
function warnUnconfirmedEmergencies() {
  if (!pendingEmergencies.size) return;
  const names = [...new Set(pendingEmergencies.values())].map((action) => ACTION_NAMES[action]).join(", ");
  emergencyStatus(`${names} NOT CONFIRMED: lost the AI pilot service - check the drone, use the RC transmitter`, true);
}

function noteEmergencyResult(event) {
  if (event.kind !== "emergency" || !pendingEmergencies.has(event.task_id)) return;
  const name = ACTION_NAMES[pendingEmergencies.get(event.task_id)];
  const when = new Date().toLocaleTimeString();
  if (event.status === "executed") {
    pendingEmergencies.delete(event.task_id);
    emergencyStatus(`${name} done at ${when}`);
  } else if (event.status === "error") {
    pendingEmergencies.delete(event.task_id);
    emergencyStatus(`${name} FAILED: ${event.error || "unknown error"} - press again`, true);
  }
}

async function sendEmergency(action) {
  const name = ACTION_NAMES[action];
  emergencyStatus(`${name} sending…`, false, true);
  try {
    const { ok, status, payload } = await postJson("/emergency", { action });
    if (ok) {
      pendingEmergencies.set(payload.task_id, action);
      emergencyStatus(`${name} sent at ${new Date().toLocaleTimeString()}, waiting for the drone`, false, true);
    } else {
      emergencyStatus(`${name} NOT SENT (HTTP ${status}): ${payload.error || "unknown error"}`, true);
    }
  } catch (error) {
    const reason = error instanceof Unauthorized ? "unauthorized" : "AI pilot service unreachable";
    emergencyStatus(`${name} NOT SENT: ${reason} - use the RC transmitter`, true);
  }
}

for (const button of document.querySelectorAll(".emergency .stop")) {
  button.addEventListener("click", () => sendEmergency(button.dataset.action));
}

// --- Display polls: one request at a time per panel, paused while the tab is hidden. ---------
// They read the drone board through the service's passive /api/* reads, so an open page never
// counts as AI contact and never keeps the hold-then-home failsafe from firing.
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function poll(path, intervalMs, onData, onError) {
  while (!stopped) {
    if (document.hidden) {
      await sleep(intervalMs);
      continue;
    }
    try {
      const response = await api(path);
      let payload = {};
      try {
        payload = await response.json();
      } catch {
        payload = {};
      }
      if (response.ok) onData(payload);
      else onError(payload.error || `HTTP ${response.status}`);
    } catch (error) {
      if (error instanceof Unauthorized) return;
      onError("AI pilot service unreachable");
    }
    await sleep(intervalMs);
  }
}

// --- Camera: the numbered picture and its candidate list (labels are untrusted text). -------
const STALE_AFTER_S = 2;

function showFrame(frame) {
  const image = $("camera-image");
  image.src = `data:${frame.mime_type || "image/jpeg"};base64,${frame.image_base64}`;
  image.hidden = false;
  $("camera-empty").hidden = true;
  const age = Number(frame.observation_age_s);
  const caption = $("camera-caption");
  const ageText = Number.isFinite(age) ? `${age.toFixed(1)} s old` : "age unknown";
  caption.textContent = `frame ${frame.frame_id} · ${frame.width}×${frame.height} · ${ageText}`;
  caption.className = Number.isFinite(age) && age <= STALE_AFTER_S ? "camera-caption" : "camera-caption stale";
  const list = $("candidates");
  const items = (frame.candidate_table || []).map((candidate) => {
    const item = document.createElement("li");
    const id = document.createElement("span");
    id.className = "id";
    id.textContent = `#${candidate.id}`;
    const labels = (candidate.untrusted_detector_labels || []).join(" ");
    const confidence = Number(candidate.confidence);
    const detail = [labels, Number.isFinite(confidence) ? confidence.toFixed(2) : ""].filter(Boolean).join(" ");
    item.append(id, document.createTextNode(detail));
    return item;
  });
  if (!items.length) {
    const none = document.createElement("li");
    none.textContent = "no numbered objects in view";
    items.push(none);
  }
  list.replaceChildren(...items);
}

function showNoCamera(reason) {
  $("camera-image").hidden = true;
  const empty = $("camera-empty");
  empty.hidden = false;
  empty.className = "camera-empty bad";
  empty.textContent = `No camera: ${reason}`;
  $("camera-caption").textContent = "";
  $("candidates").replaceChildren();
}

// --- Drone state: what the drone itself reports, marking anything old or missing. ----------
const STATE_STALE_S = 2; // telemetry arrives at 20 Hz; older than this means it stopped coming
const LINK_STALE_S = 3; // ArduPilot sends a heartbeat every second

function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function setField(id, text, level = "") {
  const field = $(id);
  field.textContent = text;
  field.className = level;
}

function showState(state) {
  $("state-empty").hidden = true;
  $("state").hidden = false;
  const vehicle = state.vehicle || {};
  headingDeg = number(vehicle.heading_deg);
  const pose = vehicle.pose || {};
  const dataAge = number(state.observation_age_s);
  const stale = dataAge === null || dataAge > STATE_STALE_S ? "stale" : "";

  const up = number(pose.up_m);
  setField("s-height", up === null ? "unknown" : `${up.toFixed(2)} m above home`, up === null ? "bad" : stale);
  const ground = number((vehicle.ground_distance || {}).distance_m);
  setField("s-ground", ground === null ? "no rangefinder reading" : `${ground.toFixed(2)} m (rangefinder)`, stale);

  const percent = number(vehicle.battery_remaining_pct);
  const volts = number(vehicle.battery_voltage_v);
  const battery = [percent === null ? null : `${Math.round(percent)} %`, volts === null ? null : `${volts.toFixed(1)} V`]
    .filter(Boolean).join(" · ") || "unknown";
  setField("s-battery", battery, percent !== null && percent < 20 ? "bad" : stale);

  const armed = vehicle.armed === true ? "armed" : vehicle.armed === false ? "disarmed" : "armed state unknown";
  setField("s-mode", `${vehicle.mode || "unknown"} · ${armed}`, stale);
  const skill = state.active_skill ? ` (${state.active_skill})` : "";
  setField("s-executor", `${state.state || "unknown"}${skill}`, "");

  const heartbeat = number(vehicle.heartbeat_age_s);
  setField(
    "s-link",
    heartbeat === null ? "no heartbeat yet" : heartbeat > LINK_STALE_S
      ? `LOST: last heartbeat ${heartbeat.toFixed(1)} s ago` : `ok (heartbeat ${heartbeat.toFixed(1)} s ago)`,
    heartbeat === null || heartbeat > LINK_STALE_S ? "bad" : "",
  );
  setField("s-age", dataAge === null ? "unknown" : `${dataAge.toFixed(1)} s${stale ? " (stale)" : ""}`, stale);

  const override = $("s-override");
  let overrideText = "";
  if (state.pilot_override) {
    const reason = typeof state.pilot_override === "object"
      ? state.pilot_override.reason || state.pilot_override.mode || "" : String(state.pilot_override);
    override.textContent = `A pilot or autopilot failsafe has control${reason ? ` (${reason})` : ""}: HORUS flight commands are refused.`;
    override.hidden = false;
    overrideText = `Pilot or failsafe has control${reason ? ` (${reason})` : ""}: HORUS flight commands refused`;
  } else {
    override.hidden = true;
  }

  const operator = $("s-operator");
  let operatorText = "";
  if (state.operator_lock) {
    operator.textContent = `Operator ${String(state.operator_lock).toUpperCase()} in effect: the AI's flight commands are refused until your next mission.`;
    operator.hidden = false;
    const lockName = ACTION_NAMES[state.operator_lock] || String(state.operator_lock).toUpperCase();
    operatorText = `Your ${lockName} holds: AI locked until your next mission`;
  } else {
    operator.hidden = true;
  }

  if (phoneView) {
    const shortArmed = vehicle.armed === true ? "armed" : vehicle.armed === false ? "disarmed" : "armed ?";
    const linkLost = heartbeat === null || heartbeat > LINK_STALE_S;
    showPhoneState([
      [up === null ? "height ?" : `${up.toFixed(1)} m`, up === null ? "bad" : stale],
      [`${vehicle.mode || "mode ?"} ${shortArmed}`, stale],
      [percent === null ? "battery ?" : `${Math.round(percent)} %`, percent !== null && percent < 20 ? "bad" : stale],
      [heartbeat === null ? "no link" : linkLost ? "link LOST" : "link ok", linkLost ? "bad" : ""],
      [`${state.state || "unknown"}${skill}`, ""],
    ], [operatorText, overrideText].filter(Boolean));
  }
}

// Phone view: the drone panel folds into one line at the top; the locks show in red below it.
function showPhoneState(parts, warnings) {
  $("phone-state").replaceChildren(...parts.map(([text, level]) => {
    const part = document.createElement("span");
    part.textContent = text;
    if (level) part.className = level;
    return part;
  }));
  $("phone-state").hidden = false;
  const lock = $("phone-lock");
  lock.replaceChildren(...warnings.map((text) => {
    const line = document.createElement("span");
    line.textContent = text;
    return line;
  }));
  lock.hidden = !warnings.length;
}

function showNoState(reason) {
  $("state").hidden = true;
  $("s-override").hidden = true;
  $("s-operator").hidden = true;
  const empty = $("state-empty");
  empty.hidden = false;
  empty.className = "state-empty bad";
  empty.textContent = `No drone data: ${reason}`;
  if (phoneView) showPhoneState([[`No drone data: ${reason}`, "bad"]], []);
}

// --- Map: drone, home (the local origin), trail, numbered objects, the street map when the --
// --- world has one (GTA), and the operator's map selections. North is up; distances are ----
// --- metres in the drone's local frame. Drag, pinch, the wheel and the +/- buttons move the -
// --- view like a street-map app; the centre button follows the drone again. ----------------
const MAP_INTERVAL_MS = 2000;
const MAP_RADIUS_M = 80; // street-map worlds: the default view, this far around the drone
const MIN_RADIUS_M = 3;
const MAX_RADIUS_M = 2000; // worlds without a street map
const TRAIL_POINTS = 150;
const MAX_PLACE_DOTS = 400; // dots drawn at most, for speed: the GTA map has 1500 places
// How much shows at each zoom, in metres per screen pixel, so a larger screen shows more of the
// same detail rather than more detail. A phone map is about 390 px wide: 0.3 m/px there is a
// radius of about 60 m, 1 m/px about 200 m.
const SMALL_PLACES_MPP = 0.3; // seats, ATMs, traffic lights, crossings, bus stops: dots only
const SMALL_LABELS_MPP = 0.25; // plus the names of ATMs and bus stops
const CROSSROADS_MPP = 0.8; // named crossroads ("Atlee St / Strawberry Ave") and other named places
const STREET_NAMES_MPP = 1.1; // street names along the roads
const DISTRICT_NAMES_MPP = 0.7; // district names, from this far out
// Street-map tiles (/api/map/index, /api/map/tile): each zoom draws one level of tiles, picked by
// metres per screen pixel so a screen needs few of them: level 0 (250 m tiles, every detail) up
// to TILE_LEVEL_MPP[0], level 1 (1 km tiles, simplified) up to TILE_LEVEL_MPP[1], level 2 (4 km
// tiles, main roads) beyond. A phone map 390 px wide shows 390 m at 1 m/px, 1.6 km at 4 m/px.
const TILE_LEVEL_MPP = [1, 4];
const TILE_FETCHES = 4; // tile requests at a time, for the view and for the whole-map download each
const TILE_RETRY_MS = 15000; // after a failed tile request
const TILE_CAP_BYTES = 2e6; // tiles kept from browsing (download size); least recently used go first
const TILE_CAP_KEY = "horus.tileCapBytes"; // localStorage override of that cap in bytes, for testing
const MEMORY_TILE_CHARS = 8e6; // decoded tiles kept in memory, counted in characters of JSON (about 60 close-in tiles)
const TOUCH_SAVE_MS = 60000; // a stored tile's last use is written back at most this often
// Roads are drawn about as wide as they are, within these pixel limits.
const ROAD_STYLES = {
  freeway: { metres: 12, min: 2.5, max: 40 },
  road: { metres: 9, min: 1.5, max: 30 },
};
const MAP_COLOURS = {
  light: {
    walkable: "rgba(31,157,85,0.12)", pavement: "rgba(255,255,255,0.4)", obstacle: "rgba(127,127,127,0.45)", path: "rgba(110,110,110,0.7)",
    roadCasing: "#c2c2c2", road: "#ffffff", freewayCasing: "#c98a1c", freeway: "#f7c562",
    text: "#2b2b2b", halo: "rgba(255,255,255,0.9)", district: "rgba(60,60,60,0.42)",
    seat: "#9a9a9a", signal: "#c98a1c", crossing: "#8a8a8a", poi: "#2f7fd8", junction: "#555555",
  },
  dark: {
    walkable: "rgba(60,190,110,0.12)", pavement: "rgba(255,255,255,0.05)", obstacle: "rgba(160,160,160,0.35)", path: "rgba(170,170,170,0.7)",
    roadCasing: "#161616", road: "#555a60", freewayCasing: "#2e2209", freeway: "#a87a26",
    text: "#ececec", halo: "rgba(20,20,20,0.85)", district: "rgba(230,230,230,0.4)",
    seat: "#8c8c8c", signal: "#e0a43a", crossing: "#a8a8a8", poi: "#6aacff", junction: "#c8c8c8",
  },
};
const LONG_PRESS_MS = 550;
const DOUBLE_TAP_MS = 300;
const TAP_SLOP_PX = 8;
const HANDLE_PX = 22; // how near a finger must land to grab a point or a corner
const SELECTION_COLOUR = "#0aa3c2";
const SCALE_BARS_M = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
const trail = [];
let headingDeg = null; // from the state poll and the live map
let lastMap = null; // { local, area }: the newest map data
let frame = null; // the view last drawn: { centre, radius, scale, width, height, streetMap }
let drawQueued = false;
// follow: re-centred on every update, on the drone (street map) or fitted around home, the
// drone, its trail, objects and selections (no street map). A drag or pinch stops it; the centre
// button starts it again. radius: half the map's shorter side in metres; null = the default.
const view = { follow: true, centre: null, radius: null };
// The operator's map selections, named in missions as P1, A1, ...: points and areas (corner
// lists, polygon or rectangle), in [north_m, east_m] from home, the frame goto takes.
// They survive a reload of the page (sessionStorage), e.g. when a new version loads itself.
const SELECTIONS_KEY = "horus.selections";
const { selections, nextNumber } = (() => {
  try {
    const kept = JSON.parse(sessionStorage.getItem(SELECTIONS_KEY) || "null");
    if (kept && Array.isArray(kept.selections) && kept.nextNumber) return kept;
  } catch {
    // a damaged entry starts empty
  }
  return { selections: [], nextNumber: { point: 1, area: 1 } };
})();
function keepSelections() {
  try {
    sessionStorage.setItem(SELECTIONS_KEY, JSON.stringify({ selections, nextNumber }));
  } catch {
    // storage full or blocked: selections stay in memory only
  }
}
let selectedId = null;
let tool = null; // "point" | "polygon" | "rectangle" while placing one
let draft = []; // the polygon's corners so far, or the rectangle drag's two corners

// The zoom-out limit is the current frame's: a street map zooms out until all of it fits.
const clampRadius = (radius, max = frame ? frame.maxRadius : MAX_RADIUS_M) => Math.min(max, Math.max(MIN_RADIUS_M, radius));
const metres = (value) => (Math.abs(value) < 0.05 ? 0 : value).toFixed(1); // never "-0.0"
const apart = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
const toXY = ([n, e], f = frame) => [f.width / 2 + (e - f.centre[1]) * f.scale, f.height / 2 - (n - f.centre[0]) * f.scale];
const toNE = ([x, y], f = frame) => [f.centre[0] - (y - f.height / 2) / f.scale, f.centre[1] + (x - f.width / 2) / f.scale];

function untrustedText(quoted) {
  // Map names arrive JSON-quoted as untrusted text; canvas text cannot run as markup.
  try {
    const value = JSON.parse(quoted);
    return typeof value === "string" ? value : String(quoted ?? "");
  } catch {
    return String(quoted ?? "");
  }
}

function finitePoint(pose) {
  const n = number(pose && pose.north_m);
  const e = number(pose && pose.east_m);
  return n === null || e === null ? null : [n, e];
}

function placedCandidates(local, drone) {
  // A candidate has a ground position only when the detector gave a distance and a bearing
  // (radians, positive to image-right). The camera looks along the heading turned by the
  // gimbal's measured pan (gimbal_yaw_deg; none reported: it faces the nose).
  const placed = [];
  let unplaced = 0;
  const pan = number(local.gimbal_yaw_deg) ?? 0;
  for (const candidate of local.candidates || []) {
    const distance = number(candidate.distance_m);
    const bearing = number(candidate.bearing);
    if (!drone || distance === null || bearing === null || headingDeg === null) {
      unplaced += 1;
      continue;
    }
    const angle = ((headingDeg + pan) * Math.PI) / 180 + bearing;
    placed.push({ id: candidate.id, n: drone[0] + distance * Math.cos(angle), e: drone[1] + distance * Math.sin(angle) });
  }
  return { placed, unplaced };
}

function updateMap(map) {
  lastMap = map;
  const drone = finitePoint((map.local || {}).vehicle_pose);
  if (drone) {
    const last = trail[trail.length - 1];
    if (!last || apart(drone, last) > 0.05) trail.push(drone);
    if (trail.length > TRAIL_POINTS) trail.shift();
  }
  drawMap();
}

function scheduleDraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => {
    drawQueued = false;
    drawMap();
  });
}

// Each outline's bounding box, kept while its tile is in memory, so drawing skips whatever is
// off screen.
const outlineBoxes = new WeakMap();
function boxOf(points) {
  let box = outlineBoxes.get(points);
  if (!box) {
    box = [Infinity, Infinity, -Infinity, -Infinity];
    for (const [n, e] of points) {
      box = [Math.min(box[0], n), Math.min(box[1], e), Math.max(box[2], n), Math.max(box[3], e)];
    }
    outlineBoxes.set(points, box);
  }
  return box;
}

// bounds: [min_n, min_e, max_n, max_e] of the street map (null: no street map).
function viewFrame(width, height, drone, streetMap, bounds, placed) {
  let centre = view.centre || drone || [0, 0];
  let radius = view.radius;
  if (view.follow && streetMap) {
    centre = drone || [0, 0];
  } else if (view.follow) {
    const points = [[0, 0], ...trail, ...placed.map((c) => [c.n, c.e]), ...selections.flatMap((s) => s.points)];
    if (drone) points.push(drone);
    const ns = points.map((p) => p[0]);
    const es = points.map((p) => p[1]);
    centre = [(Math.min(...ns) + Math.max(...ns)) / 2, (Math.min(...es) + Math.max(...es)) / 2];
    // A few metres of indoor flight stay clearly visible.
    if (radius === null) radius = Math.max(4, 0.6 * Math.max(Math.max(...ns) - Math.min(...ns), Math.max(...es) - Math.min(...es)) + 1.5);
  }
  // A street map zooms out until the whole of it fits the screen; a map panned by hand keeps the
  // screen inside it (centred on it across a side the screen is wider than). Following, the
  // drone stays centred even past its edge.
  const fit = bounds ? 1.04 * Math.max((bounds[3] - bounds[1]) / width, (bounds[2] - bounds[0]) / height) : 0;
  const maxRadius = bounds ? Math.max(MIN_RADIUS_M, (fit * Math.min(width, height)) / 2) : MAX_RADIUS_M;
  radius = clampRadius(radius === null ? MAP_RADIUS_M : radius, maxRadius);
  const scale = Math.min(width, height) / 2 / radius;
  if (bounds && !view.follow) {
    const inside = (value, low, high, half) => (high - low <= 2 * half ? (low + high) / 2 : Math.min(high - half, Math.max(low + half, value)));
    centre = [inside(centre[0], bounds[0], bounds[2], height / 2 / scale), inside(centre[1], bounds[1], bounds[3], width / 2 / scale)];
    view.centre = centre;
    view.radius = radius;
  }
  return { centre, radius, maxRadius, scale, width, height, streetMap };
}

function drawMap() {
  if (!lastMap) return;
  const canvas = $("map");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  // Resizing clears and reallocates the bitmap: only when the size really changed.
  if (canvas.width !== Math.round(width * ratio)) canvas.width = Math.round(width * ratio);
  if (canvas.height !== Math.round(height * ratio)) canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const ink = getComputedStyle(document.body).color;
  const colours = MAP_COLOURS[window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"];

  const local = lastMap.local || {};
  const area = lastMap.area || null;
  const drone = finitePoint(local.vehicle_pose);
  const { placed, unplaced } = placedCandidates(local, drone);
  const streetMap = Boolean(mapIndex && mapIndex.order.length);
  frame = viewFrame(width, height, drone, streetMap, streetMap ? mapIndex.bounds : null, placed);
  const { scale } = frame;
  const reach = [width / 2 / scale + 10 / scale, height / 2 / scale + 10 / scale];
  const sight = {
    minN: frame.centre[0] - reach[1], maxN: frame.centre[0] + reach[1],
    minE: frame.centre[1] - reach[0], maxE: frame.centre[1] + reach[0],
  };
  const onScreen = (points) => {
    if (!Array.isArray(points) || !points.length) return false;
    const box = boxOf(points);
    return box[0] <= sight.maxN && box[2] >= sight.minN && box[1] <= sight.maxE && box[3] >= sight.minE;
  };
  const trace = (points, close) => {
    ctx.beginPath();
    points.forEach((point, index) => {
      const [x, y] = toXY(point);
      if (index) ctx.lineTo(x, y);
      else ctx.moveTo(x, y);
    });
    if (close) ctx.closePath();
  };
  const polygon = (outline, fill, stroke) => {
    if (!outline || outline.length < 3 || !onScreen(outline)) return;
    trace(outline, true);
    if (fill) { ctx.fillStyle = fill; ctx.fill(); }
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke(); }
  };

  const keepClear = [[0, 0], ...(drone ? [drone] : [])]; // home and the drone stay unlabelled
  const shownTiles = streetMap ? viewTiles(frame) : null;
  if (shownTiles) drawStreetMap(ctx, shownTiles, colours, { sight, onScreen, polygon, keepClear });

  // The drone's own marks (mark_place), in every world.
  ctx.font = "11px system-ui, sans-serif";
  for (const mark of ((area && area.own_map) || {}).marks || []) {
    const [x, y] = toXY([mark.north_m, mark.east_m]);
    ctx.fillStyle = "#c39bff";
    ctx.fillRect(x - 3, y - 3, 6, 6);
    ctx.fillStyle = ink;
    ctx.fillText(untrustedText(mark.name), x + 5, y - 4);
  }

  drawSelections(ctx, trace);

  // Home: the local origin, where the drone armed.
  const [hx, hy] = toXY([0, 0]);
  ctx.strokeStyle = "#1f9d55";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(hx, hy, 7, 0, 2 * Math.PI);
  ctx.stroke();
  ctx.fillStyle = "#1f9d55";
  ctx.font = "bold 11px system-ui, sans-serif";
  ctx.fillText("H", hx - 4, hy + 4);

  if (trail.length > 1) {
    ctx.strokeStyle = "rgba(47,127,216,0.6)";
    ctx.lineWidth = 2;
    trace(trail, false);
    ctx.stroke();
  }

  ctx.font = "bold 11px system-ui, sans-serif";
  for (const candidate of placed) {
    const [x, y] = toXY([candidate.n, candidate.e]);
    ctx.fillStyle = "#d64545";
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, 2 * Math.PI);
    ctx.fill();
    ctx.fillText(`#${candidate.id}`, x + 6, y + 4);
  }

  if (drone) {
    const [x, y] = toXY(drone);
    ctx.save();
    ctx.translate(x, y);
    ctx.fillStyle = "#2f7fd8";
    if (headingDeg === null) {
      ctx.beginPath();
      ctx.arc(0, 0, 6, 0, 2 * Math.PI);
      ctx.fill();
    } else {
      ctx.rotate((headingDeg * Math.PI) / 180); // heading 0 = north (up), clockwise
      ctx.beginPath();
      ctx.moveTo(0, -11);
      ctx.lineTo(7, 8);
      ctx.lineTo(0, 4);
      ctx.lineTo(-7, 8);
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  // Scale bar and north arrow, haloed so they stay readable over roads.
  const barMetres = SCALE_BARS_M.find((m) => m * scale >= width / 6) || SCALE_BARS_M[SCALE_BARS_M.length - 1];
  for (const [lineWidth, colour] of [[4, colours.halo], [2, ink]]) {
    ctx.strokeStyle = colour;
    ctx.lineWidth = lineWidth;
    ctx.beginPath();
    ctx.moveTo(10, height - 12);
    ctx.lineTo(10 + barMetres * scale, height - 12);
    ctx.stroke();
  }
  ctx.font = "11px system-ui, sans-serif";
  haloText(ctx, `${barMetres} m`, 12, height - 17, ink, colours.halo);
  haloText(ctx, "N ↑", width - 30, 16, ink, colours.halo);

  $("map-centre").setAttribute("aria-pressed", String(view.follow));
  const caption = $("map-caption");
  const where = drone ? `drone ${metres(drone[0])} m N, ${metres(drone[1])} m E of home`
    : liveProblem ? `no live data: ${liveProblem}` : "drone position unknown";
  const source = streetMap ? `street map: ${mapIndex.provider}${tileNote(shownTiles)}`
    : mapIndex ? "no street map in this world" : `street map not loaded${indexProblem ? `: ${indexProblem}` : ""}`;
  const hidden = unplaced ? ` · ${unplaced} numbered object(s) without a distance, not drawn` : "";
  caption.textContent = `${where} · ${source}${hidden}`;
  caption.className = drone ? "map-caption" : "map-caption bad";
}

// --- Street map: what drawing needs from a tile (road kinds, straight stretches for street ---
// --- names, place dots and labels) is worked out once when the tile is decoded; the tiles on --
// --- screen merge into one scene (each feature once per level) when that set changes, not ----
// --- every frame; each frame then draws only what is on screen at its zoom. ------------------
const textWidths = new Map(); // "font|text" -> pixels
const NAME_STEP_M = 60; // a street name may sit every this many metres along a straight stretch
// Place kinds that are small things: only shown zoomed in, as a dot of this style; ATMs and bus
// stops are also named when very close in. Junctions here are on one street only.
const SMALL_PLACES = { seat: "seat", atm: "poi", bus_stop: "poi", traffic_light: "signal", crossing: "crossing", junction: "junction" };
const DOT_SIZES = { poi: 3.4, junction: 2.8, signal: 2.6, crossing: 2.2, seat: 2 }; // most important first
const DOT_ORDER = Object.keys(DOT_SIZES);
const STREET_FONT = "600 11px system-ui, sans-serif";
const PLACE_FONT = "11px system-ui, sans-serif";
const DISTRICT_FONT = "600 12px system-ui, sans-serif";

// Straight stretches of a way (turning less than about 10 degrees), where a name can lie along
// the road, with the spots a name may be centred on and how far each is from the stretch's ends.
function straightRuns(name, points) {
  const runs = [];
  const bearing = (a, b) => Math.atan2(b[1] - a[1], b[0] - a[0]);
  let start = 0;
  while (start < points.length - 1) {
    const first = bearing(points[start], points[start + 1]);
    let end = start + 1;
    while (end + 1 < points.length) {
      const turn = Math.abs(((bearing(points[end], points[end + 1]) - first + 3 * Math.PI) % (2 * Math.PI)) - Math.PI);
      if (turn > 0.18) break;
      end += 1;
    }
    const a = points[start];
    const b = points[end];
    const length = apart(a, b);
    start = end;
    if (length < 15) continue;
    const along = [(b[0] - a[0]) / length, (b[1] - a[1]) / length];
    const spots = [length / 2];
    for (let step = NAME_STEP_M; step < length / 2; step += NAME_STEP_M) spots.push(length / 2 - step, length / 2 + step);
    // Screen direction (east is +x, north is -y), turned so the text never reads upside down.
    let angle = Math.atan2(-along[0], along[1]);
    if (angle > Math.PI / 2) angle -= Math.PI;
    if (angle < -Math.PI / 2) angle += Math.PI;
    runs.push({
      name, length, angle, ends: [a, b],
      anchors: spots.map((t) => ({ at: [a[0] + along[0] * t, a[1] + along[1] * t], room: Math.min(t, length - t) })),
    });
  }
  return runs;
}

// District rectangles from every tile seen (a district is several rectangles, often in several
// tiles): one name, at the middle of all of them, or, when that middle lies outside them (the
// ocean around the land), at the middle of the largest.
const districtParts = new Map(); // area id -> { name, size, at, outline }
let districtLabels = null; // [{ text, at }], biggest district first; null = work out again

// A fixed scattered order for places, so the dot budget and label room spread over the screen.
function scatter([n, e]) {
  const value = Math.sin(n * 12.9898 + e * 78.233) * 43758.5453;
  return value - Math.floor(value);
}

function prepTile(key, data) {
  const level = Number(key.split("/")[0]);
  const tile = { key, level, ways: [], walkable: [], outlines: [], obstacles: [], places: [] };
  const idOf = (item, index, kind) => `${kind}:${typeof item.id === "string" || typeof item.id === "number" ? item.id : `${key}#${index}`}`;
  (data.ways || []).forEach((way, index) => {
    if (!Array.isArray(way.points) || way.points.length < 2) return;
    const kind = untrustedText(way.kind);
    const path = /foot|path|cross|pedestrian|steps/i.test(kind);
    const name = path || !way.name ? "" : untrustedText(way.name).trim();
    tile.ways.push({
      id: idOf(way, index, "w"), kind: path ? "paths" : /freeway|motorway/i.test(kind) ? "freeways" : "roads",
      points: way.points, runs: name ? straightRuns(name, way.points) : [],
    });
  });
  // Where people walk: pavements beside the roads, and footpaths and squares (green).
  (data.walkable || []).forEach((walk, index) => {
    if (!Array.isArray(walk.outline)) return;
    tile.walkable.push({ id: idOf(walk, index, "g"), outline: walk.outline, colour: untrustedText(walk.kind) === "road" ? "pavement" : "walkable" });
  });
  (data.obstacles || []).forEach((obstacle, index) => {
    if (Array.isArray(obstacle.outline)) tile.obstacles.push({ id: idOf(obstacle, index, "o"), outline: obstacle.outline });
  });
  // Districts get a name and no outline; other named areas keep a faint outline.
  (data.areas || []).forEach((part, index) => {
    if (!Array.isArray(part.outline) || part.outline.length < 3) return;
    const id = idOf(part, index, "a");
    if (untrustedText(part.kind) !== "district") {
      tile.outlines.push({ id, outline: part.outline });
      return;
    }
    const name = part.name ? untrustedText(part.name).trim() : "";
    const size = squareMetres(part.outline);
    if (!name || !size || districtParts.has(id)) return;
    districtParts.set(id, { name, size, at: centroid(part.outline), outline: part.outline });
    districtLabels = null;
  });
  // Crossroads of two named streets ("Atlee St / Strawberry Ave") and other named places show
  // from mid zoom with their name. Seats, ATMs, traffic lights, crossings and junctions along one
  // street are small things: dots, only zoomed in.
  (data.places || []).forEach((place, index) => {
    const n = number(place.north_m);
    const e = number(place.east_m);
    if (n === null || e === null) return;
    const kind = untrustedText(place.kind).toLowerCase();
    const name = untrustedText(place.name).trim();
    const small = Object.hasOwn(SMALL_PLACES, kind) && !(kind === "junction" && name.includes(" / "));
    const style = small ? SMALL_PLACES[kind] : kind === "junction" ? "junction" : "poi";
    tile.places.push({
      id: idOf(place, index, "p"), at: [n, e], style, order: scatter([n, e]),
      dotMpp: small ? SMALL_PLACES_MPP : CROSSROADS_MPP,
      label: !small || style === "poi" ? name : "",
      labelMpp: small ? SMALL_LABELS_MPP : CROSSROADS_MPP,
    });
  });
  return tile;
}

// The tiles drawn together as one scene: every feature once per level (a feature crossing a tile
// edge is in both tiles), roads by kind, street-name stretches longest first, places most
// important first. The newest few scenes are kept, so most frames only look one up.
const SCENES_KEPT = 4;
const scenes = new Map(); // tile keys -> scene
function sceneOf(list) {
  const key = list.map((tile) => tile.key).join(" ");
  let scene = scenes.get(key);
  if (scene) return scene;
  scene = { freeways: [], roads: [], paths: [], walkable: [], outlines: [], obstacles: [], runs: [], places: [] };
  const seen = new Set();
  const first = (tile, item) => {
    const id = `${tile.level}:${item.id}`;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  };
  for (const tile of list) {
    for (const way of tile.ways) {
      if (!first(tile, way)) continue;
      scene[way.kind].push(way.points);
      scene.runs.push(...way.runs);
    }
    for (const name of ["walkable", "outlines", "obstacles", "places"]) {
      for (const item of tile[name]) if (first(tile, item)) scene[name].push(item);
    }
  }
  scene.runs.sort((a, b) => b.length - a.length); // the longest stretch of a street carries its name
  const rank = (place) => DOT_ORDER.indexOf(place.style);
  scene.places.sort((a, b) => b.dotMpp - a.dotMpp || rank(a) - rank(b) || a.order - b.order);
  scenes.set(key, scene);
  for (const old of scenes.keys()) {
    if (scenes.size <= SCENES_KEPT) break;
    scenes.delete(old);
  }
  return scene;
}

function districtNames() {
  if (districtLabels) return districtLabels;
  const sums = new Map();
  for (const part of districtParts.values()) {
    const sum = sums.get(part.name) || { text: part.name.toUpperCase(), n: 0, e: 0, size: 0, parts: [] };
    sum.n += part.at[0] * part.size;
    sum.e += part.at[1] * part.size;
    sum.size += part.size;
    sum.parts.push(part);
    sums.set(part.name, sum);
  }
  districtLabels = [...sums.values()]
    .sort((a, b) => b.size - a.size)
    .map((d) => {
      const middle = [d.n / d.size, d.e / d.size];
      if (d.parts.some((part) => insideArea(middle, part.outline))) return { text: d.text, at: middle };
      return { text: d.text, at: d.parts.reduce((big, part) => (part.size > big.size ? part : big)).at };
    });
  return districtLabels;
}

function textWidth(ctx, text) {
  const key = `${ctx.font}|${text}`;
  let width = textWidths.get(key);
  if (width === undefined) {
    width = ctx.measureText(text).width;
    textWidths.set(key, width);
  }
  return width;
}

function haloText(ctx, text, x, y, colour, halo) {
  ctx.lineWidth = 3;
  ctx.strokeStyle = halo;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = colour;
  ctx.fillText(text, x, y);
}

// The tiles on screen; a tile still loading shows another level's tile meanwhile, clipped to
// its square, so the map never goes blank (lines only: names come with the tile itself).
function drawStreetMap(ctx, shown, colours, { sight, onScreen, polygon, keepClear }) {
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  if (shown.under.length) {
    ctx.save();
    ctx.beginPath();
    for (const [x, y] of shown.gaps) {
      const [left, top] = toXY([(y + 1) * shown.size, x * shown.size]);
      ctx.rect(left, top, shown.size * frame.scale, shown.size * frame.scale);
    }
    ctx.clip();
    drawStreetLines(ctx, sceneOf(shown.under), colours, onScreen, polygon);
    ctx.restore();
  }
  const scene = sceneOf(shown.tiles);
  drawStreetLines(ctx, scene, colours, onScreen, polygon);
  drawStreetLabels(ctx, scene, colours, { sight, onScreen, keepClear });
  ctx.restore();
}

function drawStreetLines(ctx, scene, colours, onScreen, polygon) {
  const { scale } = frame;
  for (const area of scene.outlines) polygon(area.outline, null, "rgba(127,127,127,0.35)");
  for (const walk of scene.walkable) polygon(walk.outline, colours[walk.colour], null);
  for (const obstacle of scene.obstacles) polygon(obstacle.outline, colours.obstacle, null);

  // Roads: one path per kind, every casing under every fill, so lanes that cross or run side by
  // side merge into one road; freeways go over roads, like bridges.
  const pathOf = (ways) => {
    const path = new Path2D();
    for (const points of ways) {
      if (!onScreen(points)) continue;
      points.forEach((point, index) => {
        const [x, y] = toXY(point);
        if (index) path.lineTo(x, y);
        else path.moveTo(x, y);
      });
    }
    return path;
  };
  const stroke = (path, lineWidth, colour) => {
    ctx.lineWidth = lineWidth;
    ctx.strokeStyle = colour;
    ctx.stroke(path);
  };
  const widthOf = ({ metres: wide, min, max }) => Math.min(max, Math.max(min, wide * scale));
  const casingOf = (lineWidth) => lineWidth + 2 * Math.min(1.5, Math.max(0.75, lineWidth * 0.15));
  if (scene.paths.length) {
    ctx.setLineDash([3, 3]);
    stroke(pathOf(scene.paths), 1, colours.path);
    ctx.setLineDash([]);
  }
  const roads = pathOf(scene.roads);
  const freeways = pathOf(scene.freeways);
  const roadWidth = widthOf(ROAD_STYLES.road);
  const freewayWidth = widthOf(ROAD_STYLES.freeway);
  stroke(roads, casingOf(roadWidth), colours.roadCasing);
  stroke(roads, roadWidth, colours.road);
  stroke(freeways, casingOf(freewayWidth), colours.freewayCasing);
  stroke(freeways, freewayWidth, colours.freeway);
}

function drawStreetLabels(ctx, scene, colours, { sight, onScreen, keepClear }) {
  const { scale, width, height } = frame;
  const mpp = 1 / scale;
  const onView = ([n, e]) => n >= sight.minN && n <= sight.maxN && e >= sight.minE && e <= sight.maxE;

  // Place dots for this zoom, at most MAX_PLACE_DOTS and one per few pixels, the least
  // important drawn first so the important ones end up on top.
  const shown = [];
  const cells = new Set();
  for (const place of mpp <= CROSSROADS_MPP ? scene.places : []) {
    if (mpp > place.dotMpp || !onView(place.at)) continue;
    const [x, y] = toXY(place.at);
    const cell = `${Math.round(x / 5)},${Math.round(y / 5)}`;
    if (cells.has(cell)) continue;
    cells.add(cell);
    shown.push({ place, x, y });
    if (shown.length >= MAX_PLACE_DOTS) break;
  }
  for (let index = shown.length - 1; index >= 0; index -= 1) {
    const { place, x, y } = shown[index];
    ctx.fillStyle = colours[place.style];
    ctx.beginPath();
    ctx.arc(x, y, DOT_SIZES[place.style], 0, 2 * Math.PI);
    ctx.fill();
  }

  // Labels: districts, then street names, then places; one that would overlap a label already
  // drawn, the map's buttons, the scale bar or the screen edge is left out.
  const canvasBox = ctx.canvas.getBoundingClientRect();
  const buttons = document.querySelector(".map-view").getBoundingClientRect();
  const taken = [
    [buttons.left - canvasBox.left - 4, buttons.top - canvasBox.top - 4, buttons.right - canvasBox.left + 4, buttons.bottom - canvasBox.top + 4],
    [0, height - 32, 130, height], // scale bar
    [width - 40, 0, width, 24], // north arrow
    ...keepClear.map((point) => {
      const [x, y] = toXY(point);
      return [x - 14, y - 14, x + 14, y + 14];
    }),
  ];
  const fits = (boxes) => boxes.every((b) => b[0] >= 2 && b[1] >= 2 && b[2] <= width - 2 && b[3] <= height - 2
    && !taken.some((t) => b[0] < t[2] && t[0] < b[2] && b[1] < t[3] && t[1] < b[3]));
  ctx.textBaseline = "middle";

  if (mpp >= DISTRICT_NAMES_MPP) {
    ctx.font = DISTRICT_FONT;
    ctx.letterSpacing = "2px";
    ctx.textAlign = "center";
    for (const district of districtNames()) {
      if (!onView(district.at)) continue;
      const [x, y] = toXY(district.at);
      const half = textWidth(ctx, district.text) / 2 + 3;
      const box = [x - half, y - 9, x + half, y + 9];
      if (!fits([box])) continue;
      taken.push(box);
      haloText(ctx, district.text, x, y, colours.district, colours.halo);
    }
    ctx.letterSpacing = "0px";
  }

  if (mpp <= STREET_NAMES_MPP) {
    // Each name once, on the longest straight stretch where it fits, along the road.
    ctx.font = STREET_FONT;
    ctx.textAlign = "center";
    const named = new Set();
    for (const run of scene.runs) {
      if (run.length * scale < 40) break; // the rest are shorter still
      if (named.has(run.name) || !onScreen(run.ends)) continue;
      const halfText = textWidth(ctx, run.name) / 2;
      const halfMetres = (halfText + 6) * mpp;
      if (run.length < 2 * halfMetres) continue;
      const [cos, sin] = [Math.cos(run.angle), Math.sin(run.angle)];
      for (const anchor of run.anchors) {
        if (anchor.room < halfMetres) continue;
        const [x, y] = toXY(anchor.at);
        // The rotated label as a row of small boxes, so a slanted name does not claim a whole
        // square around it; a few pixels of room at each end keep names apart.
        const halfBox = halfText + 6;
        const pieces = Math.max(1, Math.ceil((2 * halfBox) / 14));
        const piece = halfBox / pieces;
        const boxes = [];
        for (let index = 0; index < pieces; index += 1) {
          const t = -halfBox + (2 * index + 1) * piece;
          const [cx, cy] = [x + cos * t, y + sin * t];
          const [hx, hy] = [piece * Math.abs(cos) + 7 * Math.abs(sin), piece * Math.abs(sin) + 7 * Math.abs(cos)];
          boxes.push([cx - hx, cy - hy, cx + hx, cy + hy]);
        }
        if (!fits(boxes)) continue;
        taken.push(...boxes);
        named.add(run.name);
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(run.angle);
        haloText(ctx, run.name, 0, 0, colours.text, colours.halo);
        ctx.restore();
        break;
      }
    }
  }

  // Place names right of their dot, else left of it; each name once.
  ctx.font = PLACE_FONT;
  ctx.textAlign = "left";
  const labelled = new Set();
  for (const { place, x, y } of shown) {
    if (!place.label || mpp > place.labelMpp || labelled.has(place.label)) continue;
    const wide = textWidth(ctx, place.label);
    for (const left of [x + 6, x - 6 - wide]) {
      const box = [left - 2, y - 8, left + wide + 2, y + 8];
      if (!fits([box])) continue;
      taken.push(box);
      labelled.add(place.label);
      haloText(ctx, place.label, left, y, colours.text, colours.halo);
      break;
    }
  }
}

function drawSelections(ctx, trace) {
  ctx.font = "bold 12px system-ui, sans-serif";
  ctx.lineJoin = "round";
  for (const selection of selections) {
    const chosen = selection.id === selectedId;
    ctx.strokeStyle = SELECTION_COLOUR;
    ctx.fillStyle = SELECTION_COLOUR;
    if (selection.kind === "area") {
      trace(selection.points, true);
      ctx.globalAlpha = chosen ? 0.28 : 0.15;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.lineWidth = chosen ? 3 : 2;
      ctx.stroke();
      const [x, y] = toXY(centroid(selection.points));
      ctx.fillText(selection.id, x - ctx.measureText(selection.id).width / 2, y + 4);
    } else {
      const [x, y] = toXY(selection.points[0]);
      ctx.beginPath();
      ctx.arc(x, y, chosen ? 8 : 6, 0, 2 * Math.PI);
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.fillStyle = SELECTION_COLOUR;
      ctx.fillText(selection.id, x + 10, y - 7);
    }
  }
  // The area being drawn.
  if ((tool === "polygon" && draft.length) || (tool === "rectangle" && draft.length === 2)) {
    const corners = tool === "rectangle" ? rectangleCorners(draft) : draft;
    ctx.strokeStyle = SELECTION_COLOUR;
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 4]);
    trace(corners, corners.length >= 3);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  // Grab handles: the selected selection's point or corners, or the polygon's corners so far.
  for (const handle of mapHandles()) {
    const [x, y] = toXY(handle.points[handle.index]);
    ctx.beginPath();
    ctx.arc(x, y, 7, 0, 2 * Math.PI);
    ctx.fillStyle = "#fff";
    ctx.fill();
    ctx.strokeStyle = SELECTION_COLOUR;
    ctx.lineWidth = handle.index === 0 && tool === "polygon" && draft.length >= 3 ? 4 : 2;
    ctx.stroke();
  }
}

function showNoMap(reason) {
  lastMap = null;
  frame = null;
  const canvas = $("map");
  canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
  const caption = $("map-caption");
  caption.textContent = `No map: ${reason}`;
  caption.className = "map-caption bad";
}

window.addEventListener("resize", scheduleDraw);

// --- Map selections: points (Point tool, long-press, right-click) and areas (Polygon: tap the -
// --- corners; Rectangle: drag). A selected one can be dragged by its handles, sent as a quick -
// --- mission (Fly here / Search area, two taps), or named in your own mission as P1, A1, ... -
function rectangleCorners([[n1, e1], [n2, e2]]) {
  return [[n1, e1], [n1, e2], [n2, e2], [n2, e1]];
}

function centroid(points) {
  return [points.reduce((sum, p) => sum + p[0], 0) / points.length, points.reduce((sum, p) => sum + p[1], 0) / points.length];
}

function squareMetres(points) {
  let twice = 0;
  points.forEach(([n, e], index) => {
    const [n2, e2] = points[(index + 1) % points.length];
    twice += e * n2 - e2 * n;
  });
  return Math.abs(twice) / 2;
}

function insideArea([n, e], points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [ni, ei] = points[i];
    const [nj, ej] = points[j];
    if (ni > n !== nj > n && e < ((ej - ei) * (n - ni)) / (nj - ni) + ei) inside = !inside;
  }
  return inside;
}

const selectedSelection = () => selections.find((s) => s.id === selectedId) || null;

function mapHandles() {
  if (tool) return tool === "polygon" ? draft.map((_, index) => ({ points: draft, index })) : [];
  const chosen = selectedSelection();
  return chosen ? chosen.points.map((_, index) => ({ points: chosen.points, index })) : [];
}

function handleAt(xy) {
  let best = null;
  for (const handle of mapHandles()) {
    const away = apart(toXY(handle.points[handle.index]), xy);
    if (away <= HANDLE_PX && (!best || away < best.away)) best = { ...handle, away };
  }
  return best;
}

function selectionAt(xy) {
  const newestFirst = [...selections].reverse();
  const point = newestFirst.find((s) => s.kind === "point" && apart(toXY(s.points[0]), xy) <= HANDLE_PX);
  return point || newestFirst.find((s) => s.kind === "area" && insideArea(toNE(xy), s.points)) || null;
}

function addSelection(kind, points) {
  const id = `${kind === "point" ? "P" : "A"}${nextNumber[kind]++}`;
  selections.push({ id, kind, points: points.map((p) => p.slice()) });
  selectedId = id;
  setTool(null);
}

function select(selection) {
  selectedId = selection ? selection.id : null;
  disarmQuickMission();
  showMapTools();
  scheduleDraw();
}

// The map stops following while you place something, so it does not slide under your finger.
function holdView() {
  if (!frame) return;
  view.follow = false;
  view.centre = frame.centre.slice();
  view.radius = frame.radius;
}

function setTool(name) {
  tool = name;
  draft = [];
  if (name) {
    holdView();
    selectedId = null;
  }
  disarmQuickMission();
  showMapTools();
  scheduleDraw();
}

function finishPolygon() {
  if (tool === "polygon" && draft.length >= 3) addSelection("area", draft);
}

function describeSelection(selection) {
  const where = ([n, e]) => `${metres(Math.abs(n))} m ${n < 0 ? "S" : "N"}, ${metres(Math.abs(e))} m ${e < 0 ? "W" : "E"}`;
  const drone = lastMap && finitePoint((lastMap.local || {}).vehicle_pose);
  if (selection.kind === "point") {
    const away = drone ? ` · ${Math.round(apart(drone, selection.points[0]))} m from the drone` : "";
    return `${selection.id}: ${where(selection.points[0])} of home${away}`;
  }
  return `${selection.id}: ${selection.points.length} corners, about ${Math.round(squareMetres(selection.points))} m²`;
}

const TOOL_HINTS = {
  point: "Tap the map where the point goes.",
  polygon: "Tap the corners, then the first corner or Done. Drag a corner to move it.",
  rectangle: "Drag across the map to draw the rectangle.",
};

function showMapTools() {
  const chosen = tool ? null : selectedSelection();
  $("map-tool-pick").hidden = Boolean(tool);
  $("map-tool-draw").hidden = !tool;
  $("map-selection").hidden = !chosen;
  $("map").classList.toggle("placing", Boolean(tool));
  if (tool) {
    $("map-tool-hint").textContent = TOOL_HINTS[tool];
    $("map-undo").hidden = tool !== "polygon";
    $("map-done").hidden = tool !== "polygon";
    $("map-undo").disabled = !draft.length;
    $("map-done").disabled = draft.length < 3;
  }
  if (chosen) {
    $("map-selection-text").textContent = describeSelection(chosen);
    const go = $("map-go");
    go.textContent = armedId === chosen.id ? "Tap again to send" : chosen.kind === "point" ? "Fly here" : "Search area";
    go.classList.toggle("armed", armedId === chosen.id);
  }
}

// Selections a mission names (P1, A1, ...) travel with it as coordinates the AI can fly to.
function withSelections(text) {
  const named = selections.filter((s) => new RegExp(`\\b${s.id}\\b`).test(text));
  if (!named.length) return text;
  const pair = ([n, e]) => `[${metres(n)}, ${metres(e)}]`;
  const lines = named.map((s) => (s.kind === "point"
    ? `${s.id}: a point at [north_m, east_m] ${pair(s.points[0])}`
    : `${s.id}: an area with corners [north_m, east_m], in order: ${s.points.map(pair).join(", ")}; ` +
      `about ${Math.round(squareMetres(s.points))} m², centre ${pair(centroid(s.points))}`));
  return `${text}\n\nThe operator's map selections, in metres from home (the frame goto takes):\n${lines.join("\n")}`;
}

const QUICK_MISSIONS = {
  point: (s) => `Fly to ${s.id} and hold there. If you are on the ground, take off first.`,
  area: (s) => `Search ${s.id}: fly over the whole area in parallel lanes so the camera sees all of it, ` +
    "at a height where people and vehicles are still recognisable, then report what you found and where.",
};
const ARM_MS = 4000;
let armedId = null; // the selection whose quick mission waits for its second tap
let armTimer = null;

function disarmQuickMission() {
  armedId = null;
  clearTimeout(armTimer);
}

$("map-go").addEventListener("click", async () => {
  const chosen = selectedSelection();
  if (!chosen || stopped) return;
  if (armedId !== chosen.id) {
    // A quick mission is a real mission (it also ends an operator lock): the first tap only arms it.
    disarmQuickMission();
    armedId = chosen.id;
    armTimer = setTimeout(() => {
      armedId = null;
      showMapTools();
    }, ARM_MS);
    showMapTools();
    return;
  }
  disarmQuickMission();
  showMapTools();
  const go = $("map-go");
  go.disabled = true;
  try {
    await postMission(QUICK_MISSIONS[chosen.kind](chosen));
  } finally {
    go.disabled = false;
  }
});
$("map-use").addEventListener("click", () => {
  const chosen = selectedSelection();
  if (!chosen) return;
  const box = $("mission-text");
  const before = box.value.replace(/\s+$/, "");
  box.value = `${before}${before ? " " : ""}${chosen.id} `;
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
});
$("map-delete").addEventListener("click", () => {
  const index = selections.findIndex((s) => s.id === selectedId);
  if (index >= 0) selections.splice(index, 1);
  select(null);
});
for (const button of document.querySelectorAll("[data-tool]")) {
  button.addEventListener("click", () => setTool(button.dataset.tool));
}
$("map-undo").addEventListener("click", () => {
  draft.pop();
  showMapTools();
  scheduleDraw();
});
$("map-done").addEventListener("click", finishPolygon);
$("map-cancel").addEventListener("click", () => setTool(null));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && tool) setTool(null);
});

// --- View controls: +/- zoom around the centre (following continues), the centre button -----
// --- follows the drone again. On the map: one finger or the mouse drags, two fingers pinch, --
// --- the wheel zooms at the pointer, a double tap zooms in. ---------------------------------
function zoomBy(factor) {
  if (!frame) return;
  view.radius = clampRadius(frame.radius * factor);
  if (!view.follow) view.centre = frame.centre.slice();
  scheduleDraw();
}

// Zoom so the map point under `xy` stays under it.
function zoomAround(xy, radius) {
  const anchor = toNE(xy);
  view.follow = false;
  view.radius = clampRadius(radius);
  const scale = Math.min(frame.width, frame.height) / 2 / view.radius;
  view.centre = [anchor[0] + (xy[1] - frame.height / 2) / scale, anchor[1] - (xy[0] - frame.width / 2) / scale];
  scheduleDraw();
}

$("map-zoom-in").addEventListener("click", () => zoomBy(0.5));
$("map-zoom-out").addEventListener("click", () => zoomBy(2));
$("map-centre").addEventListener("click", () => {
  view.follow = true;
  if (frame && !frame.streetMap) view.radius = null; // fit everything again
  scheduleDraw();
});

const mapCanvas = $("map");
const pointers = new Map(); // pointer id -> [x, y] on the canvas
let gesture = null;
let longPress = null;
let lastTap = null;
let lastPointerType = "mouse";

function canvasXY(event) {
  const rect = mapCanvas.getBoundingClientRect();
  return [event.clientX - rect.left, event.clientY - rect.top];
}

function pinchOf() {
  const [a, b] = [...pointers.values()];
  return { middle: [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], span: Math.max(1, apart(a, b)) };
}

mapCanvas.addEventListener("pointerdown", (event) => {
  lastPointerType = event.pointerType;
  if (!frame || (event.pointerType === "mouse" && event.button !== 0)) return;
  mapCanvas.setPointerCapture(event.pointerId);
  const xy = canvasXY(event);
  pointers.set(event.pointerId, xy);
  clearTimeout(longPress);
  if (pointers.size === 1) {
    const handle = handleAt(xy);
    gesture = { kind: "one", start: xy, startNE: toNE(xy), centre: frame.centre, scale: frame.scale, handle, moved: false };
    if (!tool && !handle) {
      longPress = setTimeout(() => {
        if (gesture && gesture.kind === "one" && !gesture.moved) {
          gesture.kind = "done";
          addSelection("point", [gesture.startNE]);
          if (navigator.vibrate) navigator.vibrate(30);
        }
      }, LONG_PRESS_MS);
    }
  } else if (pointers.size === 2) {
    if (tool === "rectangle") draft = [];
    const { middle, span } = pinchOf();
    gesture = { kind: "pinch", anchor: toNE(middle), span, radius: frame.radius };
  }
});

mapCanvas.addEventListener("pointermove", (event) => {
  if (!pointers.has(event.pointerId) || !gesture || !frame) return;
  const xy = canvasXY(event);
  pointers.set(event.pointerId, xy);
  if (gesture.kind === "pinch") {
    if (pointers.size < 2) return;
    const { middle, span } = pinchOf();
    view.follow = false;
    view.radius = clampRadius((gesture.radius * gesture.span) / span);
    const scale = Math.min(frame.width, frame.height) / 2 / view.radius;
    view.centre = [gesture.anchor[0] + (middle[1] - frame.height / 2) / scale, gesture.anchor[1] - (middle[0] - frame.width / 2) / scale];
    scheduleDraw();
    return;
  }
  if (gesture.kind !== "one") return;
  if (!gesture.moved && apart(xy, gesture.start) < TAP_SLOP_PX) return;
  gesture.moved = true;
  clearTimeout(longPress);
  if (gesture.handle) {
    if (view.follow) holdView();
    gesture.handle.points[gesture.handle.index] = toNE(xy);
  } else if (tool === "rectangle") {
    draft = [gesture.startNE, toNE(xy)];
  } else {
    view.follow = false;
    view.radius = frame.radius;
    view.centre = [
      gesture.centre[0] + (xy[1] - gesture.start[1]) / gesture.scale,
      gesture.centre[1] - (xy[0] - gesture.start[0]) / gesture.scale,
    ];
  }
  scheduleDraw();
});

function endPointer(event) {
  if (!pointers.has(event.pointerId)) return;
  const xy = canvasXY(event);
  pointers.delete(event.pointerId);
  clearTimeout(longPress);
  const ended = gesture;
  // After a pinch the remaining finger does nothing until all are lifted.
  gesture = pointers.size ? { kind: "done" } : null;
  if (!ended || ended.kind !== "one" || event.type === "pointercancel" || !frame) return;
  if (ended.moved) {
    if (tool === "rectangle" && draft.length === 2) {
      const [a, b] = draft.map((p) => toXY(p));
      if (Math.abs(a[0] - b[0]) >= 10 && Math.abs(a[1] - b[1]) >= 10) addSelection("area", rectangleCorners(draft));
      else draft = [];
    }
    showMapTools();
    scheduleDraw();
    return;
  }
  tapMap(xy, ended.handle);
}
mapCanvas.addEventListener("pointerup", endPointer);
mapCanvas.addEventListener("pointercancel", endPointer);

function tapMap(xy, handle) {
  if (tool === "point") {
    addSelection("point", [toNE(xy)]);
    return;
  }
  if (tool === "polygon") {
    if (handle && handle.index === 0 && draft.length >= 3) finishPolygon();
    else if (!handle) draft.push(toNE(xy));
    showMapTools();
    scheduleDraw();
    return;
  }
  if (tool) return;
  const now = performance.now();
  if (lastTap && now - lastTap.time < DOUBLE_TAP_MS && apart(xy, lastTap.xy) < 30) {
    lastTap = null;
    zoomAround(xy, frame.radius / 2);
    return;
  }
  lastTap = { time: now, xy };
  if (!handle) select(selectionAt(xy));
}

mapCanvas.addEventListener("wheel", (event) => {
  if (!frame) return;
  event.preventDefault();
  const lines = event.deltaMode === 1 ? 33 : 1;
  zoomAround(canvasXY(event), frame.radius * Math.exp(event.deltaY * lines * 0.0015));
}, { passive: false });

// Right-click drops a point on a laptop; a touch long-press is handled above (and its own
// context menu suppressed).
mapCanvas.addEventListener("contextmenu", (event) => {
  event.preventDefault();
  if (lastPointerType === "mouse" && frame && !tool) addSelection("point", [toNE(canvasXY(event))]);
});

// --- Map data, both views. The street map comes as tiles in three levels of detail, listed by --
// --- /api/map/index: a view fetches the tiles it shows, a few at a time, newest view first, and --
// --- keeps them in IndexedDB: those you browse up to a cap (least recently used go first), a ----
// --- whole-map download (Offline map) until you remove it. The index is refreshed with ---------
// --- If-None-Match; a new version drops every stored tile. Only the small live part is polled, -
// --- and only while the map is on screen. Without IndexedDB (private mode) tiles stay in memory.
const MAP_DB = "horus";
const MAP_DB_VERSION = 2; // version 1 held one static map ("static-map"), replaced by the tiles
const TILE_STORE = "tiles"; // "level/x/y" -> { key, level, x, y, version, data (JSON text), bytes, lastUsed, pinned: 0 | 1 }
const META_STORE = "meta"; // "index": the tile index; "offline": a whole-map download was made
let phoneTab = "feed";
// The index in use: { version, provider, bounds, levels: level -> { size, listed: "x/y" -> bytes },
// order: levels finest first, count, bytes } (bytes: download sizes).
let mapIndex = null;
let indexChecked = false; // asked the service this page load (200 or 304)
let indexProblem = "";
let storedMapRead = false;
let liveMap = null;
let liveProblem = "";
let mapRunning = false;
let offlineWanted = false; // a whole-map download was made: after a new map version, say so
let download = null; // the whole-map download running: { total, done, failed, bytes, controller }
let downloadResult = ""; // how the last download or removal ended
const tiles = {
  memory: new Map(), // key -> decoded tile (prepTile), least recently used first
  memoryChars: 0,
  inView: new Set(), // keys drawn now: never dropped from memory
  stored: new Map(), // key -> { lastUsed, saved, pinned }: what IndexedDB holds (saved: lastUsed there)
  writes: new Map(), // key -> { text, version }: waiting to be stored
  writeTimer: null,
  reading: new Set(), // keys being read from IndexedDB
  fetching: new Set(), // keys the view is fetching
  inflight: new Map(), // key -> the tile request, shared by the view and the download
  failed: new Map(), // key -> when to try again
  absent: new Set(), // listed, but the service has no such tile
  wantFetch: [], // the newest view's missing tiles, most important first
  wantLocal: [], // other levels' tiles to fill gaps meanwhile, from IndexedDB only
  signature: "",
  pumpQueued: false,
  touched: new Set(), // keys drawn since their last use was last noted
  problem: "", // why the last tile request or write failed
};

const tileKey = (level, x, y) => `${level}/${x}/${y}`;
const sizeText = (bytes) => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB`
  : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.round(bytes / 1e3)} kB`);

function tileBytes(key) {
  const [level, x, y] = key.split("/");
  const info = mapIndex && mapIndex.levels.get(Number(level));
  return (info && info.listed.get(`${x}/${y}`)) || 0;
}

function tileCap() {
  try {
    const value = Number(localStorage.getItem(TILE_CAP_KEY));
    if (value > 0) return value;
  } catch {
    // storage blocked: the default
  }
  return TILE_CAP_BYTES;
}

// One connection for the page's life (reopened if a newer page upgrades it); null without
// IndexedDB (private mode, or refused).
let mapDbOpening = null;
function mapDb() {
  if (!mapDbOpening) {
    mapDbOpening = new Promise((resolve) => {
      let request;
      try {
        request = indexedDB.open(MAP_DB, MAP_DB_VERSION);
      } catch {
        resolve(null);
        return;
      }
      request.onupgradeneeded = () => {
        const db = request.result;
        if (db.objectStoreNames.contains("static-map")) db.deleteObjectStore("static-map");
        if (!db.objectStoreNames.contains(TILE_STORE)) {
          const store = db.createObjectStore(TILE_STORE, { keyPath: "key" });
          store.createIndex("lastUsed", "lastUsed");
          store.createIndex("pinned", "pinned");
        }
        if (!db.objectStoreNames.contains(META_STORE)) db.createObjectStore(META_STORE);
      };
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { // a newer page upgrades it, or the database is deleted
          db.close();
          mapDbOpening = null;
        };
        resolve(db);
      };
      request.onblocked = () => resolve(null); // an old page holds it open: memory only this time
      request.onerror = () => resolve(null);
    });
  }
  return mapDbOpening;
}

// Runs `work(transaction, out)`, which files its requests' results in `out`; resolves to `out`
// once the transaction completes.
async function transact(names, mode, work) {
  const db = await mapDb();
  if (!db) throw new Error("no IndexedDB");
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(names, mode);
    const out = {};
    work(transaction, out);
    transaction.oncomplete = () => resolve(out);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error || new Error("IndexedDB transaction aborted"));
  });
}

// The index as drawing uses it; a new version starts again with no tiles at all.
function useIndex(raw) {
  const levels = new Map();
  let count = 0;
  let bytes = 0;
  for (const entry of Array.isArray(raw.levels) ? raw.levels : []) {
    const level = number(entry && entry.level);
    const size = number(entry && entry.size_m);
    const list = raw.tiles && raw.tiles[String(level)];
    if (level === null || !(size > 0) || !Array.isArray(list)) continue;
    const listed = new Map();
    for (const item of list) {
      if (!Array.isArray(item) || !Number.isInteger(item[0]) || !Number.isInteger(item[1])) continue;
      listed.set(`${item[0]}/${item[1]}`, number(item[2]) || 0);
      bytes += number(item[2]) || 0;
    }
    count += listed.size;
    if (listed.size) levels.set(level, { size, listed });
  }
  const box = Array.isArray(raw.bounds) ? raw.bounds.map(number) : [];
  const bounds = box.length === 4 && box.every((v) => v !== null) && box[2] > box[0] && box[3] > box[1] ? box : null;
  const version = String(raw.version);
  const changed = !mapIndex || mapIndex.version !== version;
  mapIndex = { version, provider: String(raw.provider || "none"), bounds, levels, order: [...levels.keys()].sort((a, b) => a - b), count, bytes };
  if (!changed) return;
  tiles.memory.clear();
  tiles.memoryChars = 0;
  tiles.stored.clear();
  tiles.writes.clear();
  tiles.failed.clear();
  tiles.absent.clear();
  tiles.signature = "";
  scenes.clear();
  districtParts.clear();
  districtLabels = null;
}

// At page start: the stored index, and which tiles IndexedDB holds (keys and last use only).
async function loadStoredMap() {
  let out;
  try {
    out = await transact([META_STORE, TILE_STORE], "readonly", (transaction, result) => {
      const meta = transaction.objectStore(META_STORE);
      meta.get("index").onsuccess = (event) => { result.index = event.target.result; };
      meta.get("offline").onsuccess = (event) => { result.offline = event.target.result; };
      const store = transaction.objectStore(TILE_STORE);
      result.used = [];
      const cursor = store.index("lastUsed").openKeyCursor();
      cursor.onsuccess = () => {
        if (!cursor.result) return;
        result.used.push([cursor.result.primaryKey, cursor.result.key]);
        cursor.result.continue();
      };
      store.index("pinned").getAllKeys(1).onsuccess = (event) => { result.pinned = event.target.result; };
    });
  } catch {
    return;
  }
  offlineWanted = out.offline === true;
  if (!out.index || mapIndex) return;
  useIndex(out.index);
  const pinned = new Set(out.pinned || []);
  for (const [key, lastUsed] of out.used) tiles.stored.set(key, { lastUsed, saved: lastUsed, pinned: pinned.has(key) });
  saveTiles(); // trims to the cap, if it was made smaller since
}

async function refreshIndex() {
  const headers = mapIndex ? { "If-None-Match": `"${mapIndex.version}"` } : {};
  const response = await api("/api/map/index", { headers });
  if (response.status === 304) return;
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  if (typeof payload.version !== "string" || !payload.version) throw new Error("the map index has no version");
  if (mapIndex && mapIndex.version === payload.version) return;
  if (download) download.controller.abort();
  useIndex(payload);
  try {
    await transact([META_STORE, TILE_STORE], "readwrite", (transaction) => {
      transaction.objectStore(TILE_STORE).clear(); // every tile of the old version
      transaction.objectStore(META_STORE).put(payload, "index");
    });
  } catch {
    // memory only
  }
  showOfflineSheet();
  scheduleDraw();
}

function memoryTile(key) {
  const tile = tiles.memory.get(key);
  if (tile) {
    tiles.memory.delete(key); // to the newest end
    tiles.memory.set(key, tile);
  }
  return tile;
}

// The tiles a view needs at its zoom's level. Those in memory are drawn now; the missing ones are
// asked for (stored ones read, the rest fetched; nearest the centre first, then a ring of one
// tile around the screen), and meanwhile another level's tile already in memory fills each gap:
// the coarser one over it, else the finer ones in it. No storage or network work here.
function viewTiles(f) {
  const mpp = 1 / f.scale;
  const { order, levels } = mapIndex;
  const level = order[Math.min(order.length - 1, TILE_LEVEL_MPP.filter((limit) => mpp > limit).length)];
  const { size } = levels.get(level);
  const has = (other, x, y) => levels.get(other).listed.has(`${x}/${y}`) && !tiles.absent.has(tileKey(other, x, y));
  const [cn, ce] = f.centre;
  const reachE = (f.width / 2) * mpp;
  const reachN = (f.height / 2) * mpp;
  const [x0, x1] = [Math.floor((ce - reachE) / size), Math.floor((ce + reachE) / size)];
  const [y0, y1] = [Math.floor((cn - reachN) / size), Math.floor((cn + reachN) / size)];
  const cells = (margin) => {
    const list = [];
    for (let x = x0 - margin; x <= x1 + margin; x += 1) {
      for (let y = y0 - margin; y <= y1 + margin; y += 1) {
        const inside = x >= x0 && x <= x1 && y >= y0 && y <= y1;
        if ((margin ? inside : !inside) || !has(level, x, y)) continue;
        list.push([Math.hypot((x + 0.5) * size - ce, (y + 0.5) * size - cn), x, y]);
      }
    }
    return list.sort((a, b) => a[0] - b[0]);
  };
  const shown = [];
  const gaps = [];
  const fetch = [];
  for (const [, x, y] of cells(0)) {
    const key = tileKey(level, x, y);
    const tile = memoryTile(key);
    if (tile) shown.push(tile);
    else {
      gaps.push([x, y]);
      fetch.push(key);
    }
  }
  for (const [, x, y] of cells(1)) {
    const key = tileKey(level, x, y);
    if (!tiles.memory.has(key)) fetch.push(key);
  }
  const under = new Map();
  const local = new Set();
  const finer = order[order.indexOf(level) - 1];
  for (const [x, y] of gaps) {
    let covered = false;
    for (const other of order) {
      if (other <= level) continue;
      const big = levels.get(other).size;
      const [ax, ay] = [Math.floor((x * size) / big), Math.floor((y * size) / big)];
      if (!has(other, ax, ay)) continue;
      const key = tileKey(other, ax, ay);
      const tile = memoryTile(key);
      if (tile) {
        under.set(key, tile);
        covered = true;
        break;
      }
      local.add(key);
    }
    if (covered || finer === undefined) continue;
    const small = levels.get(finer).size;
    const span = (start) => [Math.floor((start * size) / small), Math.ceil(((start + 1) * size) / small) - 1];
    const [[fx0, fx1], [fy0, fy1]] = [span(x), span(y)];
    for (let fx = fx0; fx <= fx1; fx += 1) {
      for (let fy = fy0; fy <= fy1; fy += 1) {
        const key = tileKey(finer, fx, fy);
        const tile = tiles.memory.get(key);
        if (tile) under.set(key, tile);
      }
    }
  }
  tiles.inView = new Set([...shown.map((tile) => tile.key), ...under.keys()]);
  for (const tile of shown) tiles.touched.add(tile.key);
  requestTiles(fetch, [...local]);
  const byKey = (a, b) => (a.key < b.key ? -1 : 1); // a set in a fixed order: the same scene
  return { level, size, tiles: shown.sort(byKey), under: [...under.values()].sort(byKey), gaps };
}

function tileNote(shown) {
  let note = "";
  if (shown.gaps.length) {
    note += tiles.problem ? ` · ${shown.gaps.length} map tile(s) missing: ${tiles.problem}` : ` · loading ${shown.gaps.length} map tile(s)`;
  }
  if (download) note += ` · downloading the map: ${download.done} of ${download.total} tiles`;
  return note;
}

// The newest view's wishes replace the older ones: requests not yet started for tiles that left
// the screen are dropped (the ones under way finish and are kept).
function requestTiles(fetch, local) {
  const signature = `${fetch.join(" ")}|${local.join(" ")}`;
  if (signature === tiles.signature) return;
  tiles.signature = signature;
  tiles.wantFetch = fetch;
  tiles.wantLocal = local;
  schedulePump();
}

function schedulePump() {
  if (tiles.pumpQueued) return;
  tiles.pumpQueued = true;
  setTimeout(pumpTiles, 0);
}

function pumpTiles() {
  tiles.pumpQueued = false;
  if (!mapIndex) return;
  const reads = [];
  for (const key of [...tiles.wantFetch, ...tiles.wantLocal]) {
    if (tiles.memory.has(key) || !tiles.stored.has(key) || tiles.reading.has(key)) continue;
    const waiting = tiles.writes.get(key); // downloaded, not written yet
    if (waiting) keepTile(key, JSON.parse(waiting.text), waiting.text.length);
    else reads.push(key);
  }
  if (reads.length) readTiles(reads);
  const now = Date.now();
  for (const key of tiles.wantFetch) {
    if (tiles.fetching.size >= TILE_FETCHES) break;
    if (tiles.memory.has(key) || tiles.stored.has(key) || tiles.fetching.has(key) || (tiles.failed.get(key) || 0) > now) continue;
    fetchForView(key);
  }
}

function keepTile(key, data, chars) {
  const old = tiles.memory.get(key);
  if (old) {
    tiles.memoryChars -= old.chars;
    tiles.memory.delete(key);
  }
  const tile = prepTile(key, data);
  tile.chars = chars;
  tiles.memory.set(key, tile);
  tiles.memoryChars += chars;
  for (const [oldKey, oldTile] of tiles.memory) {
    if (tiles.memoryChars <= MEMORY_TILE_CHARS) break;
    if (oldKey === key || tiles.inView.has(oldKey)) continue;
    tiles.memory.delete(oldKey);
    tiles.memoryChars -= oldTile.chars;
  }
  scheduleDraw();
}

async function readTiles(keys) {
  const { version } = mapIndex;
  for (const key of keys) tiles.reading.add(key);
  let records = {};
  try {
    records = await transact([TILE_STORE], "readonly", (transaction, out) => {
      const store = transaction.objectStore(TILE_STORE);
      for (const key of keys) store.get(key).onsuccess = (event) => { out[key] = event.target.result; };
    });
  } catch {
    records = {};
  }
  for (const key of keys) tiles.reading.delete(key);
  if (!mapIndex || mapIndex.version !== version) return;
  for (const key of keys) {
    const record = records[key];
    let data = null;
    try {
      data = record && record.version === version ? JSON.parse(record.data) : null;
    } catch {
      data = null;
    }
    if (data) keepTile(key, data, record.data.length);
    else if (!tiles.writes.has(key)) tiles.stored.delete(key); // gone or damaged: fetch it again
  }
  schedulePump();
}

// One request per tile at a time, shared by the view and the download. Resolves to
// { text, data }, { missing } (the service has no such tile) or { stale } (a newer map version).
function tileText(key, signal) {
  let request = tiles.inflight.get(key);
  if (request) return request;
  const [level, x, y] = key.split("/");
  const { version } = mapIndex;
  request = (async () => {
    const response = await api(`/api/map/tile?level=${level}&x=${x}&y=${y}`, signal ? { signal } : {});
    if (response.status === 404) return { missing: true };
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        message = (await response.json()).error || message;
      } catch {
        // the status says enough
      }
      throw new Error(message);
    }
    const text = await response.text();
    const data = JSON.parse(text);
    if (String(data.version) !== version) {
      indexChecked = false; // the map changed: the loop fetches the new index
      return { stale: true };
    }
    return { text, data };
  })().finally(() => tiles.inflight.delete(key));
  tiles.inflight.set(key, request);
  return request;
}

async function fetchForView(key) {
  tiles.fetching.add(key);
  const { version } = mapIndex;
  try {
    const answer = await tileText(key);
    if (!mapIndex || mapIndex.version !== version) return;
    if (answer.missing) tiles.absent.add(key);
    else if (answer.stale) tiles.failed.set(key, Date.now() + TILE_RETRY_MS);
    else {
      tiles.problem = "";
      keepTile(key, answer.data, answer.text.length);
      saveTile(key, answer.text, false);
    }
  } catch (error) {
    if (error instanceof Unauthorized || error.name === "AbortError") return;
    tiles.failed.set(key, Date.now() + TILE_RETRY_MS);
    tiles.problem = error instanceof TypeError ? "no connection" : error.message;
    setTimeout(schedulePump, TILE_RETRY_MS);
    scheduleDraw();
  } finally {
    tiles.fetching.delete(key);
    schedulePump();
  }
}

// Tiles are written in batches; lastUsed 0 = downloaded, never shown.
function saveTile(key, text, pin, lastUsed = Date.now()) {
  const known = tiles.stored.get(key);
  const used = known ? Math.max(known.lastUsed, lastUsed) : lastUsed;
  tiles.stored.set(key, { lastUsed: used, saved: used, pinned: pin || Boolean(known && known.pinned) });
  tiles.writes.set(key, { text, version: mapIndex.version });
  clearTimeout(tiles.writeTimer);
  tiles.writeTimer = setTimeout(saveTiles, 300);
}

// The browsed (unpinned) tiles over the cap, least recently used first.
function overCap() {
  let total = 0;
  const loose = [];
  for (const [key, entry] of tiles.stored) {
    if (entry.pinned) continue;
    total += tileBytes(key);
    loose.push([entry.lastUsed, key]);
  }
  const victims = [];
  const cap = tileCap();
  if (total <= cap) return victims;
  loose.sort((a, b) => a[0] - b[0]);
  for (const [, key] of loose) {
    if (total <= cap) break;
    victims.push(key);
    total -= tileBytes(key);
  }
  return victims;
}

// Writes the waiting tiles and removes the browsed ones over the cap, in one transaction; false
// when the tiles could not be stored (they stay in memory only).
async function saveTiles() {
  clearTimeout(tiles.writeTimer);
  noteTileUse(); // the tiles on screen now count as just used
  const writes = [...tiles.writes].filter(([, waiting]) => mapIndex && waiting.version === mapIndex.version);
  tiles.writes.clear();
  const victims = overCap();
  for (const key of victims) tiles.stored.delete(key);
  if (!writes.length && !victims.length) return true;
  try {
    await transact([TILE_STORE], "readwrite", (transaction) => {
      const store = transaction.objectStore(TILE_STORE);
      for (const [key, { text, version }] of writes) {
        const entry = tiles.stored.get(key);
        if (!entry) continue; // over the cap at once
        const [level, x, y] = key.split("/").map(Number);
        store.put({ key, level, x, y, version, data: text, bytes: tileBytes(key), lastUsed: entry.lastUsed, pinned: entry.pinned ? 1 : 0 });
      }
      for (const key of victims) store.delete(key);
    });
    return true;
  } catch (error) {
    for (const [key] of writes) tiles.stored.delete(key);
    tiles.problem = error && error.name === "QuotaExceededError" ? "device storage full" : "tiles not stored on this device";
    return false;
  }
}

// Each drawn tile's last use, for the cap: exact in memory, written back now and then.
function noteTileUse() {
  if (!tiles.touched.size) return;
  const now = Date.now();
  const due = [];
  for (const key of tiles.touched) {
    const entry = tiles.stored.get(key);
    if (!entry || tiles.writes.has(key)) continue;
    entry.lastUsed = now;
    if (now - entry.saved < TOUCH_SAVE_MS) continue;
    entry.saved = now;
    due.push(key);
  }
  tiles.touched.clear();
  if (!due.length) return;
  transact([TILE_STORE], "readwrite", (transaction) => {
    const store = transaction.objectStore(TILE_STORE);
    for (const key of due) {
      const request = store.get(key);
      request.onsuccess = () => {
        if (request.result) store.put({ ...request.result, lastUsed: now });
      };
    }
  }).catch(() => {});
}
setInterval(noteTileUse, 5000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) noteTileUse();
});

// --- Offline map: the whole map downloaded a few tiles at a time and kept (pinned: never -----
// --- removed for the cap) until "Remove download" frees it, back to the browsed tiles' cap. ----
function offlineCounts() {
  const counts = { pinned: 0, pinnedBytes: 0, loose: 0, looseBytes: 0 };
  for (const [key, entry] of tiles.stored) {
    const kind = entry.pinned ? "pinned" : "loose";
    counts[kind] += 1;
    counts[`${kind}Bytes`] += tileBytes(key);
  }
  return counts;
}

let sheetShownAt = 0;
function showOfflineSheet() {
  if ($("offline-sheet").hidden) return;
  sheetShownAt = performance.now();
  const whole = $("offline-whole");
  const kept = $("offline-kept");
  const status = $("offline-status");
  const progress = $("offline-progress");
  const start = $("offline-download");
  $("offline-cancel").hidden = !download;
  progress.hidden = !download;
  if (!mapIndex || !mapIndex.order.length) {
    whole.textContent = mapIndex ? "This world has no street map." : `The street map is not loaded yet${indexProblem ? `: ${indexProblem}` : ""}.`;
    kept.textContent = "";
    status.textContent = "";
    start.hidden = true;
    $("offline-remove").hidden = true;
    return;
  }
  const counts = offlineCounts();
  const missing = mapIndex.count - counts.pinned;
  if (download) {
    whole.textContent = `Downloading the whole map for offline use (${sizeText(mapIndex.bytes)}, ${mapIndex.count} tiles).`;
  } else if (!missing) {
    whole.textContent = `The whole map is on this device for offline use: ${sizeText(counts.pinnedBytes)}, ${counts.pinned} tiles, kept until you remove it.`;
  } else if (counts.pinned) {
    whole.textContent = `Part of the map is downloaded: ${counts.pinned} of ${mapIndex.count} tiles, ${sizeText(counts.pinnedBytes)}.`;
  } else {
    whole.textContent = offlineWanted ? "The map changed since your download: download it again to use it offline."
      : "Download the whole map to use it without a connection.";
  }
  kept.textContent = !missing && !download
    ? `Without the download, the map you browse is kept up to ${sizeText(tileCap())}, the least recently used going first.`
    : `Map you browse: ${sizeText(counts.looseBytes)} (${counts.loose} tiles) kept, up to ${sizeText(tileCap())}; ` +
      "the least recently used goes first. Sizes are download sizes.";
  if (download) {
    progress.max = download.total;
    progress.value = download.done;
    status.textContent = `${download.done} of ${download.total} tiles, ${sizeText(download.bytes)}${download.failed ? `, ${download.failed} failed` : ""}`;
  } else {
    status.textContent = downloadResult;
  }
  start.hidden = Boolean(download) || !missing;
  start.textContent = counts.pinned ? `Download the rest (${sizeText(Math.max(0, mapIndex.bytes - counts.pinnedBytes))}, ${missing} tiles)`
    : `Download whole map (${sizeText(mapIndex.bytes)}, ${mapIndex.count} tiles)`;
  $("offline-remove").hidden = Boolean(download) || !counts.pinned;
}

async function showStorageEstimate() {
  const line = $("offline-storage");
  line.textContent = "";
  if (!navigator.storage || !navigator.storage.estimate) return;
  try {
    const { usage, quota } = await navigator.storage.estimate();
    const persisted = navigator.storage.persisted ? await navigator.storage.persisted() : false;
    line.textContent = `This app's browser storage: ${sizeText(usage || 0)} used of ${sizeText(quota || 0)} allowed; ` +
      (persisted ? "kept even when the device runs low on space." : "the browser may clear it when the device runs low on space.");
  } catch {
    // no estimate
  }
}

function openOfflineSheet(open) {
  $("offline-sheet").hidden = !open;
  $("map-offline").setAttribute("aria-expanded", String(open));
  if (!open) return;
  showOfflineSheet();
  showStorageEstimate();
}

// Tiles already stored change to pinned in place.
async function pinTiles(keys) {
  const marking = keys.filter((key) => tiles.stored.has(key) && !tiles.stored.get(key).pinned);
  for (const key of marking) tiles.stored.get(key).pinned = true;
  if (!marking.length) return;
  await transact([TILE_STORE], "readwrite", (transaction) => {
    const store = transaction.objectStore(TILE_STORE);
    for (const key of marking) {
      const request = store.get(key);
      request.onsuccess = () => {
        if (request.result) store.put({ ...request.result, pinned: 1 });
      };
    }
  });
}

async function downloadWholeMap() {
  if (download || stopped || !mapIndex || !mapIndex.order.length) return;
  if (!(await mapDb())) {
    downloadResult = "This browser keeps no data for this page (private mode?): nothing can be stored.";
    showOfflineSheet();
    return;
  }
  const { version } = mapIndex;
  // Coarse levels first (the whole map at a glance soonest), each nearest home first.
  const keys = [];
  for (const level of [...mapIndex.order].reverse()) {
    const { listed } = mapIndex.levels.get(level);
    const cells = [...listed.keys()].map((cell) => cell.split("/").map(Number));
    cells.sort((a, b) => Math.hypot(a[0] + 0.5, a[1] + 0.5) - Math.hypot(b[0] + 0.5, b[1] + 0.5));
    keys.push(...cells.map(([x, y]) => tileKey(level, x, y)));
  }
  const job = { total: keys.length, done: 0, failed: 0, bytes: 0, controller: new AbortController() };
  download = job;
  downloadResult = "";
  showOfflineSheet();
  try {
    if (navigator.storage && navigator.storage.persist) await navigator.storage.persist();
  } catch {
    // stored all the same, only less safe from the browser's clean-ups
  }
  offlineWanted = true;
  transact([META_STORE], "readwrite", (transaction) => transaction.objectStore(META_STORE).put(true, "offline")).catch(() => {});
  let problem = "";
  try {
    await pinTiles(keys);
  } catch {
    problem = "could not mark the stored tiles";
  }
  const queue = [];
  for (const key of keys) {
    const entry = tiles.stored.get(key);
    if (entry && entry.pinned) {
      job.done += 1;
      job.bytes += tileBytes(key);
    } else {
      queue.push(key);
    }
  }
  let next = 0;
  const worker = async () => {
    while (!job.controller.signal.aborted && next < queue.length) {
      const key = queue[next];
      next += 1;
      try {
        const answer = await tileText(key, job.controller.signal);
        if (answer.stale || !mapIndex || mapIndex.version !== version) {
          problem = "the map changed during the download";
          job.controller.abort();
          break;
        }
        if (answer.text) saveTile(key, answer.text, true, 0);
        job.done += 1;
        job.bytes += tileBytes(key);
      } catch (error) {
        if (job.controller.signal.aborted) break;
        if (error instanceof Unauthorized) {
          job.controller.abort();
          break;
        }
        job.failed += 1;
        problem = error instanceof TypeError ? "no connection" : error.message;
      }
      if (tiles.writes.size >= 32 && !(await saveTiles())) {
        problem = tiles.problem;
        job.controller.abort();
      }
      if (performance.now() - sheetShownAt > 250) showOfflineSheet();
    }
  };
  await Promise.all(Array.from({ length: TILE_FETCHES }, worker));
  if (!(await saveTiles())) problem = tiles.problem;
  const cancelled = job.controller.signal.aborted && !problem;
  download = null;
  downloadResult = cancelled ? `Cancelled: the ${job.done} tiles downloaded so far are kept.`
    : problem || job.failed ? `Stopped: ${problem || "some tiles failed"}. ${job.done} of ${job.total} tiles are on this device; download the rest to finish.`
      : `Done: ${job.total} tiles, ${sizeText(job.bytes)}.`;
  showOfflineSheet();
  showStorageEstimate();
  scheduleDraw();
}

// Back to the browsed tiles: the most recently used up to the cap stay (unpinned), the rest go.
async function removeDownload() {
  if (download) return;
  const cap = tileCap();
  const newestFirst = [...tiles.stored].sort((a, b) => b[1].lastUsed - a[1].lastUsed);
  const unpin = [];
  const drop = [];
  let kept = 0;
  let full = false;
  for (const [key, entry] of newestFirst) {
    const bytes = tileBytes(key);
    if (!full && entry.lastUsed > 0 && kept + bytes <= cap) {
      kept += bytes;
      if (entry.pinned) unpin.push(key);
    } else {
      full = true;
      drop.push(key);
    }
  }
  const freed = drop.reduce((sum, key) => sum + tileBytes(key), 0);
  for (const key of unpin) tiles.stored.get(key).pinned = false;
  for (const key of drop) {
    tiles.stored.delete(key);
    tiles.writes.delete(key);
  }
  offlineWanted = false;
  try {
    await transact([TILE_STORE, META_STORE], "readwrite", (transaction) => {
      const store = transaction.objectStore(TILE_STORE);
      for (const key of drop) store.delete(key);
      for (const key of unpin) {
        const request = store.get(key);
        request.onsuccess = () => {
          if (request.result) store.put({ ...request.result, pinned: 0 });
        };
      }
      transaction.objectStore(META_STORE).delete("offline");
    });
    downloadResult = `Removed: ${sizeText(freed)} freed (${drop.length} tiles); the ${unpin.length} most recently used stay as browsed tiles.`;
  } catch {
    downloadResult = "Could not remove the download from this device's storage.";
  }
  showOfflineSheet();
  showStorageEstimate();
}

$("map-offline").addEventListener("click", () => openOfflineSheet($("offline-sheet").hidden));
$("offline-close").addEventListener("click", () => openOfflineSheet(false));
$("offline-download").addEventListener("click", downloadWholeMap);
$("offline-cancel").addEventListener("click", () => {
  if (download) download.controller.abort();
});
$("offline-remove").addEventListener("click", () => {
  if (confirm("Remove the downloaded map? Only the tiles you browsed most recently stay, up to the cap.")) removeDownload();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("offline-sheet").hidden) openOfflineSheet(false);
});

function showLiveMap() {
  const live = liveMap || {};
  const heading = number(live.heading_deg);
  if (heading !== null) headingDeg = heading;
  // Without live data the street map still shows (stored tiles work offline); a world without
  // one has nothing to show.
  if (!liveMap && liveProblem && !(mapIndex && mapIndex.order.length)) {
    showNoMap(liveProblem);
    return;
  }
  updateMap({
    local: {
      vehicle_pose: live.vehicle_pose || null,
      candidates: live.candidates || [],
      gimbal_yaw_deg: live.gimbal_yaw_deg ?? null,
      observed_at: live.observed_at,
      observation_age_s: live.observation_age_s,
    },
    area: { own_map: { marks: live.marks || [] } },
  });
}

const mapOnScreen = () => !document.hidden && (!phoneView || phoneTab === "map");

// One loop at a time; it ends when the map or the page is hidden and starts again when shown.
async function runMap() {
  if (mapRunning || stopped || !mapOnScreen()) return;
  mapRunning = true;
  try {
    if (!storedMapRead) {
      storedMapRead = true;
      await loadStoredMap();
      if (mapIndex) showLiveMap();
    }
    while (!stopped && mapOnScreen()) {
      if (!indexChecked) {
        try {
          await refreshIndex();
          indexChecked = true;
          indexProblem = "";
        } catch (error) {
          if (error instanceof Unauthorized) return;
          indexProblem = error instanceof TypeError ? "AI pilot service unreachable" : error.message;
        }
      }
      try {
        const response = await api("/api/map/live");
        let payload = {};
        try {
          payload = await response.json();
        } catch {
          payload = {};
        }
        liveMap = response.ok ? payload : null;
        liveProblem = response.ok ? "" : payload.error || `HTTP ${response.status}`;
      } catch (error) {
        if (error instanceof Unauthorized) return;
        liveMap = null;
        liveProblem = "AI pilot service unreachable";
      }
      showLiveMap();
      await sleep(MAP_INTERVAL_MS);
    }
  } finally {
    mapRunning = false;
  }
}
document.addEventListener("visibilitychange", runMap);

// --- Phone layout: state line, Feed / Map / Camera tabs, mission box, HOLD / LAND / HOME at the bottom. ---
function showTab(name) {
  const leavingCamera = phoneTab === "camera" && name !== "camera";
  phoneTab = name;
  for (const [tab, button, panel] of [
    ["feed", $("tab-feed"), $("feed-panel")],
    ["map", $("tab-map"), $("map-panel")],
    ["camera", $("tab-camera"), $("aiview-panel")],
  ]) {
    button.setAttribute("aria-selected", String(tab === name));
    panel.hidden = tab !== name;
  }
  if (leavingCamera && liveOn) setLive(false); // live never runs off screen
  if (name === "map") {
    drawMap(); // the canvas had no size while hidden
    runMap();
  } else if (name === "camera") {
    askLatestAiView();
    showAiView();
  } else {
    const feed = $("feed");
    feed.scrollTop = feed.scrollHeight;
  }
}

// The page fills the visible viewport, so the on-screen keyboard shrinks the feed or map,
// never covers the mission box or the emergency buttons.
function fitVisibleViewport() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  document.body.style.setProperty("--app-height", `${viewport.height}px`);
  document.body.style.setProperty("--app-top", `${viewport.offsetTop}px`);
}

function setUpPhoneView() {
  const main = document.querySelector("main.grid");
  const camera = document.querySelector(".camera-panel");
  const drone = document.querySelector(".state-panel");
  $("mission-text").placeholder = "Mission for the AI pilot";
  const mission = $("mission-panel");
  camera.hidden = true; // no camera in the phone view; its poll never starts
  drone.hidden = true; // folded into the state line at the top
  main.replaceChildren($("feed-panel"), $("map-panel"), $("aiview-panel"), camera, drone);
  main.after(mission);
  mission.after(document.querySelector(".emergency"));
  $("tabs").hidden = false;
  $("tab-feed").addEventListener("click", () => showTab("feed"));
  $("tab-map").addEventListener("click", () => showTab("map"));
  $("tab-camera").addEventListener("click", () => showTab("camera"));
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitVisibleViewport);
    window.visualViewport.addEventListener("scroll", fitVisibleViewport);
    fitVisibleViewport();
  }
  showTab("feed");
}

// --- New versions: an installed phone app can stay open for days on an old page. When the page --
// --- comes back on screen, and every few minutes, it compares /version with the version it -----
// --- started with and reloads itself, unless you are typing a mission or drawing on the map. --
const VERSION_CHECK_MS = 5 * 60 * 1000;
let pageVersion = null;

async function checkPageVersion() {
  let latest = null;
  try {
    const response = await fetch("/version", { cache: "no-store" });
    if (response.ok) latest = (await response.json()).version || null;
  } catch {
    return; // offline: try again later
  }
  if (!latest) return;
  if (pageVersion === null) {
    pageVersion = latest;
    return;
  }
  if (latest === pageVersion || tool || $("mission-text").value.trim()) return;
  keepSelections();
  location.reload();
}
checkPageVersion();
setInterval(checkPageVersion, VERSION_CHECK_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) checkPageVersion();
});

// --- Installable app: the service worker keeps the app shell for offline start only. --------
if ("serviceWorker" in navigator && window.isSecureContext) {
  try {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  } catch {
    // the page works the same without it
  }
}

if (phoneView) setUpPhoneView();

if (!token) {
  stopped = true;
  setLink("no token", "bad");
  showNotice(
    phoneView
      ? "No token: open the phone address (\"HORUS phone app: https://…#token=…\", printed by " +
        "scripts/phone_serve.sh) once; the home-screen app keeps it from then on."
      : "No token: open the address the service printed at start-up " +
        "(\"HORUS control app: http://127.0.0.1:…/#token=…\").",
  );
} else {
  followEvents();
  if (!phoneView) poll("/api/frame", 1000, showFrame, showNoCamera);
  poll("/api/state", 1000, showState, showNoState);
  runMap();
}
