"use strict";

const $ = id => document.getElementById(id);
const label = value => String(value ?? "").replaceAll("_", " ").replace(/^./, first => first.toUpperCase());
const time = value => value ? new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
const sessionStorageKey = "agent-browser-client";
const randomId = () => crypto.randomUUID?.() ?? Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
let clientId = sessionStorage.getItem(sessionStorageKey) || randomId();
sessionStorage.setItem(sessionStorageKey, clientId);
const clientChannel = "BroadcastChannel" in window ? new BroadcastChannel("agent-browser-clients") : null;
const clientInstance = randomId();

function distinguishClonedTabs() {
  if (!clientChannel) return Promise.resolve();
  let probing = true;
  clientChannel.onmessage = ({ data }) => {
    if (data?.client !== clientId) return;
    if (data.kind === "probe") clientChannel.postMessage({ kind: "claimed", client: clientId, instance: data.instance });
    if (probing && data.kind === "claimed" && data.instance === clientInstance) {
      clientId = randomId();
      sessionStorage.setItem(sessionStorageKey, clientId);
    }
  };
  clientChannel.postMessage({ kind: "probe", client: clientId, instance: clientInstance });
  return new Promise(resolve => setTimeout(() => { probing = false; resolve(); }, 100));
}

let capabilities = {};
let active = null;
let creating = false;
let frameUrl = null;
let renderedFrame = null;
let lastWheel = 0;
let touchGesture = null;
let pointerPress = null;
let editRequest = null;
let modelCatalog = null;
let modelCatalogRequest = null;
let groupsVisible = false;
let groupViewVersion = 0;
let activeGroup = null;
let groupSubmitting = false;
let groupDraftRequest = null;

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined && content !== null) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function notice(message, kind = "", id = "notice") {
  const box = $(id);
  box.textContent = message;
  box.dataset.kind = kind;
  box.hidden = !message;
}

function setText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

async function request(path, { method = "GET", data, body, headers, signal } = {}) {
  const requestHeaders = new Headers(headers);
  requestHeaders.set("X-Workspace-Client", clientId);
  if (data !== undefined) {
    requestHeaders.set("Content-Type", "application/json");
    body = JSON.stringify(data);
  }
  const response = await fetch(path, { method, headers: requestHeaders, body, signal, cache: "no-store" });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = Array.isArray(payload.detail) ? payload.detail.map(item => item.msg).join(", ") : payload.detail;
    const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response;
}

async function json(path, options) {
  return (await request(path, options)).json();
}

function endpoint(scope, suffix = "") {
  const branch = scope.branch ? `?branch_id=${encodeURIComponent(scope.branch)}` : "";
  return `/sessions/${encodeURIComponent(scope.id)}${suffix}${branch}`;
}

function updateOptions(select, items, selected, getValue, getLabel, emptyLabel) {
  const signature = JSON.stringify(items.map(item => [getValue(item), getLabel(item)]));
  if (select.dataset.signature !== signature) {
    const options = items.map(item => new Option(getLabel(item), getValue(item)));
    select.replaceChildren(...(options.length ? options : [new Option(emptyLabel, "")]));
    select.dataset.signature = signature;
  }
  if (selected) select.value = selected;
  select.disabled = !items.length;
}

function setConnection(state, description) {
  const connection = $("connection");
  connection.dataset.state = state;
  if (connection.textContent !== description) connection.textContent = description;
}

function feedRow(title, meta, content, className = "") {
  const row = element("article", null, `feed-row ${className}`);
  const heading = element("div", null, "row-meta");
  heading.append(element("strong", title), element("span", meta));
  row.append(heading);
  if (content) row.append(element("p", content));
  return row;
}

function addDetails(row, name, value) {
  if (!value || !Object.keys(value).length) return;
  const details = element("details", null, "row-detail");
  details.append(element("summary", name), element("pre", JSON.stringify(value, null, 2)));
  row.append(details);
}

function updateFeed(node, items, keyOf, makeRow, emptyText, suffix = "") {
  const signature = items.map(item => keyOf(item)).join("|") + suffix;
  if (node.dataset.signature === signature) return;
  const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 45;
  const openRows = new Set([...node.querySelectorAll("details[open]")].map(details => details.parentElement.dataset.key));
  const scroll = node.scrollTop;
  const rows = items.map(item => {
    const row = makeRow(item);
    row.dataset.key = keyOf(item);
    const details = row.querySelector("details");
    if (details) details.open = openRows.has(row.dataset.key);
    return row;
  });
  node.replaceChildren(...(rows.length ? rows : [element("p", emptyText, "feed-empty")]));
  node.dataset.signature = signature;
  node.scrollTop = atBottom ? node.scrollHeight : scroll;
}

function renderMessages(items) {
  updateFeed($("chat-list"), items, item => [item.id, item.content, item.content_json?.local_llm?.status].join(":"), item => {
    const row = feedRow(item.role === "user" ? "You" : label(item.role), time(item.created_at), item.content, item.role);
    const advisory = item.content_json?.local_llm;
    if (advisory?.status === "completed") addDetails(row, "Local model advisory", advisory);
    return row;
  }, "Messages will appear here when you start a task.");
}

function renderEvents(items) {
  $("activity-count").textContent = items.length ? String(items.length) : "";
  const filter = $("event-filter").value.trim().toLowerCase();
  const visible = items.filter(event => [event.actor, event.action_type, event.observation_summary, event.page_url].some(value => value?.toLowerCase().includes(filter)));
  updateFeed($("event-list"), visible, item => item.event_id, item => {
    const metadata = [label(item.actor), item.step_index && `Step ${item.step_index}`, time(item.created_at)].filter(Boolean).join(" · ");
    const row = feedRow(label(item.action_type), metadata, item.observation_summary || item.page_url);
    addDetails(row, "Event details", { page: item.page_url, action: item.action_payload, correlation_id: item.correlation_id });
    return row;
  }, filter ? "No activity matches this filter." : "Actions, results, and errors will appear here.", filter);
}

function ownsBrowser(scope) {
  return Boolean(scope?.browser?.lease_id && scope.session?.state?.control_mode === "manual");
}

function renderApproval(scope) {
  const state = scope?.session?.state;
  const correlation = state?.approval_correlation_id;
  $("approval").hidden = !(state?.approval_required && correlation);
  if ($("approval").hidden) return;
  const event = [...scope.events].reverse().find(item => item.correlation_id === correlation && item.action_payload?.planned_action);
  const pending = state.pending_approval ?? event?.action_payload.planned_action ?? {};
  const command = pending.command;
  const artifact = scope.browser?.artifacts?.find(item => item.id === command?.artifact_id);
  $("approval-summary").textContent = pending.summary || state.pending_action_summary || "Review the pending browser action.";
  const fields = [
    ["Action", label(pending.action_type ?? pending.kind)], ["Page", command?.url ? null : pending.url ?? pending.page_url ?? event?.page_url],
    ["Target", pending.target ?? pending.payload?.target_id ?? pending.payload?.selector], ["Destination", command?.url],
    ["Form endpoint", pending.form_action ? `${(pending.form_method || "").toUpperCase()} ${pending.form_action}`.trim() : null],
    ["Text", command?.text === "" ? "Clear field" : command?.text], ["Key", command?.key], ["Mouse button", command?.button],
    ["Coordinates", command?.x !== undefined ? `${command.x}, ${command.y}` : null],
    ["Scroll", command?.kind === "wheel" ? `${command.delta_x ?? 0}, ${command.delta_y ?? 0}` : null],
    ["Size", command?.kind === "resize" ? `${command.width} × ${command.height}` : null],
    ["File", artifact?.name ?? command?.artifact_id], ["Tab", command?.tab_id], ["Request", correlation],
  ].filter(([, value]) => value != null && value !== "");
  const details = $("approval-details");
  const signature = JSON.stringify(fields);
  if (details.dataset.signature !== signature) {
    details.replaceChildren(...fields.flatMap(([name, value]) => [element("dt", name), element("dd", value)]));
    details.dataset.signature = signature;
  }
  for (const id of ["approve-btn", "reject-btn", "cancel-action-btn"]) $(id).disabled = scope.controlBusy || state.control_mode === "manual";
  $("edit-action-btn").hidden = !pending.command;
  $("edit-action-btn").disabled = scope.controlBusy || state.control_mode === "manual";
}

function renderControls(scope = active) {
  const state = scope?.session?.state;
  const status = state?.run_status || "idle";
  const manual = state?.control_mode === "manual";
  const holder = ownsBrowser(scope);
  const editable = holder && scope.browser.ready && !scope.controlBusy;
  const running = ["queued", "planning", "navigating", "extracting", "running"].includes(status);
  const canPause = running || (status === "awaiting_approval" && !state.operator_paused);
  const resumable = ["paused", "interrupted", "failed", "awaiting_input"].includes(status);
  const idle = ["completed", "cancelled", "idle"].includes(status);
  $("owner").textContent = manual ? holder ? "You have control" : "Manual control" : state ? "Agent control" : "Waiting for session";
  $("owner").dataset.mode = state?.control_mode ?? "";
  if ($("run-status").textContent !== label(status)) $("run-status").textContent = label(status);
  $("run-status").dataset.status = status;
  const modelStatus = state?.model_status === "waiting" ? "Waiting for a model slot" :
    state?.model_status === "thinking" ? "Model is deciding the next step" : "";
  const pausedReason = state?.operator_paused && ["awaiting_approval", "paused"].includes(status) ?
    (status === "awaiting_approval" ? "Paused by you. Approval waits for Resume." : "Paused by you.") : "";
  $("current-action").textContent = [pausedReason, state?.pending_action_summary || state?.last_error_message || modelStatus].filter(Boolean).join(" · ");
  $("pause-btn").disabled = !canPause || scope.controlBusy;
  $("resume-btn").disabled = !resumable || manual || scope.controlBusy;
  $("takeover-btn").hidden = holder && scope.browser.ready;
  $("takeover-btn").textContent = holder ? "Reconnect browser" : "Take control";
  $("takeover-btn").disabled = !capabilities.live_browser || !state || scope.controlBusy;
  $("release-btn").hidden = !holder;
  $("release-btn").disabled = Boolean(scope?.controlBusy);
  $("stop-btn").disabled = !capabilities.live_browser || !state || idle || scope.controlBusy;
  $("replan-btn").disabled = !capabilities.live_browser || !state || manual || scope.controlBusy;
  $("history-btn").disabled = !state;
  $("demo-btn").hidden = !capabilities.live_browser || !capabilities.demo_url;
  $("demo-btn").disabled = Boolean(scope?.controlBusy || running || manual || creating);
  $("send-btn").disabled = creating || Boolean(scope?.controlBusy);
  $("new-session-btn").disabled = creating;
  for (const id of ["new-tab", "back", "forward", "reload", "navigate", "fit-viewport", "upload-btn", "text-btn"]) $(id).disabled = !editable;
  $("address").readOnly = !editable;
  $("viewport").dataset.manual = String(editable);
  $("viewport-help").textContent = editable ? "Focus the browser to type. Shift+Escape returns focus to the workspace." :
    holder ? "Reconnect to restore the browser from its last saved location." :
    manual ? "Browser control is held by another workspace tab." : "Take control to click, type, and scroll in the browser.";
  $("artifacts-btn").disabled = !scope?.browser?.artifacts?.length;
  $("close-browser-btn").disabled = !scope?.browser?.ready || scope.controlBusy || !(holder || (!manual && (idle || resumable || status === "awaiting_approval")));
  renderApproval(scope);
}

function renderBrowser(scope) {
  const browser = scope?.browser;
  const state = scope?.session?.state;
  const live = Boolean(browser?.ready);
  const title = browser?.title || state?.last_known_title || "Browser view";
  $("browser-title").textContent = title;
  $("browser-title").title = title;
  if (document.activeElement !== $("address") || !ownsBrowser(scope)) $("address").value = browser?.url || state?.last_known_url || "";
  const tabs = browser?.tabs ?? [];
  const signature = JSON.stringify([tabs, browser?.active_tab_id, ownsBrowser(scope), scope?.controlBusy]);
  if ($("browser-tabs").dataset.signature !== signature) {
    $("browser-tabs").replaceChildren(...tabs.map(tab => {
      const selected = tab.tab_id === browser.active_tab_id;
      const name = tab.title || tab.url || "New tab";
      const group = element("div", null, `browser-tab${selected ? " active" : ""}`);
      const select = element("button", name);
      select.title = name;
      select.setAttribute("aria-pressed", String(selected));
      select.disabled = !ownsBrowser(scope) || scope.controlBusy;
      select.onclick = () => queueCommand({ kind: "switch_tab", tab_id: tab.tab_id });
      const close = element("button", "×");
      close.setAttribute("aria-label", `Close ${name}`);
      close.disabled = !ownsBrowser(scope) || scope.controlBusy;
      close.onclick = () => queueCommand({ kind: "close_tab", tab_id: tab.tab_id });
      group.append(select, close);
      return group;
    }));
    $("browser-tabs").dataset.signature = signature;
  }
  if (renderedFrame && (!live || renderedFrame.tabId !== browser.active_tab_id)) clearFrame();
  $("viewport-title").textContent = live ? "Waiting for browser view" : "Start a browsing task";
  $("viewport-description").textContent = !capabilities.live_browser ? "Live browsing is disabled for this server." :
    live ? "The shared browser is opening its first frame." : "The live browser will appear here. Take control to browse in the same session.";
  renderControls(scope);
}

function renderWorkspace(scope, workspace) {
  if (scope !== active) return;
  scope.session = workspace.session;
  scope.messages = workspace.messages;
  scope.events = workspace.events;
  updateOptions($("branch-select"), scope.session.available_branches, scope.branch, item => item.branch_id, item => item.name, "No branches");
  const context = scope.session.root_task || scope.session.title || "Instructions and activity";
  const model = scope.session.state?.model_name;
  $("agent-goal").textContent = model ? `${model} · ${context}` : context;
  renderMessages(scope.messages);
  renderEvents(scope.events);
  renderBrowser(scope);
}

async function readWorkspace(scope) {
  const version = scope.streamVersion;
  const readVersion = ++scope.readVersion;
  const workspace = await json(endpoint(scope, "/workspace"), { signal: scope.controller.signal });
  if (scope.streamVersion === version && scope.readVersion === readVersion) renderWorkspace(scope, workspace);
}

function connectStream(scope) {
  const stream = new EventSource(endpoint(scope, "/stream"));
  scope.stream = stream;
  stream.onopen = () => {
    if (scope !== active) return;
    scope.push = true;
    setConnection("online", "Live updates");
  };
  stream.addEventListener("workspace", event => {
    if (scope !== active) return;
    try {
      const workspace = JSON.parse(event.data);
      scope.streamVersion += 1;
      scope.push = true;
      renderWorkspace(scope, workspace);
      setConnection("online", "Live updates");
    } catch (error) {
      scope.push = false;
      setConnection("retry", "Refreshing");
    }
  });
  stream.onerror = () => {
    if (scope !== active) return;
    scope.push = false;
    setConnection("retry", "Reconnecting");
  };
}

async function refreshBrowser(scope) {
  if (!capabilities.live_browser || scope !== active) return;
  const version = scope.browserVersion;
  const browser = await json(endpoint(scope, "/browser"), { signal: scope.controller.signal });
  if (scope !== active || version !== scope.browserVersion) return;
  scope.browser = browser;
  renderBrowser(scope);
}

function clearFrame() {
  renderedFrame = null;
  pointerPress = null;
  touchGesture = null;
  const frame = $("browser-frame");
  frame.hidden = true;
  frame.removeAttribute("src");
  $("viewport-empty").hidden = false;
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  frameUrl = null;
}

async function refreshFrame(scope) {
  const browser = scope.browser;
  const version = scope.browserVersion;
  const response = await request(endpoint(scope, "/browser/frame"), { signal: scope.controller.signal });
  const revision = response.headers.get("X-Browser-Revision");
  const tabId = response.headers.get("X-Browser-Tab-Id");
  const frameId = response.headers.get("X-Browser-Frame-Id");
  if (!revision || !tabId) return;
  const url = URL.createObjectURL(await response.blob());
  try {
    const next = new Image();
    next.src = url;
    await next.decode();
    if (scope !== active || scope.browser !== browser || scope.browserVersion !== version ||
        !browser?.ready || browser.active_tab_id !== tabId) return;
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    frameUrl = url;
    const frame = $("browser-frame");
    frame.src = url;
    renderedFrame = { revision, tabId, frameId, width: next.naturalWidth, height: next.naturalHeight };
    frame.hidden = false;
    $("viewport-empty").hidden = true;
  } finally {
    if (frameUrl !== url) URL.revokeObjectURL(url);
  }
}

async function browserLoop(scope) {
  if (scope !== active) return;
  try {
    if (!document.hidden) {
      if ((!groupsVisible || ownsBrowser(scope)) && Date.now() >= scope.nextBrowser) {
        scope.nextBrowser = Date.now() + 1000;
        await refreshBrowser(scope);
      }
      if (!groupsVisible && scope.browser?.ready) await refreshFrame(scope);
    }
  } catch (error) {
    if (scope === active && ![404, 409, 503].includes(error.status) && error.name !== "AbortError") {
      notice(`Browser view: ${error.message}`, "error");
    }
  } finally {
    if (scope === active) setTimeout(() => browserLoop(scope), scope.browser?.ready && !groupsVisible && !document.hidden ? 250 : 1000);
  }
}

async function selectScope(id, branch = null) {
  if (active) {
    active.stream?.close();
    active.controller.abort();
  }
  clearFrame();
  notice("");
  const scope = {
    id, branch, controller: new AbortController(), streamVersion: 0, browserVersion: 0, readVersion: 0,
    messages: [], events: [], commandQueue: [], nextBrowser: 0,
  };
  active = scope;
  setConnection("retry", "Connecting");
  renderMessages([]);
  renderEvents([]);
  renderBrowser(scope);
  try {
    const workspace = await json(endpoint(scope, "/workspace"), { signal: scope.controller.signal });
    if (scope !== active) return;
    scope.branch = workspace.session.selected_branch_id;
    sessionStorage.setItem("agent-browser-session", scope.id);
    sessionStorage.setItem("agent-browser-branch", scope.branch);
    renderWorkspace(scope, workspace);
    connectStream(scope);
  } catch (error) {
    if (scope !== active) return;
    setConnection("offline", "Disconnected");
    notice(error.message, "error");
  }
  if (capabilities.live_browser) browserLoop(scope);
}

async function refreshSessions(selected) {
  const sessions = await json("/sessions");
  updateOptions($("session-select"), sessions, selected, item => item.session_id, item => item.title || item.session_id.slice(0, 8), "No sessions");
  return sessions;
}

async function newSession(title = "Browser session") {
  if (creating) return;
  creating = true;
  renderControls();
  try {
    const model = $("new-session-model").value;
    const data = model && model !== "default" ? { title, model_profile: model } : { title };
    const session = await json("/sessions", { method: "POST", data });
    await refreshSessions(session.session_id);
    await selectScope(session.session_id, session.selected_branch_id);
  } finally {
    creating = false;
    renderControls();
  }
}

async function control(suffix, data = {}) {
  const scope = active;
  if (!scope?.session || scope.controlBusy) return;
  scope.controlBusy = true;
  scope.commandQueue.length = 0;
  renderBrowser(scope);
  notice("");
  try {
    const response = await json(endpoint(scope, suffix), { method: "POST", data: { branch_id: scope.branch, ...data } });
    if (scope !== active) return;
    if (!scope.push) await readWorkspace(scope);
    if (capabilities.live_browser) await refreshBrowser(scope).catch(() => {});
    if (response.message) notice(response.message);
    return response;
  } catch (error) {
    const noticeId = suffix === "/edit-action" ? "edit-action-notice" : $("history-dialog").open ? "history-notice" : "notice";
    if (scope === active) notice(error.message, "error", noticeId);
  } finally {
    scope.controlBusy = false;
    if (scope === active) renderBrowser(scope);
  }
}

async function sendInstruction(content) {
  if (!active?.session) await newSession();
  return control("/messages", { content, trigger_agents: true });
}

function queueCommand(command, frameRevision = null, frameId = null) {
  const scope = active;
  if (!ownsBrowser(scope) || scope.controlBusy || scope.commandQueue.length >= 64) return;
  const usesFocus = ["type", "key", "upload"].includes(command.kind);
  const display = usesFocus ? { ...renderedFrame, url: scope.browser.url } : null;
  scope.commandQueue.push({ command, frameRevision, frameId, display });
  if (!scope.commandRunning) processCommands(scope);
}

function focusedRevision(scope, display) {
  const browser = scope.browser;
  if (display.tabId !== browser.active_tab_id || display.url !== browser.url) return null;
  const accepted = scope.lastAcceptedFocus;
  if (display.revision === browser.revision || (accepted?.display === display.revision && accepted?.browser === browser.revision)) {
    return browser.revision;
  }
  return null;
}

async function processCommands(scope) {
  scope.commandRunning = true;
  try {
    while (scope.commandQueue.length && scope === active && ownsBrowser(scope) && !scope.controlBusy) {
      const { command, frameRevision, frameId, display } = scope.commandQueue.shift();
      const previous = scope.browser;
      const revision = display ? focusedRevision(scope, display) : frameRevision ?? previous.revision;
      if (!revision) {
        scope.commandQueue.length = 0;
        notice("Wait for the current browser view, then focus the field and try again.");
        break;
      }
      const displayedRevision = display?.revision ?? frameRevision ?? renderedFrame?.revision;
      scope.browserVersion += 1;
      try {
        const browser = await json(endpoint(scope, "/browser/command"), {
          method: "POST",
          data: {
            lease_id: previous.lease_id, expected_revision: revision,
            command_id: randomId(), command, ...(frameId ? { frame_id: frameId } : {}),
          },
        });
        if (scope !== active) return;
        scope.browserVersion += 1;
        scope.browser = browser;
        const samePage = browser.active_tab_id === previous.active_tab_id && browser.url === previous.url;
        scope.lastAcceptedFocus = samePage && displayedRevision ? { display: displayedRevision, browser: browser.revision } : null;
        renderBrowser(scope);
      } catch (error) {
        if (scope !== active) return;
        scope.commandQueue.length = 0;
        notice(error.message, "error");
        await refreshBrowser(scope).catch(() => {});
      }
    }
  } finally {
    scope.commandRunning = false;
  }
}

function pointerPosition(event) {
  const frame = $("browser-frame");
  if (frame.hidden || !frame.complete || frame.currentSrc !== frameUrl || !renderedFrame || renderedFrame.tabId !== active?.browser?.active_tab_id) return null;
  const bounds = frame.getBoundingClientRect();
  if (!bounds.width || !bounds.height || event.clientX < bounds.left || event.clientX >= bounds.right ||
      event.clientY < bounds.top || event.clientY >= bounds.bottom) return null;
  const width = renderedFrame.width;
  const height = renderedFrame.height;
  return {
    x: Math.floor((event.clientX - bounds.left) * width / bounds.width),
    y: Math.floor((event.clientY - bounds.top) * height / bounds.height),
  };
}

function wheel(event, dx, dy) {
  const point = pointerPosition(event);
  if (!point || performance.now() - lastWheel < 70) return;
  lastWheel = performance.now();
  const delta_x = Math.max(-4000, Math.min(4000, Math.round(dx)));
  const delta_y = Math.max(-4000, Math.min(4000, Math.round(dy)));
  if (delta_x || delta_y) queueCommand({ kind: "wheel", ...point, delta_x, delta_y }, renderedFrame.revision);
}

function installViewportInput() {
  const viewport = $("viewport");
  viewport.addEventListener("pointerdown", event => {
    if (!ownsBrowser(active) || !pointerPosition(event)) return;
    pointerPress = { id: event.pointerId, revision: renderedFrame.revision, frameId: renderedFrame.frameId };
    viewport.focus({ preventScroll: true });
    if (event.pointerType === "touch") {
      touchGesture = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
      viewport.setPointerCapture(event.pointerId);
    }
  });
  viewport.addEventListener("pointermove", event => {
    if (!touchGesture || touchGesture.id !== event.pointerId) return;
    const dx = touchGesture.x - event.clientX;
    const dy = touchGesture.y - event.clientY;
    if (Math.abs(dx) + Math.abs(dy) < 8) return;
    touchGesture.moved = true;
    if (performance.now() - lastWheel >= 70) {
      wheel(event, dx, dy);
      touchGesture.x = event.clientX;
      touchGesture.y = event.clientY;
    }
  });
  viewport.addEventListener("pointercancel", () => { touchGesture = null; pointerPress = null; });
  viewport.addEventListener("pointerup", event => {
    const moved = touchGesture?.moved;
    const press = pointerPress;
    touchGesture = null;
    pointerPress = null;
    const point = pointerPosition(event);
    if (!point || !ownsBrowser(active) || moved || press?.id !== event.pointerId) return;
    if (press.revision !== renderedFrame.revision) {
      notice("The browser view changed. Click the current page to continue.");
      return;
    }
    const button = ["left", "middle", "right"][event.button] || "left";
    queueCommand({ kind: "click", ...point, button }, renderedFrame.revision, press.frameId);
  });
  viewport.addEventListener("contextmenu", event => { if (ownsBrowser(active)) event.preventDefault(); });
  viewport.addEventListener("wheel", event => {
    if (!ownsBrowser(active) || !pointerPosition(event)) return;
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
    wheel(event, event.deltaX * unit, event.deltaY * unit);
  }, { passive: false });
  viewport.addEventListener("paste", event => {
    if (!ownsBrowser(active)) return;
    const text = event.clipboardData?.getData("text/plain");
    if (!text) return;
    event.preventDefault();
    queueCommand({ kind: "type", text });
  });
  viewport.addEventListener("keydown", event => {
    if (event.target !== viewport || !ownsBrowser(active)) return;
    if (event.shiftKey && event.key === "Escape") {
      event.preventDefault();
      $("release-btn").focus();
      return;
    }
    if (["Control", "Alt", "Meta", "Shift", "Dead", "Process", "Unidentified"].includes(event.key) || event.isComposing) return;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "v") return;
    event.preventDefault();
    if ([...event.key].length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
      queueCommand({ kind: "type", text: event.key });
      return;
    }
    const baseKey = event.key.length === 1 ? event.code : event.key;
    const key = [
      event.ctrlKey && "Control", event.metaKey && "Meta", event.altKey && "Alt", event.shiftKey && "Shift",
      baseKey,
    ].filter(Boolean).join("+");
    queueCommand({ kind: "key", key });
  });
}

function setPanel(panel, focus = false) {
  for (const key of ["conversation", "activity"]) {
    const selected = key === panel;
    const tab = $(`${key}-tab`);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(key).hidden = !selected;
    if (selected && focus) tab.focus();
  }
}

async function loadHistory() {
  const scope = active;
  if (!scope?.session) return;
  notice("", "", "history-notice");
  try {
    const [checkpoints, replay] = await Promise.all([
      json(endpoint(scope, "/checkpoints")), json(endpoint(scope, "/replay")),
    ]);
    if (scope !== active) return;
    updateOptions($("checkpoint-select"), checkpoints, checkpoints.at(-1)?.checkpoint_id,
      item => item.checkpoint_id, item => item.label, "No saved checkpoints");
    $("branch-btn").disabled = !checkpoints.length;
    updateFeed($("replay-list"), replay.timeline, item => item.source_id, item =>
      feedRow(label(item.action_type || item.message_role || item.kind), time(item.created_at), item.content),
    "This branch has no recorded activity yet.");
  } catch (error) {
    notice(error.message, "error", "history-notice");
  }
}

async function openArtifacts() {
  const scope = active;
  if (!scope?.browser) return;
  const nodes = scope.browser.artifacts.map(artifact => {
    const line = element("div", null, "feed-row");
    const link = element("a", artifact.name);
    link.href = endpoint(scope, `/browser/artifacts/${encodeURIComponent(artifact.id)}`);
    link.download = artifact.name;
    link.addEventListener("click", async event => {
      event.preventDefault();
      try {
        const response = await request(link.href);
        const url = URL.createObjectURL(await response.blob());
        const download = element("a");
        download.href = url;
        download.download = artifact.name;
        download.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        notice(error.message, "error");
        $("artifacts-dialog").close();
      }
    });
    line.append(link, element("span", Number.isFinite(artifact.size) ? ` · ${artifact.size.toLocaleString()} bytes` : ""));
    if (artifact.kind === "upload" && ownsBrowser(scope)) {
      const attach = element("button", "Attach to page", "attach-file");
      attach.onclick = () => {
        $("artifacts-dialog").close();
        queueCommand({ kind: "upload", artifact_id: artifact.id });
      };
      line.append(attach);
    }
    return line;
  });
  $("artifact-list").replaceChildren(...nodes);
  $("artifacts-dialog").showModal();
}

function modelOption(model) {
  return model.model && model.label !== model.model ? `${model.label} · ${model.model}` : model.label || model.model || model.id;
}

function setModelOptions(select) {
  const models = modelCatalog ? [modelCatalog.default, ...modelCatalog.profiles] : [{ id: "default", label: "Workspace default" }];
  const selected = models.some(model => model.id === select.value) ? select.value : "default";
  updateOptions(select, models, selected, model => model.id, modelOption, "Workspace default");
}

async function loadModelCatalog() {
  if (modelCatalog) return modelCatalog;
  if (!modelCatalogRequest) modelCatalogRequest = json("/workspace/models");
  try {
    modelCatalog = await modelCatalogRequest;
    $("new-session-model-field").hidden = !modelCatalog.profiles.length;
    setModelOptions($("new-session-model"));
    for (const select of $("group-task-fields").querySelectorAll(".group-task-model")) setModelOptions(select);
    const slots = modelCatalog.inference_parallelism;
    setText($("group-model-capacity"), slots === 1 ? "One model request runs at a time. Browser tasks can overlap." :
      `Up to ${slots} model requests can run at once.`);
    return modelCatalog;
  } finally {
    modelCatalogRequest = null;
  }
}

function updateDraftRows() {
  const rows = [...$("group-task-fields").children];
  const max = modelCatalog?.max_group_tasks ?? 6;
  rows.forEach((row, index) => {
    setText(row.querySelector("legend"), `Task ${index + 1}`);
    const remove = row.querySelector(".group-task-remove");
    remove.disabled = rows.length <= 2;
    remove.setAttribute("aria-label", `Remove task ${index + 1}`);
    row.disabled = groupSubmitting;
  });
  $("group-title").disabled = groupSubmitting;
  $("group-form").setAttribute("aria-busy", String(groupSubmitting));
  $("new-group-btn").disabled = groupSubmitting;
  $("add-group-task-btn").disabled = groupSubmitting || rows.length >= max;
  $("start-group-btn").disabled = groupSubmitting || max < 2;
  setText($("start-group-btn"), groupSubmitting ? "Starting group…" : "Start group");
}

function addDraftTask(focus = false) {
  const rows = $("group-task-fields");
  if (rows.children.length >= (modelCatalog?.max_group_tasks ?? 6)) return;
  const row = $("group-task-template").content.firstElementChild.cloneNode(true);
  setModelOptions(row.querySelector(".group-task-model"));
  row.querySelector(".group-task-remove").onclick = () => {
    row.remove();
    updateDraftRows();
    $("add-group-task-btn").focus();
  };
  rows.append(row);
  updateDraftRows();
  if (focus) row.querySelector(".group-task-goal").focus();
}

function stopGroupStream() {
  activeGroup?.stream?.close();
  activeGroup?.controller.abort();
  activeGroup = null;
}

function showGroupDraft(focus = false) {
  stopGroupStream();
  $("group-select").value = "";
  $("group-overview").hidden = true;
  $("group-form").hidden = false;
  notice("", "", "group-notice");
  if (!$("group-task-fields").children.length) {
    addDraftTask();
    addDraftTask();
  }
  updateDraftRows();
  if (focus) $("group-title").focus();
}

async function refreshGroups(selected) {
  const version = groupViewVersion;
  const groups = await json("/task-groups");
  if (version !== groupViewVersion) return groups;
  const current = activeGroup?.id ?? ($("group-form").hidden ? selected : "");
  const select = $("group-select");
  const choices = [{ group_id: "", title: groups.length ? "Choose a group" : "No saved groups" }, ...groups];
  updateOptions(select, choices, current, group => group.group_id, group => group.title || "Task group", "No saved groups");
  select.value = groups.some(group => group.group_id === current) ? current : "";
  select.disabled = !groups.length;
  return groups;
}

function setGroupConnection(state, text) {
  const connection = $("group-connection");
  connection.dataset.state = state;
  setText(connection, text);
}

function groupPath(id) {
  return `/task-groups/${encodeURIComponent(id)}`;
}

function taskStatus(status) {
  return {
    awaiting_approval: "Approval required", awaiting_input: "Needs input", manual: "Manual control",
    attention: "Needs attention", settled: "Finished", active: "In progress",
  }[status] || label(status || "queued");
}

function createTaskCard(task, index) {
  const card = element("article", null, "group-task-card");
  const heading = element("div", null, "group-task-card-heading");
  const title = element("h4");
  const status = element("span", null, "badge");
  heading.append(title, status);
  const model = element("p", null, "group-task-model-name");
  const activity = element("p", null, "group-task-progress");
  const goal = element("details", null, "group-task-original-goal");
  goal.append(element("summary", "Task goal"), element("p", task.goal || ""));
  goal.hidden = !task.goal;
  const answer = element("p", null, "group-task-answer");
  const error = element("p", null, "notice");
  error.dataset.kind = "error";
  const open = element("button", "Open task workspace");
  open.type = "button";
  open.onclick = async () => {
    try {
      await refreshSessions(task.session_id);
      hideTaskGroups(false);
      await selectScope(task.session_id, task.branch_id);
      $("browser-heading").setAttribute("tabindex", "-1");
      $("browser-heading").focus();
    } catch (error) {
      notice(error.message, "error", groupsVisible ? "group-notice" : "notice");
    }
  };
  card.append(heading, model, activity, goal, answer, error, open);
  return { card, title, status, model, activity, answer, error, open, number: index + 1 };
}

function renderGroup(scope, group) {
  if (scope !== activeGroup) return;
  setText($("group-heading"), group.title || "Task group");
  const status = $("group-status");
  status.dataset.status = group.status;
  setText(status, taskStatus(group.status));
  const counts = group.counts;
  const progress = [`${counts.terminal} of ${counts.total} finished`];
  if (counts.running) progress.push(`${counts.running} running`);
  if (counts.queued) progress.push(`${counts.queued} queued`);
  if (counts.attention) progress.push(`${counts.attention} need attention`);
  setText($("group-counts"), progress.join(" · "));
  const container = $("group-task-results");
  group.tasks.forEach((task, index) => {
    let view = scope.cards.get(task.session_id);
    if (!view) {
      view = createTaskCard(task, index);
      scope.cards.set(task.session_id, view);
      container.append(view.card);
    }
    const title = task.title || `Task ${view.number}`;
    setText(view.title, title);
    view.status.dataset.status = task.run_status;
    setText(view.status, taskStatus(task.run_status));
    const model = task.model_name || (task.model_profile === "default" ? "Workspace default" : task.model_profile);
    const modelActivity = task.model_status === "waiting" ? " · Waiting for a model slot" :
      task.model_status === "thinking" ? " · Model is deciding" : "";
    setText(view.model, `${model || "Workspace default"}${modelActivity}`);
    setText(view.activity, task.activity || "");
    view.activity.hidden = !task.activity;
    setText(view.answer, task.answer || "");
    view.answer.hidden = !task.answer;
    setText(view.error, task.error || "");
    view.error.hidden = !task.error;
    setText(view.open, task.attention ? "Review task" : "Open task workspace");
    view.open.classList.toggle("primary", Boolean(task.attention));
    view.open.setAttribute("aria-label", task.attention ? `Review ${title}` : `Open ${title} workspace`);
  });
}

async function readGroup(scope) {
  const version = scope.version;
  const snapshot = await json(groupPath(scope.id), { signal: scope.controller.signal });
  if (version === scope.version) renderGroup(scope, snapshot);
}

function connectGroupStream(scope) {
  const stream = new EventSource(`${groupPath(scope.id)}/stream`);
  scope.stream = stream;
  stream.onopen = () => {
    if (scope !== activeGroup) return;
    scope.push = true;
    setGroupConnection("online", "Live updates");
  };
  stream.addEventListener("group", event => {
    if (scope !== activeGroup) return;
    try {
      const group = JSON.parse(event.data);
      scope.version += 1;
      renderGroup(scope, group);
    } catch (error) {
      scope.push = false;
      setGroupConnection("retry", "Refreshing");
    }
  });
  stream.onerror = () => {
    if (scope !== activeGroup) return;
    scope.push = false;
    setGroupConnection("retry", "Reconnecting");
  };
}

async function showGroup(id, snapshot = null, focus = false) {
  stopGroupStream();
  $("group-form").hidden = true;
  $("group-overview").hidden = false;
  $("group-task-results").replaceChildren();
  setText($("group-heading"), "Loading group…");
  setText($("group-status"), "");
  setText($("group-counts"), "");
  setGroupConnection("retry", "Connecting");
  notice("", "", "group-notice");
  const scope = { id, controller: new AbortController(), cards: new Map(), version: 0 };
  activeGroup = scope;
  sessionStorage.setItem("agent-browser-group", id);
  try {
    if (snapshot) renderGroup(scope, snapshot);
    else await readGroup(scope);
    if (scope !== activeGroup) return;
    connectGroupStream(scope);
    if (focus) $("group-heading").focus();
  } catch (error) {
    if (scope !== activeGroup) return;
    setText($("group-heading"), "Group unavailable");
    setGroupConnection("offline", "Disconnected");
    notice(error.message, "error", "group-notice");
  }
}

function setGroupView(visible) {
  groupsVisible = visible;
  groupViewVersion += 1;
  $("task-groups-view").hidden = !visible;
  $("workspace-view").hidden = visible;
  $("workspace-session-controls").hidden = visible;
  $("connection").hidden = visible;
  $("task-groups-btn").setAttribute("aria-expanded", String(visible));
  setText($("task-groups-btn"), visible ? "Back to browser" : "Task groups");
  sessionStorage.setItem("agent-browser-view", visible ? "groups" : "workspace");
}

async function openTaskGroups(focus = true) {
  setGroupView(true);
  const version = groupViewVersion;
  if (focus) $("task-groups-heading").focus();
  const saved = sessionStorage.getItem("agent-browser-group");
  try {
    const [, groups] = await Promise.all([loadModelCatalog(), refreshGroups(saved)]);
    if (version !== groupViewVersion || activeGroup || !$("group-form").hidden) return;
    const selected = groups.find(group => group.group_id === saved) ?? groups[0];
    if (!selected) showGroupDraft();
    else {
      $("group-select").value = selected.group_id;
      await showGroup(selected.group_id, selected);
    }
  } catch (error) {
    if (version === groupViewVersion) notice(error.message, "error", "group-notice");
  }
}

function hideTaskGroups(focus = true) {
  stopGroupStream();
  $("group-form").hidden = true;
  $("group-overview").hidden = true;
  setGroupView(false);
  if (active) active.nextBrowser = 0;
  if (focus) $("task-groups-btn").focus();
}

async function submitGroup(event) {
  event.preventDefault();
  if (groupSubmitting) return;
  const rows = [...$("group-task-fields").children];
  const tasks = rows.map(row => ({
    title: row.querySelector(".group-task-title").value.trim() || undefined,
    goal: row.querySelector(".group-task-goal").value.trim(),
    model_profile: row.querySelector(".group-task-model").value || "default",
  }));
  const empty = tasks.findIndex(task => !task.goal);
  if (empty >= 0) {
    notice(`Enter a goal for task ${empty + 1}.`, "error", "group-notice");
    rows[empty].querySelector(".group-task-goal").focus();
    return;
  }
  const data = { title: $("group-title").value.trim() || undefined, tasks };
  const signature = JSON.stringify(data);
  if (groupDraftRequest?.signature !== signature) groupDraftRequest = { signature, id: randomId() };
  groupSubmitting = true;
  updateDraftRows();
  notice("", "", "group-notice");
  try {
    const group = await json("/task-groups", { method: "POST", data: { ...data, request_id: groupDraftRequest.id } });
    groupDraftRequest = null;
    $("group-title").value = "";
    $("group-task-fields").replaceChildren();
    sessionStorage.setItem("agent-browser-group", group.group_id);
    if (groupsVisible) {
      await showGroup(group.group_id, group, true);
      await refreshGroups(group.group_id).catch(() => {});
    }
  } catch (error) {
    if (groupsVisible) notice(error.message, "error", "group-notice");
  } finally {
    groupSubmitting = false;
    updateDraftRows();
  }
}

function bind() {
  const safe = callback => async event => {
    try { await callback(event); } catch (error) { notice(error.message, "error"); }
  };
  $("new-session-btn").onclick = safe(() => newSession(`Session ${new Date().toLocaleTimeString()}`));
  $("task-groups-btn").onclick = () => groupsVisible ? hideTaskGroups() : openTaskGroups();
  $("new-group-btn").onclick = () => showGroupDraft(true);
  $("group-select").onchange = event => event.target.value ? showGroup(event.target.value, null, true) : showGroupDraft(true);
  $("add-group-task-btn").onclick = () => addDraftTask(true);
  $("group-form").onsubmit = submitGroup;
  $("session-select").onchange = event => selectScope(event.target.value);
  $("branch-select").onchange = event => selectScope(active.id, event.target.value);
  $("chat-form").onsubmit = safe(async event => {
    event.preventDefault();
    const draft = $("chat-input").value;
    const content = draft.trim();
    if (content && await sendInstruction(content) && $("chat-input").value === draft) $("chat-input").value = "";
  });
  $("chat-input").onkeydown = event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  };
  $("demo-btn").onclick = safe(() => sendInstruction(`Demo: ${capabilities.demo_url}`));
  $("pause-btn").onclick = () => control("/pause");
  $("resume-btn").onclick = () => control("/resume");
  $("takeover-btn").onclick = () => control("/takeover");
  $("release-btn").onclick = () => control("/release", { lease_id: active?.browser?.lease_id, resume: true });
  $("stop-btn").onclick = () => control("/stop");
  $("close-browser-btn").onclick = () => control("/browser/close", { lease_id: active?.browser?.lease_id });
  $("replan-btn").onclick = () => control("/replan");
  for (const [id, suffix] of [["approve-btn", "/approve"], ["reject-btn", "/reject"], ["cancel-action-btn", "/cancel-action"]]) {
    $(id).onclick = () => control(suffix, { correlation_id: active?.session?.state?.approval_correlation_id });
  }
  $("edit-action-btn").onclick = () => {
    const state = active?.session?.state;
    if (!state?.pending_approval?.command) return;
    editRequest = { scope: active, correlation_id: state.approval_correlation_id };
    $("edit-action-input").value = JSON.stringify(state.pending_approval.command, null, 2);
    notice("", "", "edit-action-notice");
    $("edit-action-dialog").showModal();
  };
  $("edit-action-dialog").addEventListener("close", () => { editRequest = null; $("edit-action-input").value = ""; });
  $("edit-action-form").onsubmit = async event => {
    event.preventDefault();
    try {
      const command = JSON.parse($("edit-action-input").value);
      if (editRequest?.scope !== active || editRequest.correlation_id !== active.session.state.approval_correlation_id) {
        throw new Error("The approval changed. Close this editor and review the latest request.");
      }
      const response = await control("/edit-action", { correlation_id: editRequest.correlation_id, command });
      if (response) $("edit-action-dialog").close();
    } catch (error) {
      notice(error.message, "error", "edit-action-notice");
    }
  };
  $("history-btn").onclick = () => { $("history-dialog").showModal(); loadHistory(); };
  $("snapshot-btn").onclick = async () => {
    if (await control("/snapshot", { note: "Workspace checkpoint" })) await loadHistory();
  };
  $("branch-btn").onclick = async () => {
    const checkpoint = $("checkpoint-select").value;
    if (!checkpoint) return;
    const response = await control("/branch", {
      source_branch_id: active.branch, checkpoint_id: checkpoint, name: `Branch ${new Date().toLocaleTimeString()}`,
    });
    if (response?.branch_id) {
      $("history-dialog").close();
      await selectScope(response.session_id ?? active.id, response.branch_id);
    }
  };
  $("event-filter").oninput = () => renderEvents(active?.events ?? []);
  for (const panel of ["conversation", "activity"]) {
    $(`${panel}-tab`).onclick = () => setPanel(panel);
    $(`${panel}-tab`).onkeydown = event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? "conversation" : event.key === "End" ? "activity" : panel === "activity" ? "conversation" : "activity";
      setPanel(target, true);
    };
  }
  for (const [id, kind] of [["new-tab", "new_tab"], ["back", "back"], ["forward", "forward"], ["reload", "reload"]]) {
    $(id).onclick = () => queueCommand({ kind });
  }
  $("address-form").onsubmit = event => {
    event.preventDefault();
    let url = $("address").value.trim();
    if (url && !/^[a-z][a-z\d+.-]*:\/\//i.test(url) && url !== "about:blank") url = `https://${url}`;
    if (url) queueCommand({ kind: "open", url });
    $("address").blur();
  };
  $("fit-viewport").onclick = () => {
    const viewport = $("viewport");
    const width = Math.min(1920, Math.max(320, viewport.clientWidth));
    const height = Math.min(1200, Math.max(240, viewport.clientHeight));
    queueCommand({ kind: "resize", width, height });
  };
  $("upload-btn").onclick = () => $("upload-file").click();
  $("text-btn").onclick = () => $("text-dialog").showModal();
  $("text-dialog").addEventListener("close", () => { $("remote-text").value = ""; });
  $("text-form").onsubmit = event => {
    event.preventDefault();
    queueCommand({ kind: "type", text: $("remote-text").value });
    $("text-dialog").close();
    $("viewport").focus();
  };
  $("upload-file").onchange = safe(async event => {
    const scope = active;
    const file = event.target.files[0];
    event.target.value = "";
    if (!file || !ownsBrowser(scope)) return;
    const artifact = await json(endpoint(scope, "/browser/upload"), {
      method: "POST", headers: { "X-Artifact-Name": encodeURIComponent(file.name), "Content-Type": "application/octet-stream" }, body: file,
    });
    if (scope !== active) return;
    notice(`Stored ${artifact.name}. Open Files to attach it to a page.`);
    await refreshBrowser(scope);
  });
  $("artifacts-btn").onclick = safe(openArtifacts);
  installViewportInput();
}

async function boot() {
  bind();
  renderBrowser(null);
  try {
    await distinguishClonedTabs();
    capabilities = await json("/workspace/capabilities");
    $("task-groups-btn").hidden = !capabilities.live_browser;
    if (capabilities.live_browser) loadModelCatalog().catch(() => {});
    const saved = sessionStorage.getItem("agent-browser-session");
    const sessions = await refreshSessions(saved);
    const selected = sessions.find(item => item.session_id === saved) ?? sessions[0];
    if (selected) {
      $("session-select").value = selected.session_id;
      const branch = selected.session_id === saved ? sessionStorage.getItem("agent-browser-branch") : null;
      await selectScope(selected.session_id, branch);
    } else {
      setConnection("online", "Ready");
      renderBrowser(null);
    }
    if (capabilities.live_browser && sessionStorage.getItem("agent-browser-view") === "groups") await openTaskGroups(false);
  } catch (error) {
    setConnection("offline", "Disconnected");
    notice(error.message, "error");
  }
  setInterval(async () => {
    const scope = active;
    if (!scope?.session || scope.push || scope.refreshing || document.hidden) return;
    scope.refreshing = true;
    try {
      await readWorkspace(scope);
    } catch (error) {
      if (scope === active) setConnection("offline", "Disconnected");
    } finally {
      scope.refreshing = false;
    }
  }, 2000);
  setInterval(async () => {
    const scope = activeGroup;
    if (!scope || scope.push || scope.refreshing || document.hidden) return;
    scope.refreshing = true;
    try {
      await readGroup(scope);
      if (scope === activeGroup && !scope.push) {
        if (!scope.stream) connectGroupStream(scope);
        setGroupConnection("retry", "Refreshing");
      }
    } catch (error) {
      if (scope === activeGroup) setGroupConnection("offline", "Disconnected");
    } finally {
      scope.refreshing = false;
    }
  }, 2000);
}

boot();
