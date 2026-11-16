/* Local quest planning UI. All server and user content is inserted as text. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    world: null,
    session: null,
    detail: null,
    selectedVersion: null,
    selectedRunId: null,
    activeRunId: null,
    busy: false,
    decisionPending: false,
  };
  const STORAGE_KEY = "misttide-planner-session";
  let sessionGeneration = 0;
  const requestIds = { session: 0, version: 0, replay: 0, poll: 0 };
  function latestRequest(kind) {
    const generation = sessionGeneration;
    const requestId = ++requestIds[kind];
    return () => generation === sessionGeneration && requestId === requestIds[kind];
  }
  const purposeNames = { prepare_brief: "需求摘要", generate: "生成候选", revise: "修订版本" };
  const scenarioNames = {
    normal: "正常生成", conflict: "核心目标冲突", invalid_entity: "未知实体",
    invalid_citation: "引用错误", invalid_graph: "任务图错误",
    missing_condition: "动作条件缺失", tool_failure: "工具失败",
    loop_limit: "步骤上限", provider_error: "模型服务错误",
    repair_format: "格式修复",
  };
  const svgNS = "http://www.w3.org/2000/svg";

  function setText(id, value) { $(id).textContent = value == null ? "" : String(value); }
  function clear(element) { while (element.firstChild) element.removeChild(element.firstChild); }
  function make(tag, className, textValue) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (textValue != null) element.textContent = String(textValue);
    return element;
  }
  function appendText(parent, tag, className, value) {
    const child = make(tag, className, value);
    parent.appendChild(child);
    return child;
  }
  function badge(id, textValue, tone = "muted") {
    const element = $(id);
    element.className = "pill " + tone;
    element.textContent = textValue;
  }
  function notice(message, tone = "info") {
    const element = $("notice");
    element.className = "notice " + tone;
    element.textContent = message;
    element.hidden = !message;
  }
  function errorMessage(error) {
    return error && error.message ? error.message : "操作未完成，请稍后重试。";
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
    });
    let body = null;
    try { body = await response.json(); } catch { /* The API error path may have no JSON body. */ }
    if (!response.ok) {
      const message = body && body.error && body.error.message
        ? body.error.message
        : `请求失败（HTTP ${response.status}）。`;
      const error = new Error(message);
      error.status = response.status;
      error.code = body && body.error ? body.error.code : null;
      throw error;
    }
    return body;
  }
  function post(path, body) {
    return api(path, { method: "POST", body: JSON.stringify(body) });
  }
  function worldName(collection, id) {
    const items = state.world && state.world.world && state.world.world[collection];
    const match = Array.isArray(items) && items.find((item) =>
      item.character_id === id || item.location_id === id || item.item_id === id);
    return match ? `${match.name}（${id}）` : id;
  }
  function pretty(value) {
    if (value == null) return "—";
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2);
  }
  function compact(value, limit = 180) {
    const result = pretty(value).replace(/\s+/g, " ");
    return result.length > limit ? result.slice(0, limit - 1) + "…" : result;
  }
  function versions() {
    return Array.isArray(state.session && state.session.versions) ? state.session.versions : [];
  }
  function versionNumber(version) {
    return Number(version && (version.version ?? version.candidate_version ?? version.number));
  }
  function latestVersion() {
    return versions().reduce((highest, item) => Math.max(highest, versionNumber(item) || 0), 0);
  }
  function confirmedBrief() {
    const brief = state.session && state.session.brief;
    return brief && brief.confirmed ? brief : null;
  }
  function promptMatchesBrief() {
    const brief = confirmedBrief();
    return !!brief && $("goal-input").value.trim() === String(brief.original_input || "").trim();
  }
  function promptMatchesCurrentBrief() {
    const brief = state.session && state.session.brief;
    return !!brief && $("goal-input").value.trim() === String(brief.original_input || "").trim();
  }
  function liveReady() {
    const capabilities = state.world && state.world.capabilities;
    return !!(capabilities && capabilities.live_enabled && capabilities.live_ready);
  }
  function mode() { return $("mode-select").value; }
  function scenario() { return mode() === "fake" ? $("scenario-select").value : "normal"; }
  function runAllowed() { return mode() !== "live" || liveReady(); }

  function renderCapabilities() {
    const capabilities = state.world && state.world.capabilities || {};
    const liveOption = $("mode-select").querySelector('option[value="live"]');
    liveOption.disabled = !liveReady();
    if (!liveReady() && mode() === "live") $("mode-select").value = "fake";
    setText("live-capability", liveReady()
      ? "真实模式已就绪。调用与费用由服务端记录。"
      : `真实模式未就绪：${capabilities.live_reason || "需先配置并启用模型、密钥、embedding 与计价信息。"}`);
    $("scenario-row").hidden = mode() !== "fake";
    $("script-note").hidden = mode() !== "fake";
    setText("script-note", mode() === "fake"
      ? `Fake 脚本：${scenarioNames[scenario()] || scenario()}。只覆盖明确预设输入，不代表任意要求都已理解。`
      : "");
    renderActions();
  }
  function renderBrief() {
    const brief = state.session && state.session.brief;
    const exists = !!brief;
    $("brief-empty").hidden = exists;
    $("brief-content").hidden = !exists;
    setText("brief-version", exists ? `v${brief.version}` : "—");
    if (!exists) {
      badge("brief-status", "待确认");
      renderActions();
      return;
    }
    badge("brief-status", brief.confirmed ? "已确认" : "待确认", brief.confirmed ? "good" : "warn");
    setText("brief-goal", brief.goal || "—");
    renderList("brief-must-keep", Array.isArray(brief.must_keep) && brief.must_keep.length ? brief.must_keep : ["无明确保留项"]);
    const constraints = brief.constraints || {};
    const values = [];
    for (const id of constraints.required_character_ids || []) values.push("角色：" + worldName("characters", id));
    for (const id of constraints.required_location_ids || []) values.push("地点：" + worldName("locations", id));
    for (const id of constraints.required_item_ids || []) values.push("道具：" + worldName("items", id));
    if (constraints.required_ending) values.push("结局：" + constraints.required_ending);
    for (const condition of constraints.protected_conditions || []) values.push("保留条件：" + compact(condition, 120));
    renderList("brief-constraints", values.length ? values : ["无额外结构约束"]);
    renderActions();
  }
  function renderList(id, values) {
    const parent = $(id);
    clear(parent);
    for (const value of values) appendText(parent, "li", "", value);
  }
  function renderStateSnapshot() {
    const parent = $("state-snapshot");
    clear(parent);
    const snapshot = state.session && state.session.state_snapshot || state.world && state.world.state;
    if (!snapshot) { appendText(parent, "p", "", "状态暂不可用。"); return; }
    appendText(parent, "p", "", `世界 ${snapshot.world_version} · 状态版本 ${snapshot.version}`);
    const player = snapshot.player || {};
    appendText(parent, "p", "", `玩家：${worldName("locations", player.location_id)} · ${player.coins ?? "—"} 枚钱币`);
    const inventory = (snapshot.inventory || []).filter((item) => item.quantity > 0)
      .map((item) => `${worldName("items", item.item_id)} ×${item.quantity}`);
    appendText(parent, "p", "", "持有：" + (inventory.join("、") || "无"));
    const trust = (snapshot.npcs || []).map((npc) => `${worldName("characters", npc.npc_id)} ${npc.trust_level}`);
    appendText(parent, "p", "", "信任等级：" + (trust.join("、") || "无"));
    const quests = (snapshot.quests || []).map((quest) => `${quest.quest_id}（${quest.status}）`);
    appendText(parent, "p", "", "已有任务：" + (quests.join("、") || "无"));
  }
  function renderActions() {
    const hasSession = !!state.session;
    const brief = state.session && state.session.brief;
    const matches = promptMatchesBrief();
    const ready = runAllowed();
    $("new-session").disabled = state.busy;
    $("prepare-brief").disabled = state.busy || !ready || !$("goal-input").value.trim();
    $("confirm-brief").disabled = state.busy || !brief || !!brief.confirmed || !promptMatchesCurrentBrief();
    $("run-generate").disabled = state.busy || !ready || !matches || state.decisionPending || latestVersion() > 0;
    $("run-revision").disabled = state.busy || !ready || !matches || state.decisionPending || latestVersion() === 0 || !$("revision-input").value.trim();
    $("mode-select").disabled = state.busy;
    $("scenario-select").disabled = state.busy;
    $("goal-input").disabled = state.busy;
    $("revision-input").disabled = state.busy;
    $("use-revision-example").disabled = state.busy;
    $("use-goal-example").disabled = state.busy;
    $("use-conflict-example").disabled = state.busy;
    setText("brief-action-help", !hasSession
      ? "首次整理时自动创建一份本地会话。"
      : brief && brief.confirmed && !matches
        ? "输入与已确认摘要不同；重新整理并确认后才能生成。"
        : "");
    setText("generate-help", state.busy ? "运行中，请等待结果。" :
      state.decisionPending ? "先处理待决定事项，再整理并确认需求。" :
      latestVersion() > 0 ? "已有候选。使用中栏的自然语言修改生成新版本。" :
      !confirmedBrief() ? "先确认左侧需求摘要。" :
      !matches ? "输入已改变，需重新整理并确认。" :
      !ready ? "真实模式尚未就绪。" : "使用当前已确认需求生成候选。");
    renderReviewActions();
  }
  function renderReviewActions() {
    const detail = state.detail;
    const candidate = detail && detail.candidate;
    const review = detail && detail.review;
    const report = detail && detail.report;
    const isLatest = !!candidate && Number(candidate.version) === latestVersion();
    const briefMatches = !!candidate && !!confirmedBrief()
      && Number(candidate.brief_version) === Number(confirmedBrief().version);
    const canReview = !state.busy && isLatest && briefMatches && !review;
    $("accept-version").disabled = !canReview || !report || !report.valid || !$("review-reason").value.trim();
    $("reject-version").disabled = !canReview || !$("review-reason").value.trim();
    $("review-reason").disabled = !canReview;
    const accepted = !!review && review.decision === "accept";
    $("export-json").disabled = !accepted;
    $("export-markdown").disabled = !accepted;
    if (review) {
      badge("review-status", review.decision === "accept" ? "已采纳" : "已拒绝", review.decision === "accept" ? "good" : "bad");
      setText("review-help", `v${review.candidate_version} 已${review.decision === "accept" ? "采纳" : "拒绝"}。理由：${review.reason || "未填写"}`);
    } else {
      badge("review-status", "未审核");
      setText("review-help", !candidate ? "候选生成后可人工审核。" :
        !isLatest ? "历史版本只读；请选择最新候选进行审核。" :
        !briefMatches ? "此候选绑定旧需求；请基于当前已确认需求生成修订版。" :
        report && !report.valid ? "静态检查未通过；可拒绝并说明原因，不能采纳。" :
        "审核精确绑定当前版本与内容摘要。填写理由后采纳或拒绝。");
    }
  }
  function renderVersions() {
    const select = $("version-select");
    const list = versions().slice().sort((a, b) => versionNumber(b) - versionNumber(a));
    clear(select);
    if (!list.length) {
      const option = make("option", "", "暂无版本");
      select.appendChild(option);
      select.disabled = true;
      state.selectedVersion = null;
      setText("version-meta", "一份策划 · 多个版本");
      return;
    }
    for (const item of list) {
      const number = versionNumber(item);
      if (!Number.isFinite(number)) continue;
      const option = make("option", "", `版本 ${number}${number === latestVersion() ? " · 最新" : ""}`);
      option.value = String(number);
      select.appendChild(option);
    }
    if (!list.some((item) => versionNumber(item) === state.selectedVersion)) {
      state.selectedVersion = latestVersion();
    }
    select.value = String(state.selectedVersion);
    select.disabled = false;
    setText("version-meta", `${list.length} 个版本 · 世界 ${state.session.world_version}`);
  }
  async function loadVersion(number) {
    if (!state.session || !state.session.candidate_id || !number) return;
    const currentRequest = latestRequest("version");
    const candidateId = state.session.candidate_id;
    const isCurrent = () => currentRequest() && state.session
      && state.session.candidate_id === candidateId && state.selectedVersion === number;
    try {
      const detail = await api(`/candidates/${encodeURIComponent(candidateId)}/versions/${number}`);
      if (!isCurrent()) return;
      state.detail = detail;
      renderCandidate();
      renderReport();
      renderDiff();
      renderReviewActions();
    } catch (error) {
      if (!isCurrent()) return;
      notice("读取版本失败：" + errorMessage(error) + " 可重新选择版本再试。", "error");
    }
  }
  function renderCandidate() {
    const candidate = state.detail && state.detail.candidate;
    const draft = candidate && candidate.draft;
    $("candidate-empty").hidden = !!draft;
    $("candidate-content").hidden = !draft;
    badge("candidate-status", draft ? `v${candidate.version}` : "尚无候选", draft ? "good" : "muted");
    if (!draft) return;
    setText("quest-title", draft.title || "未命名任务");
    setText("quest-summary", draft.summary || "—");
    setText("node-count", `${(draft.nodes || []).length} 个节点`);
    setText("edge-count", `${(draft.edges || []).length} 条连接`);
    renderGraph(draft);
    renderNodes(draft);
    renderList("citation-list", (draft.citations || []).map((citation) =>
      `${citation.lore_id} · 世界 ${citation.world_version}`));
  }
  function svg(tag, attributes = {}, value = null) {
    const element = document.createElementNS(svgNS, tag);
    for (const [key, attributeValue] of Object.entries(attributes)) element.setAttribute(key, String(attributeValue));
    if (value != null) element.textContent = String(value);
    return element;
  }
  function graphPositions(draft) {
    const nodes = Array.isArray(draft.nodes) ? draft.nodes : [];
    const edges = Array.isArray(draft.edges) ? draft.edges : [];
    const known = new Set(nodes.map((node) => node.node_id));
    const depth = new Map(nodes.map((node) => [node.node_id, 0]));
    const incoming = new Map(nodes.map((node) => [node.node_id, 0]));
    const outgoing = new Map(nodes.map((node) => [node.node_id, []]));
    for (const edge of edges) {
      if (!known.has(edge.source) || !known.has(edge.target)) continue;
      incoming.set(edge.target, incoming.get(edge.target) + 1);
      outgoing.get(edge.source).push(edge.target);
    }
    const queue = nodes.map((node) => node.node_id).filter((id) => incoming.get(id) === 0);
    for (let index = 0; index < queue.length; index++) {
      const current = queue[index];
      for (const target of outgoing.get(current)) {
        depth.set(target, Math.max(depth.get(target), depth.get(current) + 1));
        incoming.set(target, incoming.get(target) - 1);
        if (incoming.get(target) === 0) queue.push(target);
      }
    }
    const columns = new Map();
    for (const node of nodes) {
      const column = depth.get(node.node_id);
      if (!columns.has(column)) columns.set(column, []);
      columns.get(column).push(node.node_id);
    }
    const positions = new Map();
    for (const [column, ids] of columns) {
      ids.forEach((id, row) => positions.set(id, { x: 22 + column * 192, y: 26 + row * 88 }));
    }
    const width = Math.max(300, 44 + (Math.max(...depth.values(), 0) + 1) * 192);
    const height = Math.max(160, 48 + Math.max(...Array.from(columns.values(), (items) => items.length), 1) * 88);
    return { positions, width, height };
  }
  function renderGraph(draft) {
    const root = $("quest-graph");
    clear(root);
    const { positions, width, height } = graphPositions(draft);
    root.setAttribute("viewBox", `0 0 ${width} ${height}`);
    root.setAttribute("width", String(width));
    root.setAttribute("height", String(height));
    const defs = svg("defs");
    const marker = svg("marker", { id: "arrowhead", markerWidth: 7, markerHeight: 7, refX: 5, refY: 3.5, orient: "auto" });
    marker.appendChild(svg("path", { d: "M0 0 L7 3.5 L0 7 z", class: "graph-arrow" }));
    defs.appendChild(marker);
    root.appendChild(defs);
    for (const edge of draft.edges || []) {
      const from = positions.get(edge.source);
      const to = positions.get(edge.target);
      if (!from || !to) continue;
      const x1 = from.x + 144, y1 = from.y + 27;
      const x2 = to.x, y2 = to.y + 27;
      const bend = Math.max(34, Math.abs(x2 - x1) * .45);
      const path = svg("path", {
        class: "graph-edge", d: `M${x1} ${y1} C${x1 + bend} ${y1},${x2 - bend} ${y2},${x2} ${y2}`,
        "marker-end": "url(#arrowhead)",
      });
      root.appendChild(path);
      const label = svg("text", { class: "graph-edge-label", x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 7, "text-anchor": "middle" }, edge.label || "");
      root.appendChild(label);
    }
    (draft.nodes || []).forEach((node, index) => {
      const position = positions.get(node.node_id);
      if (!position) return;
      const group = svg("g", { class: `graph-node${node.ending ? " ending" : ""}`, role: "button", tabindex: 0, "aria-label": `定位节点：${node.title}` });
      group.appendChild(svg("rect", { x: position.x, y: position.y, width: 144, height: 54, rx: 8 }));
      group.appendChild(svg("text", { class: "node-number", x: position.x + 10, y: position.y + 16 }, String(index + 1).padStart(2, "0")));
      const title = String(node.title || node.node_id);
      group.appendChild(svg("text", { x: position.x + 10, y: position.y + 36 }, title.length > 9 ? title.slice(0, 8) + "…" : title));
      const focusNode = () => {
        const card = Array.from($("node-list").children).find((element) => element.dataset.nodeId === node.node_id);
        if (!card) return;
        for (const item of $("node-list").children) item.classList.remove("focused");
        card.classList.add("focused");
        card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      };
      group.addEventListener("click", focusNode);
      group.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focusNode(); }
      });
      root.appendChild(group);
    });
  }
  function conditionText(condition) {
    if (!condition) return "条件未注明";
    if (condition.kind === "has_item") return `持有 ${worldName("items", condition.entity_id)} ×${condition.value || 1}`;
    if (condition.kind === "trust_at_least") return `${worldName("characters", condition.entity_id)} 信任 ≥ ${condition.value}`;
    if (condition.kind === "at_location") return `位于 ${worldName("locations", condition.entity_id)}`;
    return compact(condition, 110);
  }
  function actionText(action) {
    const names = { talk: "交谈", open_gate: "开启潮门", reveal_route: "揭示路线", record_tide: "记录潮声" };
    const base = `${names[action.kind] || action.kind} · ${action.target_id}`;
    return (action.conditions || []).length
      ? `${base}（条件：${action.conditions.map(conditionText).join("；")}）`
      : base + "（无编码条件）";
  }
  function renderNodes(draft) {
    const parent = $("node-list");
    clear(parent);
    (draft.nodes || []).forEach((node, index) => {
      const card = make("article", "node-card");
      card.dataset.nodeId = node.node_id;
      const header = make("div", "node-card-header");
      const heading = make("div");
      appendText(heading, "span", "node-card-index", `NODE ${String(index + 1).padStart(2, "0")} · ${node.node_id}`);
      appendText(heading, "h5", "", node.title || node.node_id);
      header.appendChild(heading);
      const tags = make("div", "node-meta");
      if (node.location_id) appendText(tags, "span", "meta-chip", worldName("locations", node.location_id));
      if (node.ending) appendText(tags, "span", "meta-chip", "结局");
      header.appendChild(tags);
      card.appendChild(header);
      appendText(card, "p", "", node.text || "—");
      const details = [];
      if ((node.character_ids || []).length) details.push("角色：" + node.character_ids.map((id) => worldName("characters", id)).join("、"));
      if ((node.item_ids || []).length) details.push("道具：" + node.item_ids.map((id) => worldName("items", id)).join("、"));
      if ((node.actions || []).length) details.push("动作：" + node.actions.map(actionText).join("；"));
      if ((node.effects || []).length) details.push("效果：" + node.effects.join("；"));
      if (node.ending) details.push("结局：" + node.ending);
      for (const detail of details) appendText(card, "div", "node-detail", detail);
      const exits = (draft.edges || []).filter((edge) => edge.source === node.node_id);
      if (exits.length) appendText(card, "div", "node-detail", "后续：" + exits.map((edge) => `${edge.label} → ${edge.target}`).join("；"));
      parent.appendChild(card);
    });
  }
  function renderReport() {
    const report = state.detail && state.detail.report;
    $("report-empty").hidden = !!report;
    $("report-content").hidden = !report;
    if (!report) { badge("report-status", "待运行"); return; }
    badge("report-status", report.valid ? "静态通过" : "发现问题", report.valid ? "good" : "bad");
    const findings = Array.isArray(report.findings) ? report.findings : [];
    setText("report-summary", report.valid
      ? `结构与已编码规则通过静态检查。内容摘要 ${String(report.content_hash || "").slice(0, 12)}…`
      : `发现 ${findings.length} 项检查结果，需修订或拒绝。`);
    const parent = $("finding-list");
    clear(parent);
    if (!findings.length) appendText(parent, "li", "", "未发现静态检查问题。");
    for (const finding of findings) {
      const item = make("li");
      appendText(item, "strong", "", `${finding.severity || "info"} · ${finding.code || "check"}`);
      appendText(item, "span", "", finding.message || "—");
      if (finding.location) appendText(item, "span", "location", finding.location);
      if (Array.isArray(finding.evidence) && finding.evidence.length) appendText(item, "span", "location", finding.evidence.join("；"));
      parent.appendChild(item);
    }
    renderList("manual-list", Array.isArray(report.manual_review) && report.manual_review.length
      ? report.manual_review : ["剧情质量、对白语义与玩家实际可完成性仍需人工审核。"]);
  }
  function renderDiff() {
    const parent = $("diff-content");
    clear(parent);
    const changes = state.detail && Array.isArray(state.detail.diff) ? state.detail.diff : [];
    setText("diff-count", changes.length ? `${changes.length} 处` : "—");
    if (!changes.length) {
      appendText(parent, "p", "muted-text", "此版本没有可显示的父版本差异。");
      return;
    }
    for (const change of changes) {
      const item = make("div", "diff-item");
      appendText(item, "code", "", change.path || "内容");
      appendText(item, "div", "before", "− " + compact(change.before));
      appendText(item, "div", "after", "+ " + compact(change.after));
      parent.appendChild(item);
    }
  }
  function renderRuns() {
    const runs = Array.isArray(state.session && state.session.runs) ? state.session.runs.slice().reverse() : [];
    setText("run-count", `${runs.length} 次`);
    const parent = $("run-list");
    clear(parent);
    for (const run of runs) {
      const chip = make("button", "run-chip" + (run.run_id === state.selectedRunId ? " active" : ""),
        `${purposeNames[run.purpose] || run.purpose || "运行"} · ${run.status || "—"}`);
      chip.type = "button";
      chip.addEventListener("click", () => selectRun(run.run_id));
      parent.appendChild(chip);
    }
  }
  async function selectRun(runId) {
    if (!runId) return;
    const currentRequest = latestRequest("replay");
    const isCurrent = () => currentRequest() && state.selectedRunId === runId;
    state.selectedRunId = runId;
    renderRuns();
    try {
      const replay = await api(`/runs/${encodeURIComponent(runId)}/replay`);
      if (!isCurrent()) return;
      const events = Array.isArray(replay.events) ? replay.events : [];
      const parent = $("replay-list");
      clear(parent);
      $("replay-empty").hidden = events.length > 0;
      parent.hidden = events.length === 0;
      for (const event of events) {
        const item = make("li");
        const kindNames = { start: "开始运行", input_supplement: "补充输入", termination: "运行结束", tool_call: "工具调用", tool_result: "工具结果" };
        const parts = [kindNames[event.kind] || event.kind || "事件"];
        if (event.step_number != null) parts.push(`步骤 ${event.step_number}`);
        if (event.tool_name) parts.push(event.tool_name);
        appendText(item, "span", "event-kind", parts.join(" · "));
        if (event.message) appendText(item, "div", "", event.message);
        if (event.instruction != null) appendText(item, "pre", "", "公开输入 " + pretty(event.instruction));
        if (event.arguments != null) appendText(item, "pre", "", "输入 " + pretty(event.arguments));
        if (event.observation != null) appendText(item, "pre", "", "结果 " + pretty(event.observation));
        if (event.usage != null) appendText(item, "pre", "", "用量 " + pretty(event.usage));
        if (event.duration_ms != null) appendText(item, "div", "muted-text", `${event.duration_ms} ms`);
        parent.appendChild(item);
      }
      return events;
    } catch (error) {
      if (!isCurrent()) return;
      notice("读取执行记录失败：" + errorMessage(error), "error");
      return [];
    }
  }
  function renderRun(run) {
    if (!run) return;
    const status = run.status || "unknown";
    const labels = { running: "运行中", succeeded: "已完成", needs_decision: "待决定", failed: "失败", interrupted: "已中断" };
    const tones = { running: "warn", succeeded: "good", needs_decision: "warn", failed: "bad", interrupted: "bad" };
    badge("run-status", labels[status] || status, tones[status] || "muted");
    setText("run-message", run.message || run.termination_reason ||
      (status === "running" ? "正在检索设定、读取状态并准备结果…" : "运行结束。"));
    $("run-progress").hidden = status !== "running";
  }
  function renderDecision(run) {
    const result = run && run.result;
    const decision = result && result.decision;
    const pending = !!result && result.status === "needs_decision";
    state.decisionPending = pending;
    $("decision-panel").hidden = !pending;
    if (!pending) { renderActions(); return; }
    setText("decision-message", decision && (decision.explanation || decision.message || decision.reason)
      || result.message || run.message || "当前要求无法安全地自动解释。");
    const alternatives = decision && (decision.alternatives || decision.options || decision.suggestions);
    const items = Array.isArray(alternatives) ? alternatives : alternatives ? [alternatives] : [];
    renderList("decision-options", items.map((item) => typeof item === "string" ? item : compact(item, 250)));
    $("decision-options").hidden = items.length === 0;
    renderActions();
  }
  async function ensureSession() {
    if (state.session) return;
    sessionGeneration += 1;
    const session = await post("/sessions", {});
    localStorage.setItem(STORAGE_KEY, session.session_id);
    state.session = { ...session, versions: [], runs: [] };
    setText("session-label", `会话 ${session.session_id} · 世界 ${session.world_version} · 初始状态只读`);
  }
  async function refreshSession(selectLatest = false) {
    if (!state.session) return;
    const currentRequest = latestRequest("session");
    const sessionId = state.session.session_id;
    const isCurrent = () => currentRequest() && state.session && state.session.session_id === sessionId;
    let fresh;
    try {
      fresh = await api(`/sessions/${encodeURIComponent(sessionId)}`);
    } catch (error) {
      if (!isCurrent()) return;
      throw error;
    }
    if (!isCurrent()) return;
    state.session = fresh;
    localStorage.setItem(STORAGE_KEY, fresh.session_id);
    setText("session-label", `会话 ${fresh.session_id} · 世界 ${fresh.world_version} · 初始状态只读`);
    renderStateSnapshot();
    if (selectLatest) state.selectedVersion = latestVersion();
    renderBrief();
    renderVersions();
    renderRuns();
    if (state.selectedVersion) await loadVersion(state.selectedVersion);
    else {
      state.detail = null;
      renderCandidate();
      renderReport();
      renderDiff();
    }
    renderActions();
  }
  function setBusy(value) { state.busy = value; renderActions(); }
  async function pollRun(runId) {
    const currentRequest = latestRequest("poll");
    const isCurrent = () => currentRequest() && state.activeRunId === runId;
    let errors = 0;
    let polls = 0;
    while (isCurrent()) {
      try {
        const run = await api(`/runs/${encodeURIComponent(runId)}`);
        if (!isCurrent()) return;
        renderRun(run);
        polls += 1;
        if (run.status === "running" && (polls === 1 || polls % 2 === 0)) {
          const events = await selectRun(runId);
          if (!isCurrent()) return;
          const last = events && events[events.length - 1];
          if (last) setText("run-message", last.message || `${last.tool_name || last.kind || "运行"} · 步骤 ${last.step_number || "—"}`);
        }
        if (run.status !== "running" && run.status !== "queued") {
          await refreshSession(run.purpose !== "prepare_brief");
          if (!isCurrent()) return;
          renderDecision(run);
          await selectRun(runId);
          if (!isCurrent()) return;
          state.activeRunId = null;
          setBusy(false);
          if (run.status === "failed" || run.status === "interrupted") {
            notice(`${run.message || "运行未完成。"} 可检查回放后重新发起。`, "error");
          } else if (run.status === "needs_decision" || run.result && run.result.status === "needs_decision") {
            notice("这次运行需要你决定。请查看中栏建议，修改并重新确认需求。", "warn");
          } else {
            notice(run.purpose === "prepare_brief"
              ? "摘要已准备好，请核对后确认。" : "运行完成，请检查候选与报告。");
          }
          return;
        }
        errors = 0;
      } catch (error) {
        if (!isCurrent()) return;
        errors += 1;
        if (errors >= 3) {
          state.activeRunId = null;
          setBusy(false);
          notice("暂时无法读取运行状态：" + errorMessage(error) + " 刷新页面可继续查看这次运行。", "error");
          return;
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 1100));
    }
  }
  async function startRun(purpose) {
    if (state.busy) return;
    const instruction = purpose === "revise" ? $("revision-input").value.trim() : $("goal-input").value.trim();
    if (!instruction) { notice("请先填写要求。", "warn"); return; }
    if (!runAllowed()) { notice("真实模式未就绪，请检查左侧说明。", "warn"); return; }
    if (purpose !== "prepare_brief" && (!promptMatchesBrief() || state.decisionPending)) {
      notice("先整理并确认当前需求，再生成或修订。", "warn"); return;
    }
    if (purpose === "revise" && !latestVersion()) { notice("请先生成候选，再提出修改。", "warn"); return; }
    try {
      setBusy(true);
      notice("");
      await ensureSession();
      const response = await post(`/sessions/${encodeURIComponent(state.session.session_id)}/runs`, {
        purpose, instruction, mode: mode(), scenario: scenario(),
        base_version: purpose === "revise" ? latestVersion() : null,
      });
      state.activeRunId = response.run_id;
      state.selectedRunId = response.run_id;
      badge("run-status", "运行中", "warn");
      setText("run-message", `${purposeNames[purpose]}已开始，正在等待结果…`);
      $("run-progress").hidden = false;
      await refreshSession();
      await pollRun(response.run_id);
    } catch (error) {
      state.activeRunId = null;
      setBusy(false);
      $("run-progress").hidden = true;
      notice(error.status === 409
        ? "已有运行在进行，请稍后刷新页面查看结果。" : errorMessage(error), "error");
    }
  }
  async function confirmBrief() {
    const brief = state.session && state.session.brief;
    if (!brief || !promptMatchesCurrentBrief()) {
      notice("输入已改变，请先重新整理摘要。", "warn"); return;
    }
    try {
      setBusy(true);
      await post(`/sessions/${encodeURIComponent(state.session.session_id)}/brief/confirm`, { version: brief.version });
      state.decisionPending = false;
      $("decision-panel").hidden = true;
      await refreshSession();
      notice("需求已确认，现在可以生成候选。");
    } catch (error) {
      notice(errorMessage(error), "error");
    } finally { setBusy(false); }
  }
  async function submitReview(decision) {
    const detail = state.detail;
    const reason = $("review-reason").value.trim();
    if (!detail || !detail.candidate || !reason) { notice("请选择候选并填写审核理由。", "warn"); return; }
    try {
      setBusy(true);
      const candidate = detail.candidate;
      await post(`/candidates/${encodeURIComponent(candidate.candidate_id)}/versions/${candidate.version}/review`, {
        decision, reason, content_hash: candidate.content_hash,
      });
      await loadVersion(candidate.version);
      notice(decision === "accept" ? "此版本已采纳，可以导出。" : "此版本已拒绝，记录已保存。");
    } catch (error) {
      notice(error.status === 409
        ? "候选或审核状态已变化，请重新选择版本核对。" : errorMessage(error), "error");
      if (error.status === 409) await refreshSession();
    } finally { setBusy(false); }
  }
  async function exportVersion(format) {
    const detail = state.detail;
    if (!detail || !detail.candidate || !detail.review || detail.review.decision !== "accept") return;
    const candidate = detail.candidate;
    const path = `/candidates/${encodeURIComponent(candidate.candidate_id)}/versions/${candidate.version}/export?format=${format}`;
    try {
      const response = await fetch(path);
      if (!response.ok) {
        let body = null;
        try { body = await response.json(); } catch { /* Keep generic error. */ }
        throw new Error(body && body.error ? body.error.message : "导出失败。");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = make("a");
      link.href = url;
      link.download = `misttide-quest-v${candidate.version}.${format === "json" ? "json" : "md"}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notice("导出已准备好。");
    } catch (error) { notice(errorMessage(error), "error"); }
  }
  async function newSession() {
    if (state.busy) return;
    sessionGeneration += 1;
    try {
      setBusy(true);
      const session = await post("/sessions", {});
      state.session = { ...session, versions: [], runs: [] };
      state.detail = null;
      state.selectedVersion = null;
      state.selectedRunId = null;
      state.decisionPending = false;
      localStorage.setItem(STORAGE_KEY, session.session_id);
      $("decision-panel").hidden = true;
      $("revision-input").value = "";
      $("review-reason").value = "";
      clear($("replay-list"));
      $("replay-list").hidden = true;
      $("replay-empty").hidden = false;
      badge("run-status", "空闲");
      setText("run-message", "等待整理需求。");
      $("run-progress").hidden = true;
      await refreshSession();
      notice("新会话已创建。上一次会话记录仍保存在本地数据库中。");
    } catch (error) { notice(errorMessage(error), "error"); }
    finally { setBusy(false); }
  }
  async function restoreSession() {
    const savedId = localStorage.getItem(STORAGE_KEY);
    if (!savedId) return;
    const generation = ++sessionGeneration;
    try {
      state.session = { session_id: savedId };
      await refreshSession(true);
      if (generation !== sessionGeneration) return;
      const runs = Array.isArray(state.session.runs) ? state.session.runs : [];
      const latest = runs[runs.length - 1];
      if (latest && latest.run_id) {
        const run = await api(`/runs/${encodeURIComponent(latest.run_id)}`);
        if (generation !== sessionGeneration) return;
        renderRun(run);
        renderDecision(run);
        await selectRun(run.run_id);
        if (generation !== sessionGeneration) return;
        if (run.status === "running") {
          state.activeRunId = run.run_id;
          setBusy(true);
          pollRun(run.run_id);
        }
      }
    } catch (error) {
      if (generation !== sessionGeneration) return;
      sessionGeneration += 1;
      state.session = null;
      localStorage.removeItem(STORAGE_KEY);
      setText("session-label", "尚未创建会话");
      notice("上次会话无法恢复，可创建新会话。", "warn");
    }
  }
  function bindEvents() {
    $("new-session").addEventListener("click", newSession);
    $("prepare-brief").addEventListener("click", () => startRun("prepare_brief"));
    $("confirm-brief").addEventListener("click", confirmBrief);
    $("run-generate").addEventListener("click", () => startRun("generate"));
    $("run-revision").addEventListener("click", () => startRun("revise"));
    $("use-revision-example").addEventListener("click", () => {
      $("revision-input").value = state.world && state.world.fake_prompts && state.world.fake_prompts.revision
        || "保留结局，把第二个分支改成帮助米拉";
      renderActions();
      $("revision-input").focus();
    });
    $("use-goal-example").addEventListener("click", () => {
      $("goal-input").value = state.world && state.world.fake_prompts && state.world.fake_prompts.goal || "";
      $("scenario-select").value = "normal";
      renderCapabilities();
      $("goal-input").focus();
    });
    $("use-conflict-example").addEventListener("click", () => {
      $("goal-input").value = state.world && state.world.fake_prompts && state.world.fake_prompts.conflict || "";
      $("scenario-select").value = "conflict";
      renderCapabilities();
      $("goal-input").focus();
    });
    $("mode-select").addEventListener("change", renderCapabilities);
    $("scenario-select").addEventListener("change", renderCapabilities);
    $("goal-input").addEventListener("input", renderActions);
    $("revision-input").addEventListener("input", renderActions);
    $("review-reason").addEventListener("input", renderReviewActions);
    $("version-select").addEventListener("change", async (event) => {
      state.selectedVersion = Number(event.target.value);
      state.detail = null;
      await loadVersion(state.selectedVersion);
    });
    $("accept-version").addEventListener("click", () => submitReview("accept"));
    $("reject-version").addEventListener("click", () => submitReview("reject"));
    $("export-json").addEventListener("click", () => exportVersion("json"));
    $("export-markdown").addEventListener("click", () => exportVersion("markdown"));
  }
  async function init() {
    bindEvents();
    renderActions();
    try {
      state.world = await api("/api/world");
      $("goal-input").value = state.world.fake_prompts && state.world.fake_prompts.goal || "";
      renderCapabilities();
      renderStateSnapshot();
      await restoreSession();
      if (state.session && state.session.brief && state.session.brief.original_input) {
        $("goal-input").value = state.session.brief.original_input;
      }
      renderBrief();
      renderActions();
    } catch (error) {
      notice("加载世界设定失败：" + errorMessage(error) + " 请刷新页面重试。", "error");
    }
  }
  init();
})();
