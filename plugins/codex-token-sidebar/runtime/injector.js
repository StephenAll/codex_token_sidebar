(function (scriptHash) {
  "use strict";

  var SCHEMA_VERSION = 3;
  var ROOT_ID = "codex-token-sidebar";
  var STYLE_ID = "codex-token-sidebar-style";
  var ROUTE_DEBUG_MODE = false;
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
    if (isVisibleHost(cachedScroller)) return cachedScroller;
    cachedScroller = null;
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
    if (root) routeObserver.observe(root, { childList: true, subtree: true });
    return root;
  }

  function recordReroute(conversationId, turnId, item) {
    if (!item || item.type !== "modelRerouted"
        || typeof item.fromModel !== "string" || typeof item.toModel !== "string") return false;
    var fromModel = item.fromModel.trim();
    var toModel = item.toModel.trim();
    if (!fromModel || !toModel || fromModel.length > 128 || toModel.length > 128
        || fromModel.toLowerCase() === toModel.toLowerCase()) return false;
    var bucket = reroutesByConversation.get(conversationId);
    if (!bucket) {
      if (reroutesByConversation.size >= 16) {
        reroutesByConversation.delete(reroutesByConversation.keys().next().value);
      }
      bucket = new Map();
      reroutesByConversation.set(conversationId, bucket);
    }
    var key = turnId + "\u0000" + fromModel + "\u0000" + toModel;
    if (bucket.has(key)) return false;
    var previousKeys = Array.from(bucket.keys()).join("\u0001");
    bucket.set(key, { turnId: turnId, fromModel: fromModel, toModel: toModel,
      sequence: ++rerouteSequence });
    var ordered = Array.from(bucket.entries()).sort(function (a, b) {
      var first = a[1], second = b[1];
      // Native Codex turn IDs are UUIDv7; their lexical order follows creation time.
      if (CONVERSATION_ID_RE.test(first.turnId) && CONVERSATION_ID_RE.test(second.turnId)) {
        return first.turnId < second.turnId ? -1 : first.turnId > second.turnId ? 1 : 0;
      }
      return first.sequence - second.sequence;
    });
    for (var i = 0; i < ordered.length - 3; i++) bucket.delete(ordered[i][0]);
    if (Array.from(bucket.keys()).join("\u0001") === previousKeys) return false;
    rerouteRevision++;
    return true;
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
              recordReroute(conversationId, turnId, props.turn.items[j]);
            }
          }
        }
      }
    }
  }

  function recentReroutes(conversationId) {
    var bucket = reroutesByConversation.get(conversationId);
    return bucket ? Array.from(bucket.values()).sort(function (a, b) {
      if (CONVERSATION_ID_RE.test(a.turnId) && CONVERSATION_ID_RE.test(b.turnId)) {
        return a.turnId < b.turnId ? 1 : a.turnId > b.turnId ? -1 : 0;
      }
      return b.sequence - a.sequence;
    }).slice(0, 3) : [];
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
#codex-token-sidebar .cts-performance{position:relative;border-top:1px solid var(--cts-rule);padding:12px 0;font-size:12px}
#codex-token-sidebar .cts-performance-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
#codex-token-sidebar .cts-performance-title{font-size:14px;font-weight:600}
#codex-token-sidebar .cts-performance-average{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:flex-end;gap:4px;max-width:60%}
#codex-token-sidebar .cts-performance-value{font-size:18px;font-weight:650;max-width:100%;overflow-wrap:anywhere}
#codex-token-sidebar .cts-performance-legend{display:flex;flex-wrap:wrap;gap:12px;margin:10px 0 2px;font-size:12px}
#codex-token-sidebar .cts-performance-legend>span{display:flex;gap:5px;align-items:center}
#codex-token-sidebar .cts-rate-legend i{width:14px;height:2px;background:#568fe8}
#codex-token-sidebar .cts-latency-legend i{width:10px;height:10px;background:#a49aba;opacity:.6}
#codex-token-sidebar .cts-performance-plot svg{display:block;overflow:visible;font-size:12px}
#codex-token-sidebar .cts-performance-plot text{font-size:12px}
#codex-token-sidebar .cts-turn-hit:focus-visible{outline:none;stroke:#568fe8;stroke-width:1}
#codex-token-sidebar .cts-turn-tooltip{position:absolute;z-index:2;left:0;right:0;bottom:28px;padding:8px 10px;border:1px solid #777;border-radius:6px;background:#252525;color:#fff;white-space:pre-line;overflow-wrap:anywhere;font-size:12px;pointer-events:none}
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
#codex-token-sidebar .cts-route-text{flex:1;min-width:0;overflow:hidden;white-space:nowrap;container-type:inline-size}
#codex-token-sidebar .cts-route-text-content{display:inline-block;white-space:nowrap;animation:cts-route-scroll 8s linear infinite alternate}
@keyframes cts-route-scroll{0%,12%{transform:translateX(0)}88%,100%{transform:translateX(min(0px,calc(100cqi - 100%)))}}
@media (prefers-reduced-motion:reduce){#codex-token-sidebar .cts-route-text{overflow-x:auto}#codex-token-sidebar .cts-route-text-content{animation:none}}
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

  function modelCard(row, index, credits) {
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
    return card;
  }

  function modelStatsGrid(rows, credits) {
    var grid = text("div", "cts-model-grid");
    (rows || []).forEach(function (row, index) {
      grid.appendChild(modelCard(row, index, credits));
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
    var actionable = !!item.turnId;
    var route = item.fromModel + " → " + item.toModel;
    var card = text(actionable ? "button" : "div",
      "cts-reroute cts-route-card " + (actionable ? "cts-reroute-action" : "cts-route-preview"));
    if (actionable) {
      card.type = "button";
      card.title = "定位到发生改路由的轮次";
      card.setAttribute("aria-label", "定位到发生改路由的轮次：" + route);
    }
    card.appendChild(routePinIcon());
    card.appendChild(routeText(route));
    if (actionable) {
      var status = text("span", "cts-reroute-status", "");
      status.setAttribute("aria-live", "polite");
      card.appendChild(status);
      card.addEventListener("click", function () {
        if (readConversationId() !== conversationId) return;
        var anchor = hostAdapter.turnAnchor(item.turnId);
        if (!anchor) {
          status.textContent = "未加载";
          card.title = "目标轮次尚未加载";
          return;
        }
        status.textContent = "";
        card.title = "定位到发生改路由的轮次";
        var userMessage = anchor.querySelector("[data-local-conversation-user-anchor]");
        var target = userMessage || anchor;
        var reducedMotion = window.matchMedia
          && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
      });
    }
    return card;
  }

  function appendReroutes(body, conversationId, modelRows) {
    var routes = ROUTE_DEBUG_MODE ? [] : recentReroutes(conversationId);
    if (ROUTE_DEBUG_MODE && Array.isArray(modelRows)) {
      for (var i = 0; i < modelRows.length && routes.length < 3; i++) {
        var model = modelRows[i] && modelRows[i].model;
        if (typeof model === "string" && model
            && !routes.some(function (item) { return item.fromModel === model; })) {
          routes.push({ fromModel: model, toModel: "上游模型未知" });
        }
      }
    }
    if (!routes.length) return;
    var section = text("div", "cts-reroutes");
    section.appendChild(text("div", "cts-reroutes-label",
      ROUTE_DEBUG_MODE ? "模型路由" : "模型改路由（最近 3 次）"));
    routes.forEach(function (item) {
      section.appendChild(routeCard(item, conversationId));
    });
    body.appendChild(section);
  }

  function appendTurnPerformance(body, performanceData) {
    if (!performanceData || !Array.isArray(performanceData.turns)) return;
    var rows = performanceData.turns.slice(-10);
    var section = text("section", "cts-performance");
    var heading = text("div", "cts-performance-heading");
    heading.appendChild(text("span", "cts-performance-title", "最近 10 轮"));
    var metric = text("div", "cts-performance-average");
    var label = text("span", "cts-muted", "平均速率");
    label.title = "总输出 Token ÷ 总耗时，包含等待和工具执行时间。\n基于 "
      + (performanceData.averageSampleCount || 0) + "/" + rows.length + " 轮。";
    metric.appendChild(label);
    metric.appendChild(text("span", "cts-performance-value", formatDecimal(performanceData.averageOutputTokensPerSecond)));
    metric.appendChild(text("span", "cts-muted", "Token/s"));
    heading.appendChild(metric);
    section.appendChild(heading);
    body.appendChild(section);
    if (!rows.length) {
      section.appendChild(text("div", "cts-muted", performanceData.windowUnavailableReason
        ? "轮次时间暂无法确认" : "暂无已完成轮次"));
      return;
    }
    var legend = text("div", "cts-performance-legend");
    ["输出速率", "首 Token 延迟"].forEach(function (name, index) {
      var entry = text("span", index ? "cts-latency-legend" : "cts-rate-legend");
      entry.appendChild(text("i", "")); entry.appendChild(text("span", "", name)); legend.appendChild(entry);
    });
    section.appendChild(legend);
    var plot = text("div", "cts-performance-plot");
    section.appendChild(plot);
    var tooltip = text("div", "cts-turn-tooltip");
    tooltip.hidden = true;
    tooltip.setAttribute("role", "tooltip");
    section.appendChild(tooltip);
    var rateColor = "#568fe8", latencyColor = "#a49aba";
    function valid(value) { return typeof value === "number" && Number.isFinite(value) && value >= 0; }
    function svgNode(tag, attrs, value) {
      var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      Object.keys(attrs || {}).forEach(function (key) { node.setAttribute(key, String(attrs[key])); });
      if (value != null) node.textContent = value;
      return node;
    }
    var lastWidth = 0;
    function draw(width) {
      width = Math.round(width);
      if (width <= 0 || width === lastWidth || disposed) return;
      lastWidth = width;
      plot.replaceChildren(); tooltip.hidden = true;
      var svg = svgNode("svg", {width:"100%", height:176, viewBox:"0 0 " + width + " 176", "aria-label":"最近轮次输出速率与首 Token 延迟"});
      plot.appendChild(svg);
      var left = 40, right = width - 36, top = 28, bottom = 146;
      var cell = Math.max(1, right - left) / rows.length;
      var rates = rows.map(function (row) { return row.outputTokensPerSecond; });
      var delays = rows.map(function (row) { return valid(row.firstTokenLatencyMs) ? row.firstTokenLatencyMs / 1000 : null; });
      var rateMax = Math.max.apply(null, [1].concat(rates.filter(valid)));
      var delayMax = Math.max.apply(null, [1].concat(delays.filter(valid)));
      function y(value, max) { return bottom - value / max * (bottom - top); }
      function tick(value) { return value >= 1000 ? formatTokens(value) : formatDecimal(value); }
      svg.appendChild(svgNode("text", {x:0, y:14, fill:rateColor}, "Token/s"));
      svg.appendChild(svgNode("text", {x:width, y:14, "text-anchor":"end", fill:"currentColor"}, "秒"));
      [0, 0.5, 1].forEach(function (fraction) {
        var height = y(fraction, 1);
        svg.appendChild(svgNode("line", {x1:left, x2:right, y1:height, y2:height, stroke:"currentColor", opacity:0.12}));
        svg.appendChild(svgNode("text", {x:left-7, y:height+4, "text-anchor":"end", fill:"currentColor"}, tick(fraction*rateMax)));
        svg.appendChild(svgNode("text", {x:right+6, y:height+4, fill:"currentColor"}, tick(fraction*delayMax)));
      });
      rows.forEach(function (row, i) {
        var center = left + cell * (i + 0.5);
        if (valid(delays[i])) {
          var barWidth = Math.max(1, Math.min(18, cell * 0.6));
          svg.appendChild(svgNode("rect", {class:"cts-latency-bar", x:center-barWidth/2, y:y(delays[i],delayMax),
            width:barWidth, height:bottom-y(delays[i],delayMax), fill:latencyColor, opacity:0.45}));
        }
        if (width >= 300 || i === 0 || i === rows.length-1 || i === Math.floor((rows.length-1)/2)) {
          svg.appendChild(svgNode("text", {x:center,y:168,"text-anchor":"middle",fill:"currentColor"}, String(i+1)));
        }
      });
      var path = "", connected = false;
      rates.forEach(function (rate, i) {
        if (!valid(rate)) { connected = false; return; }
        path += (connected ? "L" : "M") + (left + cell*(i+0.5)) + "," + y(rate,rateMax) + " ";
        connected = true;
      });
      if (path) svg.appendChild(svgNode("path", {class:"cts-rate-line",d:path,fill:"none",stroke:rateColor,"stroke-width":2}));
      rates.forEach(function (rate, i) {
        if (valid(rate)) svg.appendChild(svgNode("circle", {class:"cts-rate-dot",cx:left+cell*(i+0.5),cy:y(rate,rateMax),r:3,fill:rateColor}));
      });
      var reasons = {turn_start_missing:"缺少轮次开始记录",duration_unavailable:"耗时记录不可用",
        latency_unavailable:"首 Token 延迟不可用",output_usage_unavailable:"输出用量记录不足",
        source_conflict:"来源记录存在差异",turn_usage_scope_unconfirmed:"轮次用量范围尚无法确认"};
      rows.forEach(function (row, i) {
        var date = new Date(row.completedAt);
        var detail = "第 " + (i+1) + " 轮 · " + (isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", {hour12:false}))
          + "\n输出速率：" + formatDecimal(rates[i]) + " Token/s"
          + "\n首 Token 延迟：" + formatDecimal(delays[i]) + " 秒";
        [row.rateUnavailableReason,row.latencyUnavailableReason].forEach(function (reason) {
          if (reasons[reason] && detail.indexOf(reasons[reason]) === -1) detail += "\n" + reasons[reason];
        });
        var hit = svgNode("rect", {class:"cts-turn-hit",x:left+cell*i,y:top,width:cell,height:bottom-top,
          fill:"transparent",tabindex:0,"aria-label":detail});
        function show() { tooltip.textContent = detail; tooltip.hidden = false; }
        function hide() { tooltip.hidden = true; }
        hit.addEventListener("mouseenter",show); hit.addEventListener("focus",show);
        hit.addEventListener("mouseleave",hide); hit.addEventListener("blur",hide);
        hit.addEventListener("keydown",function (event) { if (event.key === "Escape") hide(); });
        svg.appendChild(hit);
      });
    }
    draw(plot.clientWidth || 300);
    if (typeof ResizeObserver !== "undefined") {
      performanceObserver = new ResizeObserver(function (entries) {
        if (entries[0]) draw(entries[0].contentRect.width);
      });
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
      if (data && data.conversationId === activeId && data.selectionEpoch === selectionEpoch
          && data.sessionTurnPerformance && Array.isArray(data.sessionTurnPerformance.turns)
          && data.sessionTurnPerformance.turns.length) {
        appendTurnPerformance(body, data.sessionTurnPerformance);
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
    var breakdown = text("div", "cts-breakdown");
    breakdown.appendChild(breakdownMetric("输入", data.session.input_tokens));
    breakdown.appendChild(breakdownMetric("输出", data.session.output_tokens));
    body.appendChild(breakdown);
    appendTurnPerformance(body, data.sessionTurnPerformance);

    appendReroutes(body, activeId, data.sessionByModel);

    var featureRows = data.sessionByFeature || [];
    var modelRows = data.sessionByModel || [];
    if (featureRows.length) {
      var features = text("div", "cts-section");
      features.appendChild(statsGrid(featureRows));
      body.appendChild(features);
    }
    if (modelRows.length) {
      var models = text("div", "cts-section");
      models.appendChild(modelStatsGrid(modelRows, credits ? (credits.byModel || {}) : null));
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
    if (records.some(function (record) {
      return !panel || !panel.contains(record.target);
    })) reroutesDirty = true;
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
