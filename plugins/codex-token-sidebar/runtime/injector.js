(function (scriptHash) {
  "use strict";

  var SCHEMA_VERSION = 3;
  var PERFORMANCE_ENABLED = __CODEX_TOKEN_SIDEBAR_PERFORMANCE__;
  var ROOT_ID = "codex-token-sidebar";
  var STYLE_ID = "codex-token-sidebar-style";
  var CONVERSATION_ID_RE =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  if (
    window.__codexTokenSidebarInstalled &&
    window.__codexTokenSidebarHash === scriptHash &&
    typeof window.__codexTokenSidebarState === "function" &&
    typeof window.__codexTokenSidebarUpdate === "function"
  ) {
    return;
  }

  if (window.__codexTokenSidebarDispose) {
    window.__codexTokenSidebarDispose();
  } else if (window.__codexTokenSidebarObserver) {
    window.__codexTokenSidebarObserver.disconnect();
  }
  var stale = document.getElementById(ROOT_ID);
  if (stale) stale.remove();
  var staleStyle = document.getElementById(STYLE_ID);
  if (staleStyle) staleStyle.remove();

  window.__codexTokenSidebarHash = scriptHash;
  window.__codexTokenSidebarData = null;
  var pageEpoch = crypto.randomUUID();
  var selectionEpoch = 0;
  var lastConversationId;
  var probeId = 0;
  var acceptedProbeId = 0;

  var cachedScroller = null;
  var hostSelectionDirty = true;
  var contextAnchor = null;
  var panel = null;
  var observedScroller = null;
  var observedParent = null;
  var disposed = false;
  var frameId = null;
  var lastHeartbeat = performance.now();
  var heartbeatTimeout = 45000;
  var readerStatus = "waiting";
  var healthNode = null;
  var performanceObserver = null;
  var healthText = "";
  var reroutesByConversation = new Map();
  var rerouteSequence = 0;
  var rerouteRevision = 0;
  var reroutesDirty = true;
  var observedRouteRoot = null;
  var lastRouteScanId = null;

  function isMainPage() {
    try {
      var url = new URL(window.location.href);
      if (url.protocol !== "app:" || url.host !== "-" || url.username || url.password
          || url.pathname !== "/index.html" || url.hash) return false;
      var routes = url.searchParams.getAll("initialRoute");
      if (routes.length > 1) return false;
      if (!routes.length) return true;
      var route = routes[0];
      var path = route.split(/[?#]/)[0];
      // Dedicated Page windows use the same entrypoint as chat windows.
      var routeQuery = new URL(route, "app://-").searchParams;
      if ((path === "/space" || path.startsWith("/space/"))
          && routeQuery.getAll("window").indexOf("page") !== -1) return false;
      return route.startsWith("/") && !route.startsWith("//") && !route.includes("\\")
        && !path.includes("%")
        && !["/avatar-overlay", "/detached-window", "/global-dictation", "/hotkey-window",
          "/chatgpt/quick-chat", "/chatgpt/quick-chat-prewarm"].some(function (prefix) {
            return path === prefix || path.startsWith(prefix + "/");
          });
    } catch (_) { return false; }
  }

  function receiveHeartbeat(health) {
    lastHeartbeat = performance.now();
    if (health && ["ok", "waiting", "deferred", "failed"].indexOf(health.reader) !== -1) {
      readerStatus = health.reader;
    }
    if (health && Number.isFinite(health.timeoutMs) && health.timeoutMs >= 45000) {
      heartbeatTimeout = health.timeoutMs;
    }
    renderHealth();
  }

  function hasMainShell() {
    return !!(window.codexWindowType === "electron" && window.electronBridge
      && document.getElementById("root"));
  }

  function renderHealth() {
    if (disposed || !panel) return;
    var message = performance.now() - lastHeartbeat > heartbeatTimeout
      ? "用量更新已停止，正在等待连接恢复…"
      : readerStatus === "deferred" ? "用量暂时无法读取，显示上次结果"
      : readerStatus === "failed" ? "用量暂时无法读取，正在重试…" : "";
    if (message === healthText) return;
    healthText = message;
    if (!message) {
      if (healthNode) healthNode.remove();
      healthNode = null;
    } else {
      if (!healthNode) {
        healthNode = text("div", "cts-health");
        healthNode.setAttribute("role", "status");
        panel.appendChild(healthNode);
      }
      healthNode.textContent = message;
    }
  }

  function isVisibleHost(node) {
    if (!node || !node.isConnected) return false;
    // Retained task trees can stay connected while hidden or inactive.
    for (var parent = node; parent; parent = parent.parentElement) {
      if (parent.hidden || parent.inert || parent.getAttribute("aria-hidden") === "true") return false;
    }
    return node.checkVisibility
      ? node.checkVisibility({ visibilityProperty: true, opacityProperty: true })
      : node.getClientRects().length > 0 && window.getComputedStyle(node).visibility === "visible";
  }

  function findScroller() {
    if (!hostSelectionDirty && isVisibleHost(cachedScroller)) return cachedScroller;
    hostSelectionDirty = false;
    var previous = cachedScroller;
    cachedScroller = null;
    // Current summary panels can render non-collapsible headings instead of
    // toggle buttons. Their explicit panel boundary owns the section container.
    var panels = Array.from(document.querySelectorAll(
      '[data-summary-panel-variant="summary"],[data-summary-panel-variant="dynamic-isle"]'
    )).filter(isVisibleHost);
    var hosts = new Set(), ambiguous = false;
    for (var p = 0; p < panels.length; p++) {
      var summary = panels[p], containers = new Set();
      // The scrollable variant has a direct scrolling wrapper and one inner
      // flex column. Do not select a section's nested list or code scroller.
      for (var c = 0; c < summary.children.length; c++) {
        var wrapper = summary.children[c];
        if (!wrapper.classList.contains("overflow-y-auto") || wrapper.children.length !== 1) continue;
        var content = wrapper.children[0];
        if (content.classList.contains("flex-col") && isVisibleHost(content)) containers.add(content);
      }
      if (!containers.size) {
        var sections = summary.querySelectorAll("section");
        for (var s = 0; s < sections.length; s++) {
          var section = sections[s], parent = section.parentElement;
          if (section.id === ROOT_ID || section.closest("[data-summary-panel-variant]") !== summary) continue;
          var enclosing = parent && parent.closest("section");
          if ((!enclosing || !summary.contains(enclosing)) && isVisibleHost(parent)) containers.add(parent);
        }
      }
      if (containers.size > 1) ambiguous = true;
      if (containers.size === 1) hosts.add(containers.values().next().value);
    }
    if (hosts.size || ambiguous) {
      // Multiple usable containers are ambiguous; wait rather than attaching the
      // selected conversation's statistics to another host.
      cachedScroller = !ambiguous && hosts.size === 1 ? hosts.values().next().value : null;
      return cachedScroller;
    }
    if (isVisibleHost(previous) && !previous.closest("[data-summary-panel-variant]")) {
      cachedScroller = previous;
      return cachedScroller;
    }
    var buttons = document.querySelectorAll(
      'section header button[class~="group/section-toggle"],' +
        'section header button[class*="section-toggle"]'
    );
    for (var i = 0; i < buttons.length; i++) {
      var section = buttons[i].closest("section");
      if (section && isVisibleHost(buttons[i]) && isVisibleHost(section.parentElement)) {
        cachedScroller = section.parentElement;
        return cachedScroller;
      }
    }
    return null;
  }

  function domFiber(node) {
    if (!node || !node.isConnected) return null;
    for (var key in node) {
      if (key.indexOf("__reactFiber$") === 0) {
        return node[key];
      }
    }
    return null;
  }

  function currentFiberPath(start, cache) {
    if (!start || typeof start !== "object") return null;
    var pending = [], seen = new Set(), cursor = start, path = null;
    while (cursor && typeof cursor === "object" && pending.length < 100) {
      if (cache.has(cursor)) { path = cache.get(cursor); break; }
      if (seen.has(cursor)) return null;
      seen.add(cursor);
      if (!cursor.return) {
        var current = cursor.stateNode && cursor.stateNode.current;
        if (!current || (current !== cursor && current !== cursor.alternate)) return null;
        path = [current];
        break;
      }
      pending.push(cursor);
      cursor = cursor.return;
    }
    if (!path) return null;
    // Walk down from the committed root using actual child membership. A
    // shared bailout subtree may retain old return pointers, so an upward
    // return chain alone cannot determine which branch is currently visible.
    var budget = 4000;
    for (var i = pending.length - 1; i >= 0; i--) {
      var original = pending[i], child = path[0].child;
      while (child && child !== original && child !== original.alternate && --budget > 0) {
        child = child.sibling;
      }
      if (!child || budget <= 0) return null;
      path = [child].concat(path);
      cache.set(original, path);
      if (original.alternate) cache.set(original.alternate, path);
    }
    return path;
  }

  function fiberConversationId(start, cache) {
    var path = currentFiberPath(domFiber(start), cache);
    if (!path) return null;
    for (var depth = 0; depth < path.length; depth++) {
      var fiber = path[depth];
      var bags = [fiber.memoizedProps, fiber.memoizedState];
      for (var b = 0; b < bags.length; b++) {
        var bag = bags[b];
        if (!bag || typeof bag !== "object") continue;
        for (var prop in bag) {
          if (
            prop === "conversationId" ||
            /[Cc]onversationId$/.test(prop)
          ) {
            var value = bag[prop];
            if (typeof value === "string" && CONVERSATION_ID_RE.test(value)) {
              return value;
            }
          }
        }
      }
    }
    return null;
  }

  function resolveConversationId() {
    if (!isVisibleHost(contextAnchor)) {
      contextAnchor = Array.from(document.querySelectorAll('[aria-label^="Context usage:"]')).find(isVisibleHost) || null;
    }
    var root = document.getElementById(ROOT_ID);
    var anchors = [
      contextAnchor,
      findScroller(),
      root && root.previousElementSibling,
    ];
    var cache = new WeakMap(), identities = new Set();
    for (var i = 0; i < anchors.length; i++) {
      if (!isVisibleHost(anchors[i])) continue;
      var id = fiberConversationId(anchors[i], cache);
      if (id) identities.add(id);
    }
    if (identities.size) return identities.size === 1 ? identities.values().next().value : null;
    // The composer exposes its identity even when its localized context label
    // or React props cannot be read. Empty portal slots have no CSS box; their
    // visible parent distinguishes them from retained, hidden task trees.
    var markers = document.querySelectorAll('[data-above-composer-portal][data-above-composer-conversation-id]');
    for (var j = 0; j < markers.length; j++) {
      var marker = markers[j], markerId = marker.getAttribute('data-above-composer-conversation-id');
      if (!CONVERSATION_ID_RE.test(markerId || '') || marker.hidden || marker.inert
          || marker.getAttribute('aria-hidden') === 'true') continue;
      if (isVisibleHost(marker) || (!marker.hasChildNodes() && isVisibleHost(marker.parentElement))) {
        identities.add(markerId);
      }
    }
    return identities.size === 1 ? identities.values().next().value : null;
  }

  function readConversationId() {
    var current = hostAdapter.conversationId();
    if (current !== lastConversationId) {
      lastConversationId = current;
      selectionEpoch++;
    }
    return current;
  }

  function observeRouteRoot() {
    var root = document.getElementById("root");
    if (root === observedRouteRoot) return root;
    routeObserver.disconnect();
    observedRouteRoot = root;
    reroutesDirty = true;
    hostSelectionDirty = true;
    // Summary panels may use a React portal outside #root. Observe native
    // host membership/visibility changes without polling all hosts when idle.
    if (root) routeObserver.observe(document.body || root, { childList: true, subtree: true,
      attributes: true, attributeFilter: ["hidden", "inert", "aria-hidden", "style", "class", "data-summary-panel-variant"] });
    return root;
  }

  function rerouteTime(item) {
    if (item.timeConflict) return null;
    if (item.startedAt != null) return item.startedAt * 1000;
    // Only UUIDv7 embeds a timestamp. Other IDs supply no chronology.
    if (/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(item.turnId)) {
      return parseInt(item.turnId.slice(0, 8) + item.turnId.slice(9, 13), 16);
    }
    return null;
  }

  function compareReroutes(a, b) {
    if (a.turnId === b.turnId) return b.itemIndex - a.itemIndex || a.eventKey.localeCompare(b.eventKey);
    var first = rerouteTime(a), second = rerouteTime(b);
    if (first == null || second == null) {
      return first == null && second == null ? b.sequence - a.sequence : first == null ? 1 : -1;
    }
    return second - first || b.turnId.localeCompare(a.turnId);
  }

  function rerouteReason(reason) {
    if (!reason) return "";
    return reason === "highRiskCyberActivity" ? "高风险网络安全活动" : "原因代码：" + reason;
  }

  function rerouteDescription(item) {
    return item.fromModel + " → " + item.toModel + (item.reason ? "（" + rerouteReason(item.reason) + "）" : "");
  }

  function recordReroute(conversationId, turnId, item, itemIndex, startedAt) {
    if (!item || item.type !== "modelRerouted"
        || typeof item.fromModel !== "string" || typeof item.toModel !== "string") return;
    var fromModel = item.fromModel.trim(), toModel = item.toModel.trim();
    if (!fromModel || !toModel || fromModel.length > 128 || toModel.length > 128
        || fromModel.toLowerCase() === toModel.toLowerCase()) return;
    var bucket = reroutesByConversation.get(conversationId);
    if (!bucket) {
      if (reroutesByConversation.size >= 16) reroutesByConversation.delete(reroutesByConversation.keys().next().value);
      bucket = new Map(); reroutesByConversation.set(conversationId, bucket);
    }
    var previous = rerouteSignature(conversationId);
    var itemId = typeof item.id === "string" && item.id && item.id.length <= 256 ? item.id : null;
    var key = turnId + "\u0000" + (itemId ? "id:" + itemId : "index:" + itemIndex);
    var reason = typeof item.reason === "string" && item.reason.length <= 128 ? item.reason : null;
    var time = typeof startedAt === "number" && Number.isFinite(startedAt) && startedAt > 0 && startedAt < 8640000000000 ? startedAt : null;
    var record = bucket.get(key);
    if (!record) {
      record = {eventKey:key, turnId:turnId, fromModel:fromModel, toModel:toModel,
        reason:reason, itemIndex:itemIndex, startedAt:time, sequence:++rerouteSequence, conflicts:[]};
      bucket.set(key, record);
    } else if (record.fromModel !== fromModel || record.toModel !== toModel
        || (record.reason && reason && record.reason !== reason)) {
      var variant = rerouteDescription({fromModel:fromModel, toModel:toModel, reason:reason});
      if (record.conflicts.indexOf(variant) === -1 && record.conflicts.length < 4) record.conflicts.push(variant);
    } else {
      if (!record.reason && reason) record.reason = reason;
      if (record.itemIndex !== itemIndex) record.orderConflict = true;
    }
    var sameTurn = Array.from(bucket.values()).filter(function (row) { return row.turnId === turnId; });
    var times = new Set(sameTurn.map(function (row) { return row.startedAt; }).filter(function (value) { return value != null; }));
    if (time != null) times.add(time);
    var conflict = times.size > 1 || sameTurn.some(function (row) { return row.timeConflict; });
    sameTurn.forEach(function (row) {
      row.timeConflict = conflict;
      if (times.size === 1) row.startedAt = times.values().next().value;
    });
    var ordered = Array.from(bucket.values()).sort(compareReroutes);
    ordered.slice(64).forEach(function (row) { bucket.delete(row.eventKey); });
    if (previous !== rerouteSignature(conversationId)) rerouteRevision++;
  }

  function scanNativeReroutes(conversationId) {
    var root = observeRouteRoot();
    if (!root || !conversationId || (!reroutesDirty && lastRouteScanId === conversationId)) return;
    reroutesDirty = false;
    lastRouteScanId = conversationId;
    var seenFibers = new WeakSet();
    var cache = new WeakMap();
    var nodes = root.querySelectorAll("*");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var path = currentFiberPath(domFiber(node), cache);
      if (!path) continue;
      for (var depth = 0; depth < path.length; depth++) {
        var fiber = path[depth];
        if (seenFibers.has(fiber)) break;
        seenFibers.add(fiber);
        var props = fiber.memoizedProps;
        if (props && props.conversationId === conversationId
            && props.turn && Array.isArray(props.turn.items)) {
          var turnId = props.turnId || props.turn.turnId || props.turn.id;
          if (typeof turnId === "string" && turnId) {
            for (var j = 0; j < props.turn.items.length; j++) {
              recordReroute(conversationId, turnId, props.turn.items[j], j, props.turn.startedAt);
            }
          }
        }
      }
    }
  }

  function recentReroutes(conversationId) {
    var bucket = reroutesByConversation.get(conversationId);
    return bucket ? Array.from(bucket.values()).sort(compareReroutes).slice(0, 3) : [];
  }

  function reroutesOrdered(conversationId) {
    var bucket = reroutesByConversation.get(conversationId);
    return !bucket || Array.from(bucket.values()).every(function (item) {
      return rerouteTime(item) != null && !item.orderConflict;
    });
  }

  function rerouteSignature(conversationId) {
    return JSON.stringify([recentReroutes(conversationId), reroutesOrdered(conversationId)]);
  }

  function findNativeTurnAnchor(turnId) {
    var root = document.getElementById("root");
    if (!root || typeof turnId !== "string") return null;
    var key = "history-content:turn:" + turnId;
    var nodes = root.querySelectorAll("[data-turn-key]");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].getAttribute("data-turn-key") === key && isVisibleHost(nodes[i])) {
        return nodes[i];
      }
    }
    return null;
  }

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
#codex-token-sidebar{--cts-muted:var(--color-token-text-secondary,currentColor);--cts-rule:rgba(128,128,128,.2);display:block;container-type:inline-size;padding:0 0 12px;user-select:none;color:var(--color-token-text-primary,inherit);font-size:12px;line-height:1.5;font-variant-numeric:tabular-nums}
#codex-token-sidebar *{box-sizing:border-box}
#codex-token-sidebar .cts-header{display:flex;align-items:center;height:30px;padding:0 16px;background:var(--color-token-dropdown-background,transparent)}
#codex-token-sidebar .cts-toggle{display:inline-flex;align-items:center;gap:6px;border:0;background:transparent;color:var(--cts-muted);font-size:14px;cursor:pointer;padding:2px 0}
#codex-token-sidebar .cts-toggle svg{width:14px;height:14px;opacity:.65;transition:transform .15s ease}
#codex-token-sidebar.cts-collapsed .cts-toggle svg{transform:rotate(-90deg)}
#codex-token-sidebar.cts-collapsed .cts-body{display:none}
#codex-token-sidebar .cts-body{padding:14px 16px 0}
#codex-token-sidebar .cts-performance{--cts-chart-accent:#4d8ee9;position:relative;margin:12px -4px 0;font-size:12px}
#codex-token-sidebar .cts-performance-title{font-size:12px;color:var(--cts-muted)}
#codex-token-sidebar .cts-performance-legend{display:flex;flex-wrap:wrap;gap:6px 10px;margin:8px 0;font-size:12px}
#codex-token-sidebar .cts-performance-legend>button{display:flex;gap:5px;align-items:center;padding:0;border:0;background:none;color:inherit;font:inherit;cursor:pointer}
#codex-token-sidebar .cts-performance-legend>button[data-active="true"],#codex-token-sidebar .cts-performance-legend>button:focus-visible{outline:none;box-shadow:0 1px 0 currentColor}
#codex-token-sidebar .cts-average-legend i{width:14px;height:2px;background:var(--cts-chart-accent)}
#codex-token-sidebar .cts-performance-plot{position:relative}
#codex-token-sidebar .cts-performance-summary{position:absolute;top:0;left:0;right:0;display:flex;align-items:baseline;justify-content:space-between;gap:8px}
#codex-token-sidebar .cts-performance-plot svg{display:block;overflow:visible;font-size:12px}
#codex-token-sidebar .cts-performance-plot text{font-size:12px}
#codex-token-sidebar .cts-turn-hit:focus-visible{outline:none;stroke:var(--cts-chart-accent);stroke-width:1}
#codex-token-sidebar .cts-detail-slot{min-height:42px;margin-top:6px}
#codex-token-sidebar .cts-turn-detail{color:var(--cts-muted);font-size:12px;pointer-events:none}
#codex-token-sidebar .cts-detail-time{line-height:18px}
#codex-token-sidebar .cts-detail-values{display:block;line-height:24px;overflow-wrap:anywhere}
#codex-token-sidebar .cts-health{padding:8px 16px 0;color:var(--cts-muted);font-size:12px}
#codex-token-sidebar .cts-credits{display:flex;flex-direction:column;min-width:0;padding-left:12px;font-size:12px}
#codex-token-sidebar .cts-credits-value{font-size:clamp(20px,9cqi,28px);line-height:1.4;font-weight:650;white-space:nowrap}
#codex-token-sidebar .cts-reroutes{border-top:1px solid var(--cts-rule);padding:12px 0 0}
#codex-token-sidebar .cts-reroutes-label{font-size:12px;font-weight:600;margin-bottom:6px}
#codex-token-sidebar .cts-reroute{font-size:12px;overflow-wrap:anywhere;padding:2px 0}
#codex-token-sidebar .cts-reroute+.cts-reroute{margin-top:4px}
#codex-token-sidebar .cts-route-card{display:flex;align-items:center;gap:8px}
#codex-token-sidebar .cts-reroute-action{width:100%;text-align:left;font:inherit;cursor:pointer}
#codex-token-sidebar .cts-reroute-action:focus-visible{outline:2px solid #4b8de8;outline-offset:2px}
#codex-token-sidebar .cts-route-icon{align-self:center;width:16px;height:16px;flex:none;color:var(--cts-muted)}
#codex-token-sidebar .cts-route-text{flex:1;min-width:0;white-space:normal;overflow-wrap:anywhere}
#codex-token-sidebar .cts-route-text-content{white-space:normal}
#codex-token-sidebar .cts-reroute-status{margin-left:auto;flex:none;color:var(--cts-muted);font-size:12px}
#codex-token-sidebar .cts-reroute-status:empty{display:none}
#codex-token-sidebar .cts-route-card{margin-top:8px;padding:7px 10px;border:1px solid rgba(128,128,128,.18);border-radius:9px;background:rgba(128,128,128,.08);color:inherit;font-size:13px;line-height:1.4;transition:background-color .15s ease,border-color .15s ease}
#codex-token-sidebar .cts-route-card:hover{border-color:rgba(128,128,128,.42);background:rgba(128,128,128,.2)}
#codex-token-sidebar .cts-total{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:center;padding-bottom:12px}
#codex-token-sidebar .cts-summary-main{border-right:1px solid var(--cts-rule);padding-right:12px}
#codex-token-sidebar .cts-summary-label{color:var(--cts-muted);font-size:12px;white-space:nowrap}
#codex-token-sidebar .cts-summary-main .cts-summary-label{color:inherit;font-weight:550}
#codex-token-sidebar .cts-summary-value{font-size:clamp(20px,9cqi,28px);line-height:1.4;font-weight:650;white-space:nowrap}
#codex-token-sidebar .cts-breakdown{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-top:1px solid var(--cts-rule);padding:8px 0 12px}
#codex-token-sidebar .cts-breakdown-item{display:flex;align-items:baseline;justify-content:space-between;gap:6px;min-width:0;font-size:12px}
#codex-token-sidebar .cts-breakdown-item:first-child{padding-right:12px}
#codex-token-sidebar .cts-breakdown-item+.cts-breakdown-item{border-left:1px solid var(--cts-rule);padding-left:12px}
#codex-token-sidebar .cts-breakdown-label{color:var(--cts-muted)}
#codex-token-sidebar .cts-breakdown-value{font-size:14px;font-weight:550;white-space:nowrap}
#codex-token-sidebar .cts-latest{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));border-top:1px solid var(--cts-rule);padding:8px 0 12px}
#codex-token-sidebar .cts-latest-metric{min-width:0}
#codex-token-sidebar .cts-latest-metric:first-child{padding-right:12px}
#codex-token-sidebar .cts-latest-metric+.cts-latest-metric{border-left:1px solid var(--cts-rule);padding-left:12px}
#codex-token-sidebar .cts-latest-label{color:var(--cts-muted);font-size:12px}
#codex-token-sidebar .cts-latest-value{display:flex;align-items:baseline;flex-wrap:wrap;gap:4px;margin-top:2px;font-size:12px}
#codex-token-sidebar .cts-latest-number{font-size:18px;font-weight:550;overflow-wrap:anywhere;min-width:0}
#codex-token-sidebar .cts-section{border-top:1px solid var(--cts-rule);padding-top:14px;margin-top:0;padding-bottom:16px}
#codex-token-sidebar .cts-reroutes+.cts-section{border-top:0;padding-top:16px}
#codex-token-sidebar .cts-stats-grid{display:grid;gap:8px;margin-top:12px}
#codex-token-sidebar .cts-stat{display:grid;grid-template-columns:minmax(0,1fr) 56px 52px;align-items:baseline;gap:10px}
#codex-token-sidebar .cts-stat-label{display:flex;align-items:center;gap:8px;min-width:0}
#codex-token-sidebar .cts-dot{width:8px;height:8px;border-radius:50%;background:var(--cts-color);flex:none}
#codex-token-sidebar .cts-amount,#codex-token-sidebar .cts-share{text-align:right;white-space:nowrap;font-size:12px}
#codex-token-sidebar .cts-amount{font-weight:550}
#codex-token-sidebar .cts-share{color:inherit}
#codex-token-sidebar .cts-bar{display:flex;height:6px;border-radius:4px;overflow:hidden;background:rgba(128,128,128,.25)}
#codex-token-sidebar .cts-feature-bar{height:10px}
#codex-token-sidebar .cts-bar-segment{height:100%;background:var(--cts-color);flex:none}
#codex-token-sidebar .cts-model-card{min-width:0;padding:12px 0}
#codex-token-sidebar .cts-model-card:first-child{padding-top:0}
#codex-token-sidebar .cts-model-card+.cts-model-card{border-top:1px solid var(--cts-rule)}
#codex-token-sidebar .cts-model-head{display:flex;align-items:baseline;justify-content:space-between;gap:8px;min-width:0}
#codex-token-sidebar .cts-model-name{min-width:0;font-size:12px;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#codex-token-sidebar .cts-model-share{display:flex;align-items:baseline;gap:6px;flex:none;font-size:12px;white-space:nowrap}
#codex-token-sidebar .cts-model-share-label{color:var(--cts-muted)}
#codex-token-sidebar .cts-model-share-value{font-weight:550}
#codex-token-sidebar .cts-model-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:baseline;margin-top:8px}
#codex-token-sidebar .cts-model-metric{display:flex;align-items:baseline;gap:5px;min-width:0;white-space:nowrap}
#codex-token-sidebar .cts-model-metric+.cts-model-metric{border-left:1px solid var(--cts-rule);padding-left:12px}
#codex-token-sidebar .cts-model-metric-label{color:var(--cts-muted);font-size:12px;white-space:nowrap}
#codex-token-sidebar .cts-model-metric-value{font-size:18px;line-height:1.35;font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#codex-token-sidebar .cts-model-card .cts-bar{margin:8px 0 10px}
#codex-token-sidebar .cts-model-details{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:28px;row-gap:4px}
#codex-token-sidebar .cts-model-detail{display:flex;align-items:baseline;justify-content:space-between;gap:6px;min-width:0;font-size:12px}
#codex-token-sidebar .cts-model-detail-label{color:var(--cts-muted);white-space:nowrap}
#codex-token-sidebar .cts-model-detail-value{color:inherit;white-space:nowrap}
#codex-token-sidebar .cts-muted{color:var(--cts-muted);font-size:12px}
@container (max-width:360px){
#codex-token-sidebar .cts-model-details{column-gap:18px}
}
@container (max-width:270px){
#codex-token-sidebar .cts-summary-main{padding-right:8px}
#codex-token-sidebar .cts-credits{padding-left:8px}
#codex-token-sidebar .cts-summary-value,#codex-token-sidebar .cts-credits-value{font-size:22px}
#codex-token-sidebar .cts-model-metric{flex-direction:column;gap:0}
#codex-token-sidebar .cts-model-metric+.cts-model-metric{padding-left:8px}
#codex-token-sidebar .cts-model-details{grid-template-columns:1fr}
#codex-token-sidebar .cts-model-metric-value{font-size:16px}
}
`;
    (document.head || document.documentElement).appendChild(style);
  }

  function text(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function formatRate(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
    if (value >= 1000000000) return value.toExponential(3);
    if (value > 0 && value < 0.05) return value.toPrecision(2);
    return value.toLocaleString("en-US", {minimumFractionDigits:1, maximumFractionDigits:1});
  }

  function formatTokens(value) {
    var n = Number(value || 0);
    if (!isFinite(n)) return "0";
    if (n >= 1000000000) return (n / 1000000000).toFixed(1).replace(/\.0$/, "") + "B";
    if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    if (n >= 1000) return Math.round(n / 1000) + "k";
    return String(Math.round(n));
  }

  function summaryMetric(label, value, main) {
    var metric = text("div", main ? "cts-summary-main" : "cts-summary-metric");
    metric.appendChild(text("div", "cts-summary-label", label));
    metric.appendChild(text("div", "cts-summary-value", formatTokens(value)));
    return metric;
  }

  function breakdownMetric(label, value) {
    var metric = text("div", "cts-breakdown-item");
    metric.appendChild(text("span", "cts-breakdown-label", label));
    metric.appendChild(text("span", "cts-breakdown-value", formatTokens(value)));
    return metric;
  }

  function appendLatestPerformance(body, sample) {
    if (!sample || sample.metric !== "engine_iapi_sampling_interval" || sample.selection !== "completed_segment"
        || typeof sample.model !== "string" || !sample.model.trim() || sample.model === "unknown"
        || typeof sample.sampledAt !== "string" || !Number.isFinite(Date.parse(sample.sampledAt))
        || typeof sample.outputTokensPerSecond !== "number" || !Number.isFinite(sample.outputTokensPerSecond) || sample.outputTokensPerSecond <= 0
        || typeof sample.firstTokenLatencyMs !== "number" || !Number.isFinite(sample.firstTokenLatencyMs) || sample.firstTokenLatencyMs < 0) return;
    var row = text("div", "cts-latest");
    var context = "模型：" + sample.model + "\n采样时间：" + new Date(sample.sampledAt).toLocaleString("zh-CN", {hour12:false});
    [["最新 TPS", sample.outputTokensPerSecond, "Token/s", "最近完整分段的采样速率，包含推理和工具调用输出，不含工具执行和等待。"],
     ["首 Token 延迟", sample.firstTokenLatencyMs/1000, "秒", "同一分段的引擎首 Token 耗时，不等于发送消息后等待正文的完整时间。"]].forEach(function (item) {
      var metric = text("div", "cts-latest-metric");
      metric.title = context + "\n" + item[3];
      metric.appendChild(text("div", "cts-latest-label", item[0]));
      var value = text("div", "cts-latest-value");
      value.appendChild(text("span", "cts-latest-number", item[2] === "Token/s" ? formatRate(item[1]) : formatDecimal(item[1])));
      value.appendChild(text("span", "cts-latest-unit", item[2]));
      metric.appendChild(value); row.appendChild(metric);
    });
    body.appendChild(row);
  }

  var COLORS = ["#4b8de8", "#f39a4a", "#a278e6", "#72d58d", "#f2ca3d", "#f0646b"];
  var FEATURE_COLORS = { tasks: COLORS[0], auto_review: COLORS[1], subagents: COLORS[2] };

  function barSegment(share, color) {
    var segment = text("span", "cts-bar-segment");
    var value = Number(share) || 0;
    segment.style.width = Math.max(0, Math.min(100, value)) + "%";
    segment.style.setProperty("--cts-color", color);
    return segment;
  }

  var FEATURE_LABELS = {
    tasks: "任务",
    subagents: "子代理",
    auto_review: "自动审查",
  };

  function formatPercent(value) {
    var n = Number(value || 0);
    if (!isFinite(n)) return "0%";
    return n.toFixed(1).replace(/\.0$/, "") + "%";
  }

  function formatDecimal(value) {
    var amount = value == null ? NaN : Number(value);
    if (!Number.isFinite(amount) || amount < 0) return "—";
    if (amount > 0 && amount < 0.1) return "<0.1";
    return amount.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  function statsGrid(rows) {
    var group = text("div", "");
    var bar = text("div", "cts-bar cts-feature-bar");
    bar.setAttribute("aria-hidden", "true");
    var grid = text("div", "cts-stats-grid");
    var ordered = rows.slice().sort(function (a, b) { return b.total_tokens - a.total_tokens; });
    ordered.forEach(function (row) {
      var color = FEATURE_COLORS[row.feature] || COLORS[0];
      bar.appendChild(barSegment(row.share, color));
      var item = text("div", "cts-stat");
      item.style.setProperty("--cts-color", color);
      var label = text("div", "cts-stat-label");
      label.appendChild(text("span", "cts-dot"));
      label.appendChild(text("span", "", FEATURE_LABELS[row.feature] || row.feature));
      item.appendChild(label);
      item.appendChild(text("span", "cts-amount", formatTokens(row.total_tokens)));
      item.appendChild(text("span", "cts-share", formatPercent(row.share)));
      grid.appendChild(item);
    });
    group.appendChild(bar);
    group.appendChild(grid);
    return group;
  }

  function modelDetail(label, value) {
    var row = text("div", "cts-model-detail");
    row.appendChild(text("span", "cts-model-detail-label", label));
    row.appendChild(text("span", "cts-model-detail-value", value));
    return row;
  }

  function modelMetric(label, value, className) {
    var metric = text("div", "cts-model-metric " + className);
    metric.appendChild(text("span", "cts-model-metric-value", value));
    metric.appendChild(text("span", "cts-model-metric-label", label));
    return metric;
  }

  function modelCard(row, index, credits, performanceData) {
    var card = text("div", "cts-model-card");
    var heading = text("div", "cts-model-head");
    var name = text("div", "cts-model-name", row.model);
    name.title = row.model;
    heading.appendChild(name);
    var share = text("div", "cts-model-share");
    share.appendChild(text("span", "cts-model-share-label", "占比"));
    share.appendChild(text("span", "cts-model-share-value", formatPercent(row.share)));
    heading.appendChild(share);
    card.appendChild(heading);
    var metrics = text("div", "cts-model-metrics");
    var modelCredits = credits && credits[row.model];
    var creditMetric = modelMetric("Credits",
      formatDecimal(modelCredits && modelCredits.estimatedCredits), "cts-model-credit");
    if (modelCredits && modelCredits.unpricedResponses > 0) {
      creditMetric.title = modelCredits.unpricedResponses + " 条响应暂无法计价";
    }
    metrics.appendChild(creditMetric);
    metrics.appendChild(modelMetric("Token", formatTokens(row.total_tokens), "cts-model-usage"));
    card.appendChild(metrics);
    var bar = text("div", "cts-bar");
    bar.setAttribute("aria-hidden", "true");
    bar.appendChild(barSegment(row.share, COLORS[index % COLORS.length]));
    card.appendChild(bar);

    var details = text("div", "cts-model-details");
    details.appendChild(modelDetail("输入", formatTokens(row.input_tokens)));
    details.appendChild(
      modelDetail("缓存输入", formatTokens(row.cached_input_tokens))
    );
    details.appendChild(
      modelDetail("推理", formatTokens(row.reasoning_output_tokens))
    );
    details.appendChild(modelDetail("输出", formatTokens(row.output_tokens)));
    details.appendChild(
      modelDetail("缓存命中率", formatPercent(row.cache_hit_rate))
    );
    details.appendChild(
      modelDetail(
        "已记录请求",
        row.request_count == null ? "—" : formatTokens(row.request_count)
      )
    );
    card.appendChild(details);
    if (performanceData) appendTurnPerformance(card, performanceData);
    return card;
  }

  function modelStatsGrid(rows, credits, performanceData) {
    var grid = text("div", "cts-model-grid");
    (rows || []).forEach(function (row, index) {
      var group = performanceGroups(performanceData).find(function (item) { return item.model === row.model; });
      grid.appendChild(modelCard(row, index, credits, group));
    });
    return grid;
  }

  function routePinIcon() {
    var namespace = "http://www.w3.org/2000/svg";
    var icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("class", "cts-route-icon");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.8");
    icon.setAttribute("stroke-linecap", "round");
    icon.setAttribute("stroke-linejoin", "round");
    icon.setAttribute("aria-hidden", "true");
    var pin = document.createElementNS(namespace, "path");
    pin.setAttribute("d", "M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z");
    icon.appendChild(pin);
    var center = document.createElementNS(namespace, "circle");
    center.setAttribute("cx", "12");
    center.setAttribute("cy", "10");
    center.setAttribute("r", "2.5");
    icon.appendChild(center);
    return icon;
  }

  function routeText(value) {
    var viewport = text("span", "cts-route-text");
    viewport.appendChild(text("span", "cts-route-text-content", value));
    return viewport;
  }

  function routeCard(item, conversationId) {
    var route = item.conflicts.length ? "改路由记录存在差异" : item.fromModel + " → " + item.toModel;
    var detail = "定位到发生改路由的轮次";
    if (item.conflicts.length) detail += "\n改路由记录存在差异：\n" + [rerouteDescription(item)].concat(item.conflicts).sort().join("\n");
    else if (item.reason) detail += "\n原因：" + rerouteReason(item.reason);
    if (item.startedAt != null && !item.timeConflict) detail += "\n轮次开始：" + new Date(item.startedAt * 1000).toLocaleString("zh-CN", {hour12:false});
    if (item.timeConflict || item.orderConflict) detail += "\n事件顺序暂无法确认";
    var card = text("button", "cts-reroute cts-route-card cts-reroute-action");
    card.type = "button";
    card.title = detail;
    card.setAttribute("aria-label", "定位到发生改路由的轮次：" + route);
    card.setAttribute("aria-description", detail);
    card.appendChild(routePinIcon());
    card.appendChild(routeText(route));
    var status = text("span", "cts-reroute-status", "");
    status.setAttribute("aria-live", "polite");
    card.appendChild(status);
    card.addEventListener("click", function () {
      if (readConversationId() !== conversationId) return;
      var anchor = hostAdapter.turnAnchor(item.turnId);
      if (!anchor) {
        status.textContent = "未加载";
        card.title = detail + "\n目标轮次尚未加载";
        return;
      }
      status.textContent = "";
      card.title = detail;
      var userMessage = anchor.querySelector("[data-local-conversation-user-anchor]");
      var target = userMessage || anchor;
      var reducedMotion = window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    });
    return card;
  }

  function appendReroutes(body, conversationId) {
    var routes = recentReroutes(conversationId);
    if (!routes.length) return;
    var section = text("div", "cts-reroutes");
    section.appendChild(text("div", "cts-reroutes-label",
      reroutesOrdered(conversationId) ? "模型改路由（最近 3 次）" : "已记录改路由"));
    routes.forEach(function (item) {
      section.appendChild(routeCard(item, conversationId));
    });
    body.appendChild(section);
  }

  function performanceGroups(data) {
    return data && data.metric === "engine_iapi_sampling_interval" && data.selection === "complete_sampling" && Array.isArray(data.byModel) ? data.byModel : [];
  }

  function appendPendingModelPerformance(body, data) {
    performanceGroups(data).forEach(function (group) {
      if (!Array.isArray(group.turns) || !group.turns.length) return;
      var card = text("div", "cts-model-card");
      card.appendChild(text("div", "cts-model-name", group.model));
      appendTurnPerformance(card, group);
      body.appendChild(card);
    });
  }

  function appendTurnPerformance(card, performanceData) {
    if (!Array.isArray(performanceData.turns) || !performanceData.turns.length) return;
    var rows = performanceData.turns.slice(-10);
    var section = text("section", "cts-performance");
    section.setAttribute("aria-label", performanceData.model + " 最近完整轮次平均 TPS");
    section.appendChild(text("div", "cts-performance-title", "最近 10 个完整轮次"));
    card.appendChild(section);
    var hovered = null, focused = null, curveHovered = false, curveFocused = false, resizing = false;
    var refresh = function () {};
    var legend = text("div", "cts-performance-legend");
    var entry = text("button", "cts-average-legend");
    entry.type = "button";
    entry.setAttribute("aria-label", "平均 TPS，查看全部轮次数值");
    entry.setAttribute("aria-pressed", "false");
    entry.title = "每轮总采样间隔数 ÷ 总采样时间。";
    entry.appendChild(text("i", "")); entry.appendChild(text("span", "", "平均 TPS")); legend.appendChild(entry);
    section.appendChild(legend);
    function clearSelection() { curveHovered=curveFocused=false; hovered=focused=null; refresh(); }
    entry.addEventListener("mouseenter", function () { curveHovered=true; hovered=null; refresh(); });
    entry.addEventListener("mouseleave", function () { curveHovered=false; refresh(); });
    entry.addEventListener("focus", function () { curveFocused=true; focused=hovered=null; refresh(); });
    entry.addEventListener("blur", function () { curveFocused=false; refresh(); });
    entry.addEventListener("click", function () { curveFocused=true; entry.focus(); refresh(); });
    entry.addEventListener("keydown", function (event) {
      if (event.key === "Escape") clearSelection();
      else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); curveFocused=true; refresh(); }
    });
    var plot = text("div", "cts-performance-plot"); section.appendChild(plot);
    var summary = text("div", "cts-performance-summary");
    summary.appendChild(text("span", "cts-performance-unit", "Token/s"));
    var average = modelDetail("平均 TPS", formatRate(performanceData.averageOutputTokensPerSecond));
    average.className += " cts-chart-average";
    average.querySelector(".cts-model-detail-value").className += " cts-performance-value";
    average.title = "该模型主控最近 10 个合格完成轮次的采样间隔总数 ÷ 采样总耗时（Token/s）。"
      + "\n不含工具执行和等待；包含推理和工具调用输出。"
      + "\n基于 " + (performanceData.averageSampleCount || 0) + " 个轮次。";
    summary.appendChild(average);
    var detailBox = text("div", "cts-turn-detail"); detailBox.setAttribute("role", "group"); detailBox.setAttribute("aria-label", "轮次详情");
    var detailSlot = text("div", "cts-detail-slot"); detailSlot.appendChild(detailBox); section.appendChild(detailSlot);
    function valid(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0; }
    function svgNode(tag, attrs, value) {
      var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, String(attrs[key])); });
      if (value != null) node.textContent = value; return node;
    }
    var lastWidth = 0;
    function draw(width) {
      width = Math.round(width); if (width <= 0 || width === lastWidth || disposed) return;
      var activeControl = document.activeElement;
      var restoreCurveHit = activeControl && plot.contains(activeControl)
        && activeControl.getAttribute("data-hit-index") !== null
        ? Number(activeControl.getAttribute("data-hit-index")) : null;
      lastWidth=width; resizing=true; plot.replaceChildren(summary);
      var baseHeight=164, left=6, right=Math.max(left+1,width-6), top=30, bottom=132;
      var svg=svgNode("svg", {width:"100%",height:baseHeight,viewBox:"0 0 "+width+" "+baseHeight,"aria-label":"最近完整轮次平均 TPS 曲线"});
      plot.appendChild(svg);
      var rates=rows.map(function (row) { return row.outputTokensPerSecond; });
      var rateMax=Math.max.apply(null,[1].concat(rates.filter(valid)));
      function y(value) { return bottom-value/rateMax*(bottom-top); }
      var cell=(right-left)/rows.length;
      function center(i) { return left+cell*(i+0.5); }
      var group=svgNode("g", {class:"cts-series-average"});svg.appendChild(group);
      var path="",previous=null,dots=[],curveHits=[];
      rates.forEach(function (rate,i) {
        var x=center(i);
        if (width>=300 || i===0 || i===rows.length-1 || i===Math.floor((rows.length-1)/2)) svg.appendChild(svgNode("text",{x:x,y:154,"text-anchor":"middle",fill:"currentColor"},String(i+1)));
        if (!valid(rate)) { previous=null; return; }
        var height=y(rate);
        if (previous) { var bend=(x-previous.x)*0.6;path+="C"+(previous.x+bend)+","+previous.y+" "+(x-bend)+","+height+" "+x+","+height+" "; }
        else path+="M"+x+","+height+" ";
        previous={x:x,y:height};
        var dot=svgNode("circle",{class:"cts-average-dot",cx:x,cy:height,r:2.5,fill:"var(--cts-chart-accent)"});group.appendChild(dot);dots.push(dot);
        curveHits.push(svgNode("circle",{class:"cts-series-hit","data-series":"average",cx:x,cy:height,r:7,fill:"transparent",tabindex:0,role:"button","aria-label":"查看全部平均 TPS 数值"}));
      });
      var line=null;
      if (path) {
        line=svgNode("path",{class:"cts-average-line",d:path,fill:"none",stroke:"var(--cts-chart-accent)","stroke-width":1.8,"stroke-linecap":"round","stroke-linejoin":"round"});group.appendChild(line);
        curveHits.unshift(svgNode("path",{class:"cts-series-hit","data-series":"average",d:path,fill:"none",stroke:"transparent","stroke-width":12,"pointer-events":"stroke",tabindex:0,role:"button","aria-label":"查看全部平均 TPS 数值"}));
      }
      var readout=svgNode("g",{class:"cts-turn-readout","pointer-events":"none","aria-hidden":"true"});svg.appendChild(readout);
      function turnTime(i) {
        var date=new Date(rows[i].completedAt);
        return "轮次 "+(i+1)+" · "+(isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}));
      }
      function select() {
        var active=curveHovered||curveFocused,index=hovered!==null?hovered:focused;
        entry.setAttribute("data-active",active?"true":"false");entry.setAttribute("aria-pressed",active?"true":"false");
        if (line) line.setAttribute("stroke-width",active?2.5:1.8);
        dots.forEach(function(dot){dot.setAttribute("r",active?3.5:2.5);});
        var selected=index===null?rows.length-1:index;
        detailBox.replaceChildren(text("div","cts-detail-time",turnTime(selected)),text("div","cts-detail-values","平均 TPS "+formatRate(rates[selected])));
        readout.replaceChildren();var height=baseHeight;
        if (active) {
          // A dedicated label band preserves the curve's geometry while packing
          // each turn independently into non-overlapping horizontal lanes.
          var lanes=[];
          rates.forEach(function(value,i){
            if (!valid(value)) return;
            var label=formatRate(value),half=Math.min((right-left)/2,label.length*4+3);
            var x=Math.max(left+half,Math.min(right-half,center(i))),lane=0;
            for (;lane<lanes.length;lane++) if (!lanes[lane].some(function(other){return Math.abs(x-other.x)<half+other.half+6;})) break;
            if (!lanes[lane]) lanes[lane]=[];lanes[lane].push({x:x,half:half});
            var labelY=baseHeight+18+lane*20;
            readout.appendChild(svgNode("line",{x1:center(i),x2:x,y1:y(value),y2:labelY-13,stroke:"currentColor",opacity:0.15}));
            readout.appendChild(svgNode("text",{class:"cts-series-number","data-index":i,x:x,y:labelY,"text-anchor":"middle",fill:"currentColor"},label));
          });
          height=baseHeight+lanes.length*20+8;
        } else if (index!==null && valid(rates[index])) {
          var label=formatRate(rates[index]),half=label.length*4+3,x=Math.max(left+half,Math.min(right-half,center(index)));
          readout.appendChild(svgNode("text",{class:"cts-turn-number","data-index":index,x:x,y:baseHeight+18,"text-anchor":"middle",fill:"currentColor"},label));height=baseHeight+28;
        }
        svg.setAttribute("height",height);svg.setAttribute("viewBox","0 0 "+width+" "+height);
      }
      refresh=select;
      var hits=[];
      rows.forEach(function(row,i){
        var hit=svgNode("rect",{class:"cts-turn-hit",x:left+cell*i,y:top,width:cell,height:bottom-top,fill:"transparent",tabindex:0,"aria-label":turnTime(i)+"\n平均 TPS："+formatRate(rates[i])+" Token/s"});
        hit.addEventListener("mouseenter",function(){hovered=i;refresh();});hit.addEventListener("mouseleave",function(){hovered=null;refresh();});
        hit.addEventListener("focus",function(){if(!resizing){focused=i;curveFocused=false;refresh();}});hit.addEventListener("blur",function(){if(!resizing){focused=null;refresh();}});
        hit.addEventListener("keydown",function(event){if(event.key==="Escape")clearSelection();});svg.appendChild(hit);hits.push(hit);
      });
      curveHits.forEach(function(hit,hitIndex){
        hit.setAttribute("data-hit-index",hitIndex);
        hit.addEventListener("mouseenter",function(){curveHovered=true;hovered=null;refresh();});
        hit.addEventListener("mouseleave",function(event){if(event&&event.relatedTarget&&event.relatedTarget.getAttribute&&event.relatedTarget.getAttribute("data-series")==="average")return;curveHovered=false;refresh();});
        hit.addEventListener("focus",function(){if(!resizing){curveFocused=true;focused=hovered=null;refresh();}});
        hit.addEventListener("blur",function(){if(!resizing){curveFocused=false;refresh();}});
        hit.addEventListener("click",function(){curveFocused=true;hit.focus();refresh();});
        hit.addEventListener("keydown",function(event){if(event.key==="Escape")clearSelection();else if(event.key==="Enter"||event.key===" "){event.preventDefault();curveFocused=true;refresh();}});
        svg.appendChild(hit);
      });
      if (restoreCurveHit !== null && curveHits[restoreCurveHit]) curveHits[restoreCurveHit].focus({preventScroll:true});
      else if (focused!==null) hits[focused].focus({preventScroll:true});
      resizing=false;select();
    }
    draw(plot.clientWidth||300);
    if(typeof ResizeObserver!=="undefined"){
      plot.__ctsDraw=draw;
      if(!performanceObserver)performanceObserver=new ResizeObserver(function(entries){entries.forEach(function(item){item.target.__ctsDraw(item.contentRect.width);});});
      performanceObserver.observe(plot);
    }
  }
  function render(node, data) {
    var body = node.querySelector(".cts-body");
    if (!body) return;
    var activeId = readConversationId();
    var signature = String(data && data.revision) + "|" + String(activeId || "")
      + "|" + selectionEpoch + "|" + String(data && data.selectionEpoch)
      + "|" + rerouteRevision;
    if (node.__ctsRenderSignature === signature) {
      return;
    }
    node.__ctsRenderSignature = signature;
    if (performanceObserver) performanceObserver.disconnect();
    performanceObserver = null;
    body.replaceChildren();
    if (!data || data.status !== "ok") {
      body.appendChild(text("div", "cts-muted", "等待当前会话用量…"));
      if (PERFORMANCE_ENABLED && data && data.conversationId === activeId && data.selectionEpoch === selectionEpoch) {
        appendPendingModelPerformance(body, data.sessionTurnPerformance);
      }
      appendReroutes(body, activeId);
      return;
    }

    if (
      data.conversationId &&
      (activeId !== data.conversationId || data.selectionEpoch !== selectionEpoch)
    ) {
      body.appendChild(text("div", "cts-muted", "正在切换会话…"));
      appendReroutes(body, activeId);
      return;
    }

    var totalLine = text("div", "cts-total");
    var sessionMetric = summaryMetric("已记录用量", data.session.total_tokens, true);
    var scope = data.sessionUsageCompleteness;
    if (scope && (scope.status === "unknown" || scope.status === "partial")) {
      var explanations = {
        history_scope_unconfirmed: "无法确认是否已包含较早的历史用量。",
        checkpoint_missing: "缺少用于核实统计范围的累计记录。",
        legacy_only_turns: "部分轮次只有历史统计，尚无对应的响应明细。",
        source_conflict: "不同来源记录的响应数值存在差异。",
        response_identity_missing: "部分响应缺少标识，无法确认请求数。",
        modern_cumulative_inconsistent: "已记录响应的合计与其累计用量不一致。",
        legacy_cumulative_inconsistent: "历史累计记录的数值变化不一致。"
      };
      sessionMetric.querySelector(".cts-summary-label").title = (scope.reasons || []).map(function (reason) {
        return explanations[reason] || "部分历史用量的范围尚无法确认。";
      }).join("\n");
    }
    totalLine.appendChild(sessionMetric);
    var credits = data.sessionCredits;
    var creditRow = text("div", "cts-credits");
    var creditLabel = text("span", "cts-summary-label", "参考 Credits");
    creditRow.appendChild(creditLabel);
    creditRow.appendChild(text("span", "cts-credits-value",
      formatDecimal(credits && credits.estimatedCredits)));
    if (credits) {
      var details = [];
      var verified = credits.rateVerifiedAt && new Date(credits.rateVerifiedAt);
      var checked = verified && !isNaN(verified.getTime())
        ? verified.toLocaleString("zh-CN", {hour12:false}) : credits.rateVerifiedOn;
      if (checked) details.push("最近核实：" + checked);
      if (credits.rateStatus === "stale") {
        var failures = {fetch_timeout:"官方来源请求超时", fetch_failed:"无法获取官方费率来源",
          parse_failed:"官方来源未通过费率校验", cache_write_failed:"无法保存费率缓存"};
        details.push("最近刷新失败：" + (failures[credits.rateRefreshError] || "原因未记录"));
      }
      if (credits.unpricedResponses > 0) details.push(credits.unpricedResponses + " 条响应暂无法计价");
      if (credits.limitations && credits.limitations.some(function (reason) { return reason !== "read_incomplete"; })) {
        details.push("部分已记录用量暂无法计价");
      }
      if (details.length) creditLabel.title = details.join("\n");
    }
    totalLine.appendChild(creditRow);
    body.appendChild(totalLine);
    if (PERFORMANCE_ENABLED) appendLatestPerformance(body, data.sessionLatestPerformance);
    var breakdown = text("div", "cts-breakdown");
    breakdown.appendChild(breakdownMetric("输入", data.session.input_tokens));
    breakdown.appendChild(breakdownMetric("输出", data.session.output_tokens));
    body.appendChild(breakdown);

    appendReroutes(body, activeId);

    var featureRows = data.sessionByFeature || [];
    var modelRows = data.sessionByModel || [];
    if (featureRows.length) {
      var features = text("div", "cts-section");
      features.appendChild(statsGrid(featureRows));
      body.appendChild(features);
    }
    if (modelRows.length) {
      var models = text("div", "cts-section");
      models.appendChild(modelStatsGrid(modelRows, credits ? (credits.byModel || {}) : null, PERFORMANCE_ENABLED ? data.sessionTurnPerformance : null));
      body.appendChild(models);
    }

  }

  function ensureNode() {
    if (disposed) return null;
    if (!hostAdapter.isMainPage() || !hostAdapter.hasShell()) {
      observer.disconnect();
      observedScroller = observedParent = null;
      if (panel) panel.remove();
      return null;
    }
    ensureStyle();
    var scroller = hostAdapter.scroller();
    if (!scroller) {
      observer.disconnect();
      observedScroller = null;
      observedParent = null;
      if (panel) panel.remove();
      return null;
    }
    var node = panel || document.getElementById(ROOT_ID);
    if (!node) {
      node = document.createElement("section");
      node.id = ROOT_ID;
      var header = text("header", "cts-header");
      var toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "cts-toggle";
      toggle.setAttribute("aria-expanded", "true");
      toggle.appendChild(text("span", "", "Token 用量"));
      toggle.insertAdjacentHTML(
        "beforeend",
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>'
      );
      toggle.addEventListener("click", function () {
        node.classList.toggle("cts-collapsed");
        toggle.setAttribute("aria-expanded", String(!node.classList.contains("cts-collapsed")));
      });
      header.appendChild(toggle);
      node.appendChild(header);
      node.appendChild(text("div", "cts-body"));
    }
    panel = node;
    // React can append Lab sections after the injector has mounted. Keep the
    // Token section at the bottom whenever the host container changes.
    if (node.parentElement !== scroller || node !== scroller.lastElementChild) {
      scroller.appendChild(node);
    }
    if (observedScroller !== scroller || observedParent !== scroller.parentElement) {
      observer.disconnect();
      // Direct child changes cover host sections and removal of our panel.
      // Ancestor replacement is recovered by the regular Python state probe.
      observer.observe(scroller, { childList: true });
      if (scroller.parentElement) observer.observe(scroller.parentElement, { childList: true });
      observedScroller = scroller;
      observedParent = scroller.parentElement;
    }
    render(node, window.__codexTokenSidebarData);
    renderHealth();
    return node;
  }

  // All host-private selectors and React navigation stay behind this adapter.
  var hostAdapter = {
    isMainPage: isMainPage, hasShell: hasMainShell, scroller: findScroller,
    conversationId: resolveConversationId, scanReroutes: scanNativeReroutes,
    turnAnchor: findNativeTurnAnchor,
  };

  window.__codexTokenSidebarUpdate = function (data) {
    if (disposed || !data || data.schemaVersion !== SCHEMA_VERSION
        || typeof data.revision !== "string" || !Number.isSafeInteger(data.probeId)) {
      return { status: "needs_install" };
    }
    if (!hostAdapter.isMainPage() || !hostAdapter.hasShell() || data.pageUrl !== window.location.href) {
      ensureNode();
      return { status: "stale" };
    }
    var activeId = readConversationId();
    hostAdapter.scanReroutes(activeId);
    if (data.pageEpoch !== pageEpoch || data.selectionEpoch !== selectionEpoch
        || data.conversationId !== activeId || data.probeId !== probeId
        || data.probeId < acceptedProbeId
        || (data.probeId === acceptedProbeId && window.__codexTokenSidebarData
            && data.revision !== window.__codexTokenSidebarData.revision)) {
      ensureNode();
      return { status: "stale" };
    }
    window.__codexTokenSidebarData = data;
    acceptedProbeId = data.probeId;
    var mounted = !!ensureNode();
    receiveHeartbeat(data.health);
    return {
      mounted: mounted,
      pageUrl: window.location.href,
      status: "accepted", schemaVersion: SCHEMA_VERSION, pageEpoch: pageEpoch,
      selectionEpoch: selectionEpoch, probeId: data.probeId,
      conversationId: data.conversationId, revision: data.revision,
    };
  };

  window.__codexTokenSidebarState = function (health) {
    if (disposed) return null;
    receiveHeartbeat(health);
    var activeId = readConversationId();
    hostAdapter.scanReroutes(activeId);
    var mounted = !!ensureNode();
    var data = window.__codexTokenSidebarData;
    return {
      schemaVersion: SCHEMA_VERSION,
      pageUrl: window.location.href,
      mounted: mounted,
      targetRecognized: hostAdapter.hasShell(),
      scriptHash: scriptHash,
      pageEpoch: pageEpoch,
      conversationId: activeId,
      selectionEpoch: selectionEpoch,
      probeId: ++probeId,
      dataConversationId: data ? data.conversationId : null,
      dataSelectionEpoch: data ? data.selectionEpoch : null,
      revision: data ? data.revision : null,
    };
  };

  var scheduled = false;
  var observer = new MutationObserver(function (records) {
    if (disposed) return;
    var relevant = records.some(function (record) {
      if (panel && panel.contains(record.target)) return false;
      var changed = Array.from(record.addedNodes).concat(Array.from(record.removedNodes));
      // Ignore our own successful append/move; a host removal must remount it.
      return changed.some(function (node) {
        return node !== panel || !panel.isConnected || panel.parentElement !== cachedScroller;
      });
    });
    if (!relevant) return;
    if (scheduled) return;
    scheduled = true;
    frameId = requestAnimationFrame(function () {
      scheduled = false;
      frameId = null;
      ensureNode();
    });
  });
  var routeObserver = new MutationObserver(function (records) {
    if (disposed) return;
    var nativeChanges = records.filter(function (record) {
      if (panel && panel.contains(record.target)) return false;
      var changed = Array.from(record.addedNodes || []).concat(Array.from(record.removedNodes || []));
      return !changed.length || changed.some(function (node) { return node !== panel; });
    });
    if (nativeChanges.length) reroutesDirty = true;
    function affectsHost(node) {
      return !!node && ((cachedScroller && node.contains && node.contains(cachedScroller))
        || (node.getAttribute && node.getAttribute("data-summary-panel-variant") !== null)
        || (node.closest && node.closest("[data-summary-panel-variant]"))
        || (node.querySelector && node.querySelector("[data-summary-panel-variant]")));
    }
    if (nativeChanges.some(function (record) {
      if (record.type === "attributes") return affectsHost(record.target);
      if (affectsHost(record.target)) return true;
      return Array.from(record.addedNodes || []).concat(Array.from(record.removedNodes || [])).some(affectsHost);
    })) hostSelectionDirty = true;
  });
  window.__codexTokenSidebarDispose = function () {
    disposed = true;
    clearInterval(healthTimer);
    if (performanceObserver) performanceObserver.disconnect();
    observer.disconnect();
    routeObserver.disconnect();
    if (frameId !== null) cancelAnimationFrame(frameId);
    if (panel) panel.remove();
    var style = document.getElementById(STYLE_ID);
    if (style) style.remove();
    window.__codexTokenSidebarInstalled = false;
    window.__codexTokenSidebarData = null;
  };
  window.__codexTokenSidebarObserver = observer;
  var healthTimer = setInterval(renderHealth, 1000);
  observeRouteRoot();
  window.__codexTokenSidebarInstalled = true;
})(__CODEX_TOKEN_SIDEBAR_HASH__);
