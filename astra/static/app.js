/* ============================================================
   Astra — клиентская логика
   ============================================================ */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const el = (tag, cls, html) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
};

const state = {
  ws: null,
  connected: false,
  reconnectDelay: 1000,
  config: {},
  models: [],
  sessions: [],
  session: null,
  busy: false,
  recording: false,
  appState: "idle",
  ttsEnabled: true,
  streamNode: null,
  streamBuffer: "",
  streamRun: null,
  stickToBottom: true,
  confirmQueue: [],
  confirmOpen: false,
  statusModel: "",
  health: null,
  lastUserText: "",
  autoScrolled: true,
  endpoint: null,
  endpointProbes: {},
  // ---------- режим работы ----------
  mode: "text",            // text | voice
  modeChosen: false,       // экран выбора уже пройден
  sphere: null,            // 3D-сфера голосового режима
  sphereMini: null,        // маленькая сфера на экране выбора
  statusFeed: [],          // блок статусов под сферой
  statusSeq: 0,
  sttModels: [],
  sttCurrent: "",
  sttReady: false,
  sttStatus: null,
  micBroken: false,        // микрофон недоступен — показываем текстовый ввод
  nativeWindow: false,
  transcript: [],
  voiceHintAt: 0,
};

/* ============================== API ============================== */
async function api(path, options = {}) {
  const opts = Object.assign({ headers: { "Content-Type": "application/json" } }, options);
  if (opts.body && typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const data = await resp.json();
      detail = data.detail || data.message || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  const type = resp.headers.get("content-type") || "";
  return type.includes("application/json") ? resp.json() : resp.text();
}

/* ============================== утилиты ============================== */
function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function inlineMd(text) {
  const codes = [];
  let out = escapeHtml(text).replace(/`([^`]+)`/g, (m, code) => {
    codes.push(code);
    return `\u0000CODE${codes.length - 1}\u0000`;
  });
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer noopener">$1</a>');
  out = out.replace(/(^|[\s(])(https?:\/\/[^\s<)"]+)/g,
    '$1<a href="$2" target="_blank" rel="noreferrer noopener">$2</a>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/(^|[\s(])_([^_\n]+)_/g, "$1<em>$2</em>");
  out = out.replace(/\u0000CODE(\d+)\u0000/g, (m, i) => `<code>${codes[Number(i)]}</code>`);
  return out;
}

function md(text) {
  const src = String(text || "");
  const parts = src.split(/```/);
  let html = "";
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      // блок кода
      const lines = part.replace(/^\n/, "").split("\n");
      let lang = "";
      if (lines.length && /^[\w+-]{1,16}$/.test(lines[0].trim())) {
        lang = lines[0].trim();
        lines.shift();
      }
      const code = escapeHtml(lines.join("\n").replace(/\n+$/, ""));
      html += `<pre><button class="copy-btn" data-copy>копировать</button><code data-lang="${escapeHtml(lang)}">${code}</code></pre>`;
      return;
    }
    const lines = part.split("\n");
    let listType = null;
    let para = [];
    const flushPara = () => {
      if (para.length) {
        html += `<p>${para.map(inlineMd).join("<br>")}</p>`;
        para = [];
      }
    };
    const closeList = () => {
      if (listType) {
        html += `</${listType}>`;
        listType = null;
      }
    };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) {
        flushPara();
        closeList();
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flushPara();
        closeList();
        const level = Math.min(3, heading[1].length + 1);
        html += `<h${level}>${inlineMd(heading[2])}</h${level}>`;
        continue;
      }
      if (/^&gt;\s?/.test(escapeHtml(line)) || /^>\s?/.test(line)) {
        flushPara();
        closeList();
        html += `<blockquote>${inlineMd(line.replace(/^>\s?/, ""))}</blockquote>`;
        continue;
      }
      const ul = line.match(/^\s*[-*•]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const wanted = ul ? "ul" : "ol";
        if (listType !== wanted) {
          closeList();
          html += `<${wanted}>`;
          listType = wanted;
        }
        html += `<li>${inlineMd((ul || ol)[1])}</li>`;
        continue;
      }
      closeList();
      para.push(line);
    }
    flushPara();
    closeList();
  });
  return html;
}

function timeStr(ts) {
  const d = new Date((ts || Date.now() / 1000) * 1000);
  return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function relTime(ts) {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} дн назад`;
  return new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "2-digit", month: "short" });
}

function toast(message, level = "info", timeout = 4200) {
  const node = el("div", `toast ${level}`);
  node.innerHTML = `<div style="flex:1">${escapeHtml(message)}</div>`;
  const close = el("button", "t-close", "×");
  close.onclick = () => node.remove();
  node.appendChild(close);
  $("#toasts").appendChild(node);
  if (timeout) setTimeout(() => node.remove(), timeout);
}

function formatArgs(args) {
  if (!args || typeof args !== "object") return "";
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => {
      let val = typeof v === "string" ? v : JSON.stringify(v);
      if (val && val.length > 46) val = val.slice(0, 46) + "…";
      return `${k}=${val}`;
    })
    .join(", ");
}

const CATEGORY_ICON = {
  apps: "▶",
  input: "⌨",
  system: "⚙",
  files: "🗂",
  shell: "❯",
  web: "🌐",
  misc: "•",
};

/* ============================== WebSocket ============================== */
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws`;
}

function connect() {
  if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) return;
  let ws;
  try {
    ws = new WebSocket(wsUrl());
  } catch (err) {
    scheduleReconnect();
    return;
  }
  state.ws = ws;

  ws.onopen = () => {
    state.connected = true;
    state.reconnectDelay = 1000;
    setBanner("");
    if (state.session) ws.send(JSON.stringify({ type: "session_select", session_id: state.session.id }));
    refreshHealth();
  };

  ws.onmessage = (event) => {
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    handleEvent(data);
  };

  ws.onclose = () => {
    state.connected = false;
    setBanner("Соединение с сервером потеряно. Переподключаюсь…", "err");
    scheduleReconnect();
  };

  ws.onerror = () => {};
}

function scheduleReconnect() {
  setTimeout(() => {
    state.reconnectDelay = Math.min(state.reconnectDelay * 1.6, 12000);
    connect();
  }, state.reconnectDelay);
}

function send(payload) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    toast("Нет соединения с сервером", "err");
    return false;
  }
  state.ws.send(JSON.stringify(payload));
  return true;
}

/* ============================== обработка событий ============================== */
function handleEvent(ev) {
  switch (ev.type) {
    case "hello":
      state.config = ev.config || {};
      state.ttsEnabled = !!getCfg("voice.tts_enabled");
      state.nativeWindow = !!ev.native_window;
      if (ev.stt && ev.stt.model) {
        state.sttCurrent = ev.stt.model;
        state.sttReady = !!ev.stt.ready;
        fillSttSelect({ current: ev.stt.model });
      }
      applyConfigToUi();
      if (ev.model) setStatusModel(ev.model);
      if (ev.endpoint) updateEndpoint(ev.endpoint);
      updateModeFoot();
      // сообщаем серверу режим вкладки: в голосовом он включает «постоянно слушаю»
      if (state.modeChosen) send({ type: "mode", mode: state.mode });
      break;

    case "state":
      setAppState(ev.state);
      break;

    case "thought":
      addThought(ev.text);
      break;

    case "action_start":
      addActionCard(ev, false);
      break;

    case "action_result":
      updateActionCard(ev);
      finishStatus(ev);
      break;

    case "token":
      appendToken(ev.text);
      break;

    case "user_message":
      if (ev.session && (!state.session || state.session.id !== ev.session.id)) {
        state.session = ev.session;
        if (state.connected) send({ type: "session_select", session_id: ev.session.id });
      }
      if (ev.message && ev.message.content) {
        const guess = ev.message.content.trim().replace(/\s+/g, " ").slice(0, 42);
        $("#chat-title").textContent = guess || "Новый чат";
        $("#chat-subtitle").textContent = "идёт работа…";
      }
      appendMessage(ev.message);
      if (state.mode === "voice" && ev.message && ev.message.content) {
        appendTranscript("you", ev.message.content);
        clearStatuses();
      }
      loadSessions();
      break;

    case "assistant_message":
      finalizeAssistant(ev.message, ev.elapsed);
      if (state.mode === "voice" && ev.message && ev.message.content) {
        appendTranscript("astra", ev.message.content);
      }
      markStatusesDone();
      loadSessions();
      if (state.session) {
        const count = document.querySelectorAll(".msg.assistant").length;
        $("#chat-subtitle").textContent = `${count} ${count === 1 ? "ответ" : "ответов"} · ${new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
      }
      break;

    case "regenerate_start":
      // убираем последний ответ и начинаем заново
      const msgs = $$(".msg.assistant");
      if (msgs.length) msgs[msgs.length - 1].remove();
      state.streamRun = null;
      state.streamNode = null;
      state.streamBuffer = "";
      setAppState("thinking");
      break;

    case "note_message":
      appendNote(ev.message.content);
      break;

    case "confirm_request":
      enqueueConfirm(ev);
      break;

    case "confirm_timeout":
      toast("Время на подтверждение истекло — действие отменено", "warn");
      break;

    case "stt_result":
      if (ev.text && !ev.auto_sent) {
        $("#input").value = ev.text;
        autoGrow();
        $("#input").focus();
        state.lastUserText = ev.text;
      }
      if (ev.text && ev.auto_sent) state.lastUserText = ev.text;
      break;

    case "status":
      pushStatus(ev.text, { kind: ev.kind || "action", id: ev.id || "" });
      break;

    case "tts_done":
      // озвучка закончилась: сервер сам снова включит микрофон в голосовом режиме
      if (state.sphere) state.sphere.setLevel(0);
      break;

    case "stt_model":
      applySttModel(ev);
      break;

    case "cuda":
      state.cuda = ev;
      break;

    case "cuda_install":
      if (!ev.running && ev.ok) toast("Библиотеки CUDA 12 установлены", "ok", 4000);
      break;

    case "stt_error":
      state.sttStatus = ev.status || state.sttStatus;
      toast(ev.message || "Не удалось распознать речь", "warn", 12000);
      pushStatus("Речь не распознана — попробуй ещё раз", { kind: "system" });
      break;

    case "stt_info":
      state.sttStatus = ev.status || state.sttStatus;
      toast(ev.message, "info", 8000);
      pushStatus("Распознаю речь на процессоре", { kind: "system" });
      break;

    case "level":
      if (ev.kind === "mic" && state.orb) state.orb.setLevel(ev.value);
      if (ev.kind === "speech" && state.orb) state.orb.setLevel(ev.value);
      if (state.sphere) state.sphere.setLevel(ev.value);
      break;

    case "tts":
      state.tts = Object.assign({}, state.tts || {}, ev.status || {}, {
        ok: ev.ok, engine: ev.engine, label: ev.label, error: ev.error,
      });
      if (ev.status && ev.status.last_engine === undefined && ev.engine) {
        state.tts.last_engine = ev.engine;
      }
      updateTtsBadge();
      break;

    case "tts_error":
      showTtsError(ev.message, ev.detail);
      break;

    case "vision":
      state.vision = Object.assign({}, state.vision || {}, ev);
      updateVisionBadge();
      break;

    case "endpoint":
      handleEndpointEvent(ev);
      updateModeFoot();
      break;

    case "session_updated":
      if (state.session && ev.session.id === state.session.id) {
        state.session = ev.session;
        $("#chat-title").textContent = ev.session.title;
      }
      loadSessions();
      break;

    case "toast":
      toast(ev.message, ev.level || "info");
      break;

    case "error":
      toast(ev.message, "err", 8000);
      if (isMicError(ev.message)) showMicFallback(ev.message);
      setAppState("idle");
      break;

    case "pong":
      break;
  }
}

/* ============================== сервер LM Studio ============================== */
function normalizeEp(url) {
  let u = String(url || "").trim().replace(/\/+$/, "");
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "http://" + u;
  try {
    const parsed = new URL(u);
    let path = (parsed.pathname || "").replace(/\/+$/, "");
    if (!path) path = "/v1";
    else if (!path.endsWith("/v1")) path += "/v1";
    return `${parsed.protocol}//${parsed.host}${path}`;
  } catch (_) {
    return u;
  }
}

function bindModelItems(root) {
  $$(".model-item", root).forEach((item) => {
    if (item.dataset.bound) return;
    item.dataset.bound = "1";
    item.onclick = async () => {
      const model = item.dataset.model;
      if (!model) return;
      try {
        await api("/api/config", { method: "POST", body: { patch: { lmstudio: { model } } } });
        state.config.lmstudio = Object.assign({}, state.config.lmstudio, { model });
        setStatusModel(model);
        $$(".model-item", root).forEach((node) => {
          node.classList.toggle("active", node.dataset.model === model);
          const check = node.querySelector(".m-check");
          if (node.dataset.model === model && !check) node.insertAdjacentHTML("beforeend", '<span class="m-check">✓</span>');
          if (node.dataset.model !== model && check) check.remove();
        });
        toast(`Модель: ${model}`, "ok", 2000);
        loadModels();
        refreshHealth();
      } catch (err) {
        toast(err.message, "err");
      }
    };
  });
}

function hostOf(url) {
  if (!url) return "—";
  try {
    return new URL(url).host;
  } catch (_) {
    return String(url).replace(/^https?:\/\//, "").replace(/\/v1$/, "");
  }
}

function updateEndpoint(info) {
  if (!info) return;
  state.endpoint = info;
  const badge = $("#endpoint-badge");
  const dot = $("#endpoint-dot");
  const host = $("#endpoint-host");
  const status = $("#status-endpoint");
  const ok = !!info.ok;
  const isLan = info.kind === "lan";
  const label = ok ? hostOf(info.url) : "нет сервера";
  if (badge) {
    badge.classList.toggle("lan", ok && isLan);
    badge.title = ok
      ? `LM Studio: ${info.url} (${info.kind_label || ""}) · моделей: ${info.models_count || 0}`
      : (info.error || "LM Studio недоступен");
  }
  if (dot) dot.className = "dot " + (ok ? "ok" : "err");
  if (host) host.textContent = label;
  if (status) status.textContent = ok ? `${hostOf(info.url)} · ${info.kind === "lan" ? "сеть" : "локально"}` : "сервер недоступен";
  if (info.model) setStatusModel(info.model);
}

function handleEndpointEvent(ev) {
  const previous = state.endpoint && state.endpoint.url;
  updateEndpoint(ev);
  const current = ev.url;
  if (!ev.ok) {
    setBanner(ev.error || "LM Studio недоступен", "warn");
    toast("Сервер LM Studio недоступен: " + (ev.error || ""), "err", 7000);
  } else {
    setBanner("");
    if (previous && current && previous !== current) {
      toast(`Переключился на сервер ${hostOf(current)} (${ev.kind_label || ""})`, "ok", 5000);
    }
  }
  loadModels();
}

/* ============================== состояние ============================== */
const STATE_LABEL = {
  idle: "готов",
  thinking: "думаю…",
  acting: "выполняю…",
  waiting_confirmation: "жду подтверждения",
  speaking: "говорю…",
  listening: "слушаю…",
  transcribing: "распознаю речь…",
};

function setAppState(next) {
  state.appState = next || "idle";
  state.busy = ["thinking", "acting", "waiting_confirmation", "speaking", "transcribing"].includes(state.appState);
  state.recording = state.appState === "listening";

  const statusEl = $("#status-text");
  statusEl.textContent = STATE_LABEL[state.appState] || state.appState;
  statusEl.classList.toggle("busy", state.busy);
  $("#mic-btn").classList.toggle("recording", state.recording);
  $("#stop-btn").classList.toggle("hidden", !(state.busy || state.recording));
  $("#send-btn").classList.toggle("hidden", state.busy || state.recording);

  const showOrb = ["listening", "transcribing", "speaking"].includes(state.appState);
  $("#orb-panel").classList.toggle("hidden", !showOrb);
  if (state.orb) {
    const modeMap = {
      listening: "listening",
      transcribing: "transcribing",
      speaking: "speaking",
      thinking: "thinking",
    };
    state.orb.setMode(modeMap[state.appState] || "idle");
    if (state.appState !== "listening" && state.appState !== "speaking") state.orb.setLevel(0);
  }
  syncSphere();

  const labels = {
    listening: ["Слушаю…", "Скажи задачу · Esc — отмена"],
    transcribing: ["Распознаю речь…", "модель faster-whisper"],
    speaking: ["Отвечаю…", "нажми «Стоп», чтобы прервать"],
  };
  if (labels[state.appState]) {
    $("#orb-label").textContent = labels[state.appState][0];
    $("#orb-hint").textContent = labels[state.appState][1];
    $("#orb-actions").classList.toggle("hidden", state.appState === "transcribing");
  }

  if (state.appState === "idle") {
    finalizeStreaming();
  }
}

function updateTtsBadge() {
  const node = $("#status-tts");
  if (!node) return;
  if (!getCfg("voice.tts_enabled", true)) {
    node.textContent = "голос: выкл";
    node.style.color = "var(--muted-2)";
    node.title = "Озвучка выключена — включите кнопкой динамика или в настройках";
    return;
  }
  const t = state.tts || {};
  if (t.last_error && !t.last_engine) {
    node.textContent = "голос: ошибка";
    node.style.color = "var(--err)";
    node.title = t.last_error;
    node.style.cursor = "pointer";
    node.onclick = () => openSettings("voice");
    return;
  }
  const names = { "edge-tts": "edge", sapi: "офлайн", pyttsx3: "pyttsx3" };
  node.textContent = "голос: " + (names[t.last_engine] || "авто");
  node.style.color = t.last_engine ? "var(--ok)" : "var(--muted)";
  node.title = t.last_engine
    ? "Последняя озвучка: " + t.last_engine + (t.last_error ? " (" + t.last_error + ")" : "")
    : "Движок подберётся автоматически";
}

let ttsErrorAt = 0;
function showTtsError(hint, detail) {
  const now = Date.now();
  if (now - ttsErrorAt < 20000) return;   // не спамим одинаковыми уведомлениями
  ttsErrorAt = now;
  toast(hint || "Озвучка недоступна", "err", 9000);
  if (detail) console.warn("[tts]", detail);
  const node = $("#status-tts");
  if (node) {
    node.textContent = "голос: ошибка";
    node.style.color = "var(--err)";
    node.title = (hint ? hint + "\n" : "") + (detail || "");
    node.style.cursor = "pointer";
    node.onclick = () => openSettings("voice");
  }
}

function updateVisionBadge() {
  const node = $("#status-vision");
  if (!node) return;
  const v = state.vision || {};
  const mode = v.mode || getCfg("lmstudio.vision", "auto");
  let text = "зрение: выкл";
  let color = "var(--muted-2)";
  if (mode !== "off") {
    if (v.supported === true) { text = "зрение: 👁 вкл"; color = "var(--ok)"; }
    else if (v.supported === false) { text = "зрение: нет (текст)"; color = "var(--warn)"; }
    else { text = "зрение: авто"; color = "var(--muted)"; }
  }
  node.textContent = text;
  node.style.color = color;
  node.title = v.supported === false
    ? (v.last_error || "Модель не принимает изображения")
    : "Агент получает снимки окна и экрана";
}

function setStatusModel(model) {
  state.statusModel = model || "";
  $("#status-model").textContent = model || "модель не выбрана";
}

/* ============================== сессии ============================== */
async function loadSessions() {
  try {
    const q = $("#session-search").value.trim();
    const data = await api(`/api/sessions?q=${encodeURIComponent(q)}`);
    state.sessions = data.sessions || [];
    renderSessions();
  } catch (err) {
    console.error(err);
  }
}

function sessionGroup(ts) {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86400000;
  const t = (ts || 0) * 1000;
  if (t >= startOfToday) return "Сегодня";
  if (t >= startOfToday - day) return "Вчера";
  if (t >= startOfToday - 7 * day) return "На этой неделе";
  if (t >= startOfToday - 30 * day) return "В этом месяце";
  return "Раньше";
}

function renderSessions() {
  const list = $("#session-list");
  list.innerHTML = "";
  $("#session-count").textContent = state.sessions.length;
  if (!state.sessions.length) {
    list.appendChild(el("div", "session-empty", "Пока пусто. Начни первый диалог ✨"));
    return;
  }
  let lastGroup = "";
  for (const s of state.sessions) {
    const group = s.pinned ? "Закреплённые" : sessionGroup(s.updated_at);
    if (group !== lastGroup) {
      lastGroup = group;
      list.appendChild(el("div", "session-group-title", escapeHtml(group)));
    }
    const item = el("div", "session-item" + (state.session && state.session.id === s.id ? " active" : ""));
    item.dataset.id = s.id;
    item.innerHTML = `
      <div class="s-title">${escapeHtml(s.title || "Без названия")}</div>
      <div class="s-meta">
        <span>${relTime(s.updated_at)}</span>
        ${s.model ? `<span>·</span><span style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(s.model)}</span>` : ""}
      </div>
      ${s.pinned ? `<span class="s-pin">★</span>` : ""}
      <button class="s-menu" title="Действия">
        <svg viewBox="0 0 24 24" width="15" height="15"><circle cx="5" cy="12" r="1.7" fill="currentColor"/><circle cx="12" cy="12" r="1.7" fill="currentColor"/><circle cx="19" cy="12" r="1.7" fill="currentColor"/></svg>
      </button>`;
    item.querySelector(".s-menu").onclick = (e) => {
      e.stopPropagation();
      openSessionMenu(s, e.currentTarget);
    };
    item.onclick = () => openSession(s.id);
    list.appendChild(item);
  }
}

async function openSession(id) {
  if (state.busy) {
    if (!confirm("Ассистент ещё выполняет задачу. Переключить чат?")) return;
    send({ type: "cancel" });
  }
  try {
    const data = await api(`/api/sessions/${id}`);
    state.session = data.session;
    renderSessions();
    $("#chat-title").textContent = data.session.title || "Чат";
    $("#chat-subtitle").textContent = `создан ${relTime(data.session.created_at)}`;
    renderMessages(data.messages || []);
    if (state.connected) send({ type: "session_select", session_id: id });
    if (window.innerWidth <= 900) document.body.classList.remove("sidebar-open");
  } catch (err) {
    toast(`Не удалось открыть чат: ${err.message}`, "err");
  }
}

function newChat() {
  state.session = null;
  state.streamRun = null;
  $("#chat-title").textContent = "Новый чат";
  $("#chat-subtitle").textContent = "выбери модель и просто скажи, что делать";
  const container = $("#messages");
  container.innerHTML = "";
  container.appendChild(buildWelcome());
  renderSessions();
  $("#input").focus();
}

function buildWelcome() {
  const wrap = el("div", "welcome");
  wrap.id = "welcome";
  wrap.innerHTML = `
    <div class="welcome-orb"><div class="orb small" id="welcome-orb">
      <div class="orb-glow"></div><div class="orb-core"></div>
      <div class="orb-ring r1"></div><div class="orb-ring r2"></div><div class="orb-ring r3"></div>
    </div></div>
    <h1>Привет! Я Astra.</h1>
    <p>Я работаю локально через LM Studio и умею управлять твоим компьютером: открывать программы,
      печатать текст, работать внутри открытых окон (например, в браузере), искать файлы, выполнять команды и делать скриншоты.</p>
    <div class="chips">
      <button class="chip" data-prompt="Открой блокнот и напиши в нём список дел на сегодня">Открой блокнот и напиши список дел</button>
      <button class="chip" data-prompt="Какая у меня сейчас загрузка процессора и памяти?">Сколько ресурсов занято?</button>
      <button class="chip" data-prompt="Открой YouTube и найди видео про Python">Открой YouTube с видео про Python</button>
      <button class="chip" data-prompt="Открой в Chrome сайт с погодой в Мурманске и покажи, что на странице">Погода в Chrome</button>
      <button class="chip" data-prompt="Сделай скриншот экрана">Сделай скриншот</button>
    </div>
    <p class="hint">Говори голосом — нажми <kbd>Ctrl</kbd>+<kbd>Space</kbd> или кнопку микрофона.</p>`;
  wrap.querySelectorAll(".chip").forEach((chip) => {
    chip.onclick = () => {
      $("#input").value = chip.dataset.prompt;
      autoGrow();
      sendMessage();
    };
  });
  setTimeout(() => {
    const orbEl = wrap.querySelector(".orb");
    if (orbEl && !window.__welcomeOrb) window.__welcomeOrb = new Orb(orbEl);
  }, 0);
  return wrap;
}

function makeTitleEditable() {
  const titleEl = $("#chat-title");
  if (!titleEl || titleEl.dataset.bound) return;
  titleEl.dataset.bound = "1";
  titleEl.classList.add("editable");
  titleEl.title = "Двойной клик — переименовать";
  titleEl.ondblclick = () => {
    if (!state.session) {
      toast("Начни диалог, чтобы переименовать чат", "warn", 2200);
      return;
    }
    const current = state.session.title || "Новый чат";
    const input = el("input", "title-input");
    input.value = current;
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    let finished = false;
    const finish = async (save) => {
      if (finished) return;
      finished = true;
      const value = input.value.trim();
      const fresh = el("div", "", "");
      fresh.id = "chat-title";
      if (save && value && value !== current) {
        try {
          const data = await api(`/api/sessions/${state.session.id}`, { method: "PATCH", body: { title: value } });
          state.session = data.session || state.session;
          fresh.textContent = value;
          loadSessions();
        } catch (err) {
          toast(err.message, "err");
          fresh.textContent = current;
        }
      } else {
        fresh.textContent = current;
      }
      input.replaceWith(fresh);
      setTimeout(makeTitleEditable, 0);
    };
    input.onblur = () => finish(true);
    input.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { e.preventDefault(); finished = true; const fresh = el("div", "", current); fresh.id = "chat-title"; input.replaceWith(fresh); setTimeout(makeTitleEditable, 0); }
    };
  };
}

function openSessionMenu(session, anchor) {
  closeFloating();
  const rect = anchor.getBoundingClientRect();
  const menu = el("div", "context-menu");
  menu.style.position = "fixed";
  menu.style.top = `${rect.bottom + 4}px`;
  menu.style.left = `${Math.max(8, rect.right - 180)}px`;
  menu.style.zIndex = 300;
  menu.innerHTML = `
    <button data-act="rename">Переименовать</button>
    <button data-act="pin">${session.pinned ? "Открепить" : "Закрепить"}</button>
    <button data-act="export">Экспорт в Markdown</button>
    <button data-act="delete" class="danger">Удалить</button>`;
  const style = document.createElement("style");
  style.textContent = `.context-menu{background:var(--panel-2);border:1px solid var(--border-strong);border-radius:11px;
    padding:5px;min-width:180px;box-shadow:var(--shadow);display:flex;flex-direction:column;gap:2px}
    .context-menu button{background:transparent;border:none;text-align:left;padding:8px 10px;border-radius:7px;
    font-size:13px;color:var(--text)}
    .context-menu button:hover{background:rgba(255,255,255,.07)}
    .context-menu button.danger{color:#ffb4b4}`;
  document.head.appendChild(style);
  menu.querySelector('[data-act="rename"]').onclick = async () => {
    closeFloating();
    const title = prompt("Новое название чата:", session.title);
    if (title && title.trim()) {
      await api(`/api/sessions/${session.id}`, { method: "PATCH", body: { title: title.trim() } });
      if (state.session && state.session.id === session.id) $("#chat-title").textContent = title.trim();
      loadSessions();
    }
  };
  menu.querySelector('[data-act="pin"]').onclick = async () => {
    closeFloating();
    await api(`/api/sessions/${session.id}`, { method: "PATCH", body: { pinned: !session.pinned } });
    loadSessions();
  };
  menu.querySelector('[data-act="export"]').onclick = () => {
    closeFloating();
    window.open(`/api/sessions/${session.id}/export`, "_blank");
  };
  menu.querySelector('[data-act="delete"]').onclick = async () => {
    closeFloating();
    if (!confirm(`Удалить чат «${session.title}»?`)) return;
    await api(`/api/sessions/${session.id}`, { method: "DELETE" });
    if (state.session && state.session.id === session.id) newChat();
    loadSessions();
  };
  document.body.appendChild(menu);
  setTimeout(() => document.addEventListener("click", closeFloating, { once: true }), 0);
}

function closeFloating() {
  $$(".context-menu").forEach((n) => n.remove());
}

/* ============================== сообщения ============================== */
function clearWelcome() {
  const w = $("#welcome");
  if (w) w.remove();
}

function scrollToBottom(force = false) {
  const container = $("#messages");
  if (!state.stickToBottom && !force) {
    state.unseen = (state.unseen || 0) + 1;
    const badge = $("#scroll-badge");
    const btn = $("#scroll-bottom");
    if (badge && btn) {
      badge.textContent = state.unseen > 99 ? "99+" : String(state.unseen);
      badge.classList.remove("hidden");
      btn.classList.remove("hidden");
    }
    return;
  }
  container.scrollTop = container.scrollHeight + 200;
}

function appendMessage(message) {
  clearWelcome();
  if (message.role === "user") {
    renderUserMessage(message);
  } else if (message.role === "assistant") {
    renderAssistantMessage(message);
  } else if (message.role === "note") {
    appendNote(message.content);
  }
  scrollToBottom();
}

function renderMessages(messages) {
  const container = $("#messages");
  container.innerHTML = "";
  if (!messages.length) {
    container.appendChild(buildWelcome());
    return;
  }
  for (const msg of messages) appendMessage(msg);
  state.stickToBottom = true;
  scrollToBottom(true);
}

function renderUserMessage(message) {
  const node = el("div", "msg user");
  node.dataset.id = message.id;
  node.innerHTML = `
    <div class="avatar">Ты</div>
    <div class="body">
      <div class="who"><span>${(message.meta && message.meta.modality === "voice") ? "🎙 голосом" : "ты"}</span>
        <span class="stamp">${timeStr(message.created_at)}</span></div>
      <div class="bubble">${escapeHtml(message.content)}</div>
    </div>`;
  $("#messages").appendChild(node);
}

function renderAssistantMessage(message) {
  const actions = (message.meta && message.meta.actions) || [];
  const node = el("div", "msg assistant");
  node.dataset.id = message.id;
  const body = el("div", "body");
  const who = el("div", "who",
    `<span>Astra</span><span class="stamp">${timeStr(message.created_at)}</span>` +
    (message.meta && message.meta.elapsed ? `<span class="stamp">· ${message.meta.elapsed} с</span>` : ""));
  body.appendChild(who);

  if (message.meta && message.meta.thought) {
    body.appendChild(buildThought(message.meta.thought));
  }
  if (actions.length) {
    const cards = el("div", "actions-stack collapsed");
    actions.forEach((a) => cards.appendChild(buildActionCard(a)));
    body.appendChild(cards);
    updateStepsSummary(cards);      // «3 шага · все успешно» — шаги в истории не разворачиваем
  }

  const bubble = el("div", "bubble", md(message.content));
  body.appendChild(bubble);
  body.appendChild(buildMessageActions(message));
  node.appendChild(el("div", "avatar", "A"));
  node.appendChild(body);
  $("#messages").appendChild(node);
  bindCopyButtons(node);
}

function buildThought(text) {
  const node = el("div", "thought");
  node.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13"><path d="M12 3a7 7 0 0 0-4 12.7V19l4-2 4 2v-3.3A7 7 0 0 0 12 3z" fill="none" stroke="currentColor" stroke-width="1.6"/></svg><span>${escapeHtml(text)}</span>`;
  return node;
}

function buildMessageActions(message) {
  const node = el("div", "msg-actions");
  const copy = el("button", "mini-btn", "копировать");
  copy.onclick = () => {
    navigator.clipboard.writeText(message.content || "").then(() => toast("Скопировано", "ok", 1500));
  };
  const regen = el("button", "mini-btn accent", "повторить");
  regen.onclick = () => {
    if (!state.session) return;
    if (state.busy) {
      toast("Сначала дождись или останови текущую задачу", "warn", 2500);
      return;
    }
    send({ type: "regenerate", session_id: state.session.id });
  };
  const speak = el("button", "mini-btn", "озвучить");
  speak.onclick = () => send({ type: "speak", text: message.content || "" });
  const del = el("button", "mini-btn", "удалить");
  del.onclick = async () => {
    if (!confirm("Удалить это сообщение из истории?")) return;
    if (message.id) await api(`/api/sessions/${message.session_id}/messages/${message.id}`, { method: "DELETE" });
    const node = $(`.msg[data-id="${message.id}"]`);
    if (node) node.remove();
  };
  node.append(copy, regen, speak, del);
  return node;
}

function appendNote(text) {
  clearWelcome();
  $("#messages").appendChild(el("div", "note-line", escapeHtml(text)));
  scrollToBottom();
}

/* ---- живой запуск агента ---- */
function ensureRunContainer() {
  if (state.streamRun && document.body.contains(state.streamRun.body)) return state.streamRun;
  clearWelcome();
  const node = el("div", "msg assistant");
  const body = el("div", "body");
  const who = el("div", "who", `<span>Astra</span><span class="stamp">${timeStr()}</span>`);
  const typing = el("div", "typing", "<span></span><span></span><span></span>");
  body.appendChild(who);
  body.appendChild(typing);
  node.appendChild(el("div", "avatar", "A"));
  node.appendChild(body);
  $("#messages").appendChild(node);
  state.streamRun = { node, body, typing, bubble: null, cards: null, thought: null };
  scrollToBottom(true);
  return state.streamRun;
}

function addThought(text) {
  const run = ensureRunContainer();
  if (run.thought) {
    run.thought.querySelector("span").textContent = text;
    return;
  }
  const thought = buildThought(text);
  run.thought = thought;
  run.body.insertBefore(thought, run.typing);
  scrollToBottom();
}

/* ---- сворачивание блока шагов ---- */
const STEPS_CHEVRON = '<svg class="chev" viewBox="0 0 24 24" width="12" height="12"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';

function stepsSummary(stack) {
  const cards = [...stack.querySelectorAll(".action-card")];
  const total = cards.length;
  const ok = cards.filter((c) => c.classList.contains("ok")).length;
  const failed = cards.filter((c) => c.classList.contains("err")).length;
  const denied = cards.filter((c) => c.classList.contains("denied")).length;
  const running = cards.filter((c) => c.classList.contains("running")).length;
  const parts = [`${total} ${plural(total, "шаг", "шага", "шагов")}`];
  if (running) parts.push(`выполняется ${running}`);
  if (failed) parts.push(`${failed} с ошибкой`);
  if (denied) parts.push(`${denied} отклонено`);
  if (total && !running && !failed && !denied) parts.push("все успешно");
  return {
    text: parts.join(" · "),
    total, ok, failed, denied, running,
  };
}

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function buildStepsSummary(stack) {
  const info = stepsSummary(stack);
  const button = el("button", "steps-summary");
  button.type = "button";
  button.dataset.open = String(!stack.classList.contains("collapsed"));
  const cls = info.failed ? "has-err" : info.denied ? "has-warn" : info.running ? "is-running" : "is-ok";
  button.classList.add(cls);
  button.innerHTML = `${STEPS_CHEVRON}<span class="steps-title">${info.running ? "Агент работает" : "Шаги агента"}</span>` +
    `<span class="steps-count">${escapeHtml(info.text)}</span>`;
  button.onclick = () => {
    stack.dataset.manual = "1";          // пользователь сам решил — больше не сворачиваем автоматически
    const collapsed = stack.classList.toggle("collapsed");
    button.dataset.open = String(!collapsed);
  };
  return button;
}

function updateStepsSummary(stack) {
  const host = stack.previousElementSibling;
  if (!host || !host.classList || !host.classList.contains("steps-summary")) {
    const button = buildStepsSummary(stack);
    stack.parentNode.insertBefore(button, stack);
    return;
  }
  const fresh = buildStepsSummary(stack);
  host.innerHTML = fresh.innerHTML;
  host.className = fresh.className;
  host.dataset.open = fresh.dataset.open;
}

function collapseSteps(run = state.streamRun) {
  if (!run || !run.cards || !document.body.contains(run.cards)) return;
  updateStepsSummary(run.cards);
  // если пользователь сам развернул шаги — уважаем выбор и не складываем обратно
  if (run.cards.dataset.manual === "1") return;
  run.cards.classList.add("collapsed");
  updateStepsSummary(run.cards);
}

function expandSteps(run = state.streamRun) {
  if (!run || !run.cards) return;
  run.cards.classList.remove("collapsed");
  updateStepsSummary(run.cards);
}

function addActionCard(ev, completed = false) {
  const run = ensureRunContainer();
  if (!run.cards) {
    run.cards = el("div", "actions-stack");
    run.body.insertBefore(run.cards, run.typing);
  }
  const card = buildActionCard({
    id: ev.id,
    tool: ev.tool,
    args: ev.args,
    status: completed ? (ev.ok ? "ok" : ev.denied ? "denied" : "err") : "running",
    result: ev.result,
    error: ev.error,
    artifact: ev.artifact,
    duration_ms: ev.duration_ms,
  });
  card.dataset.actionId = ev.id;
  run.cards.appendChild(card);
  updateStepsSummary(run.cards);
  if (!ev.ok) scrollToBottom();
  scrollToBottom();
  if (run.typing) run.typing.classList.add("hidden");
}

function updateActionCard(ev) {
  const card = !ev.id ? null : document.querySelector(`[data-action-id="${ev.id}"]`);
  if (!card) {
    addActionCard(ev, true);
    return;
  }
  const status = ev.ok ? "ok" : ev.denied ? "denied" : "err";
  card.classList.remove("ok", "err", "denied", "running");
  card.classList.add(status);
  const statusEl = card.querySelector(".action-status");
  if (statusEl) {
    statusEl.innerHTML = statusText({ status, duration_ms: ev.duration_ms });
  }
  if (!card.classList.contains("expanded")) card.classList.remove("expanded");
  const body = card.querySelector(".action-body");
  if (body) {
    const text = ev.ok ? (ev.result || "готово") : (ev.error || "ошибка");
    body.innerHTML = `<pre>${escapeHtml(text)}</pre>` +
      (ev.artifact && ev.artifact.data_uri
        ? `<div class="action-artifact"><img alt="артефакт" src="${ev.artifact.data_uri}"></div>`
        : "");
  }
  const stack = card.closest(".actions-stack");
  if (stack) updateStepsSummary(stack);
  scrollToBottom();
}

function statusText(ev) {
  if (ev.status === "running") return `<span class="spinner"></span> выполняется`;
  if (ev.status === "ok") return `<span style="color:var(--ok)">✓</span> ${ev.duration_ms ? ev.duration_ms + " мс" : "готово"}`;
  if (ev.status === "denied") return `<span style="color:var(--warn)">⊘</span> отклонено`;
  return `<span style="color:var(--err)">✕</span> ошибка`;
}

function buildActionCard(action) {
  const status = action.status || (action.ok === true ? "ok" : action.ok === false ? (action.denied ? "denied" : "err") : "running");
  const card = el("div", `action-card ${status === "running" ? "" : status}`);
  const icon = CATEGORY_ICON[action.category || "misc"] || "•";
  const head = el("div", "action-head");
  head.innerHTML = `
    <div class="action-icon">${icon}</div>
    <div style="min-width:0;flex:1">
      <div class="action-name">${escapeHtml(action.tool || "tool")}</div>
      <div class="action-args">${escapeHtml(formatArgs(action.args))}</div>
    </div>
    <div class="action-status">${statusText({ status, duration_ms: action.duration_ms })}</div>`;
  head.onclick = () => card.classList.toggle("expanded");
  card.appendChild(head);

  const body = el("div", "action-body");
  const text = action.ok === false ? (action.error || "ошибка") : (action.result || "");
  if (text) {
    body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
  } else if (!action.result && status === "running") {
    body.innerHTML = `<pre>Аргументы: ${escapeHtml(JSON.stringify(action.args || {}, null, 2))}</pre>`;
  }
  if (action.vision_sent) {
    const note = el("div", "action-vision-note", "👁 снимок передан модели");
    body.appendChild(note);
  }
  if (action.artifact && action.artifact.data_uri) {
    const wrap = el("div", "action-artifact");
    wrap.innerHTML = `<img alt="${escapeHtml(action.artifact.name || "скриншот")}" src="${action.artifact.data_uri}">`;
    body.appendChild(wrap);
  }
  card.appendChild(body);
  return card;
}

/* ---- стриминг ответа ---- */
function appendToken(text) {
  const run = ensureRunContainer();
  if (run.typing) {
    run.typing.classList.add("hidden");
  }
  if (!run.bubble) {
    run.bubble = el("div", "bubble streaming");
    run.body.appendChild(run.bubble);
    state.streamBuffer = "";
    collapseSteps(run);          // ответ пошёл — шаги больше не мешают читать
  }
  state.streamBuffer += text;
  state.streamNode = run.bubble;
  scheduleStreamRender();
  scrollToBottom();
}

let streamRaf = null;
function scheduleStreamRender() {
  if (streamRaf) return;
  streamRaf = requestAnimationFrame(() => {
    streamRaf = null;
    if (state.streamNode) {
      state.streamNode.innerHTML = md(state.streamBuffer);
      bindCopyButtons(state.streamNode);
    }
  });
}

function finalizeStreaming() {
  if (state.streamNode) {
    state.streamNode.classList.remove("streaming");
    state.streamNode.innerHTML = md(state.streamBuffer);
    bindCopyButtons(state.streamNode);
    state.streamNode = null;
  }
}

function finalizeAssistant(message, elapsed) {
  const run = state.streamRun;
  if (run && document.body.contains(run.node)) {
    if (run.typing) run.typing.classList.add("hidden");
    if (!run.bubble) {
      run.bubble = el("div", "bubble");
      run.body.appendChild(run.bubble);
    }
    run.bubble.classList.remove("streaming");
    run.bubble.innerHTML = md(message.content || state.streamBuffer);
    bindCopyButtons(run.bubble);
    if (message.id) run.node.dataset.id = message.id;
    if (message.meta && message.meta.thought && !run.thought) {
      run.thought = buildThought(message.meta.thought);
      run.body.insertBefore(run.thought, run.body.firstChild.nextSibling || null);
    }
    const who = run.body.querySelector(".who");
    if (who && elapsed) who.insertAdjacentHTML("beforeend", `<span class="stamp">· ${elapsed} с</span>`);
    collapseSteps(run);
    if (!run.body.querySelector(".msg-actions")) run.body.appendChild(buildMessageActions(message));
    state.streamRun = null;
  } else {
    renderAssistantMessage(message);
  }
  state.streamNode = null;
  state.streamBuffer = "";
  scrollToBottom();
}

function bindCopyButtons(root) {
  $$("[data-copy]", root).forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.onclick = () => {
      const code = btn.parentElement.querySelector("code");
      navigator.clipboard.writeText(code ? code.textContent : "").then(() => toast("Скопировано", "ok", 1400));
    };
  });
}

/* ============================== отправка ============================== */
function sendMessage() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text) return;
  if (state.busy) {
    toast("Ассистент ещё выполняет задачу — нажми «Стоп», чтобы прервать", "warn");
    return;
  }
  const payload = { type: "message", text, modality: "text" };
  if (state.session) payload.session_id = state.session.id;
  if (!send(payload)) return;
  input.value = "";
  autoGrow();
  state.streamBuffer = "";
  state.streamNode = null;
  state.streamRun = null;
  state.stickToBottom = true;
  setAppState("thinking");
}

function autoGrow() {
  const input = $("#input");
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
}

/* ============================== голос ============================== */
function toggleMic() {
  if (state.appState === "listening") {
    send({ type: "record_stop" });
    return;
  }
  if (state.appState === "speaking") {
    send({ type: "stop_speech" });
  }
  send({ type: "record_start" });
}

/* ============================== режимы работы ==============================
   При запуске спрашиваем: обычный чат или голосовой режим со сферой.
   Голосовой режим — отдельный «квадратный» экран: сфера из точек, блок статусов,
   микрофон слушает постоянно (сервер выключает его на время ответа и действий).
   ========================================================================== */
const VOICE_STATE_TEXT = {
  idle: "Слушаю…",        // idle = микрофон свободен; ниже подправим по факту записи
  listening: "Слушаю…",
  transcribing: "Распознаю речь…",
  thinking: "Думаю…",
  acting: "Выполняю…",
  waiting_confirmation: "Жду подтверждения",
  speaking: "Отвечаю…",
};

const BUSY_STATES = ["thinking", "acting", "waiting_confirmation", "speaking", "transcribing"];

function isMicError(message) {
  const text = String(message || "").toLowerCase();
  return ["sounddevice", "portaudio", "микрофон", "audio", "микрофон: "].some((m) => text.includes(m));
}

/* ---- сфера ---- */
function syncSphere() {
  const caption = $("#sphere-caption");
  if (state.sphere) {
    const modeMap = {
      listening: "listening", transcribing: "transcribing", speaking: "speaking",
      thinking: "thinking", acting: "thinking", waiting_confirmation: "thinking", idle: "listening",
    };
    state.sphere.setMode(modeMap[state.appState] || "listening");
    if (!["listening", "speaking"].includes(state.appState)) state.sphere.setLevel(0);
  }
  if (caption) {
    let text = VOICE_STATE_TEXT[state.appState] || "Слушаю…";
    if (state.appState === "idle") {
      text = state.micBroken ? "Микрофон недоступен" : (state.recording ? "Слушаю…" : "Готов слушать");
    }
    caption.textContent = text;
    caption.classList.toggle("busy", BUSY_STATES.includes(state.appState) || state.recording);
    caption.classList.toggle("err", state.micBroken);
  }
  const dot = $("#voice-status-dot");
  if (dot) dot.classList.toggle("live", BUSY_STATES.includes(state.appState));
  const stop = $("#voice-stop");
  if (stop) stop.classList.toggle("hidden", !(state.busy || state.recording));
  const mic = $("#voice-mic");
  if (mic) {
    mic.classList.toggle("recording", state.recording);
    mic.classList.toggle("hidden", state.micBroken);
  }
  const hint = $("#voice-hint");
  if (hint) {
    hint.textContent = state.micBroken
      ? "микрофон недоступен — напиши текстом"
      : state.recording ? "слушаю — говори" : state.busy ? "не слушаю, пока работаю" : "микрофон выключен";
  }
}

/* ---- блок статусов ---- */
function statusLimit() {
  const value = Number(getCfg("voice.status_history", 4));
  return Math.max(1, Math.min(8, Number.isFinite(value) ? value : 4));
}

function pushStatus(text, meta = {}) {
  const clean = String(text || "").trim();
  if (!clean) return;
  const feed = state.statusFeed;
  const last = feed[feed.length - 1];
  if (last && last.text === clean) return;            // не повторяем одну и ту же фразу
  if (last && last.state === "current") { last.state = "done"; last.fail = false; }
  feed.push({
    id: (state.statusSeq += 1),
    text: clean,
    kind: meta.kind || "action",
    actionId: meta.id || "",
    state: "current",
    at: Date.now(),
  });
  while (feed.length > statusLimit()) feed.shift();
  renderStatuses();
}

function renderStatuses() {
  const list = $("#voice-status-list");
  if (!list) return;
  if (!state.statusFeed.length) {
    list.innerHTML = `<li class="current"><span class="st-mark">▸</span>` +
      `<span class="st-text">${escapeHtml(state.recording ? "Слушаю…" : "Готов слушать")}</span></li>`;
    return;
  }
  list.innerHTML = state.statusFeed.map((item) => {
    const mark = item.state === "current" ? "▸" : (item.fail ? "✕" : "✓");
    const cls = item.state === "current" ? "current" : (item.fail ? "fail" : "done");
    return `<li class="${cls}"><span class="st-mark">${mark}</span>` +
      `<span class="st-text">${escapeHtml(item.text)}</span></li>`;
  }).join("");
}

function clearStatuses() {
  state.statusFeed = [];
  renderStatuses();
}

function markStatusesDone() {
  state.statusFeed.forEach((item) => {
    if (item.state === "current") item.state = "done";
  });
  renderStatuses();
}

function finishStatus(ev) {
  const feed = state.statusFeed;
  const item = [...feed].reverse().find((it) => it.actionId && it.actionId === ev.id);
  if (item) {
    item.state = "done";
    item.fail = !ev.ok;
    renderStatuses();
  }
}

/* ---- расшифровка разговора ---- */
function appendTranscript(who, text) {
  const clean = String(text || "").trim();
  if (!clean) return;
  state.transcript.push({ who, text: clean });
  while (state.transcript.length > 4) state.transcript.shift();
  renderTranscript();
}

function renderTranscript() {
  const box = $("#voice-transcript");
  if (!box) return;
  box.innerHTML = state.transcript.map((item) => {
    const label = item.who === "you" ? "Ты" : "Астра";
    const cls = item.who === "you" ? "you" : "astra";
    const body = item.text.length > 240 ? item.text.slice(0, 239) + "…" : item.text;
    return `<div class="${cls}"><b>${label}:</b> ${escapeHtml(body)}</div>`;
  }).join("");
  box.scrollTop = box.scrollHeight;
}

/* ---- недоступный микрофон ---- */
function showMicFallback(message) {
  state.micBroken = true;
  const input = $("#voice-text-input");
  if (input) {
    input.classList.remove("hidden");
    input.focus();
  }
  pushStatus("Микрофон недоступен — работаю с текстом", { kind: "system" });
  const hint = $("#voice-hint");
  if (hint) hint.textContent = "микрофон недоступен — напиши текстом";
  if (message) console.warn("[mic]", message);
  syncSphere();
}

/* ---- вход / выход из голосового экрана ---- */
function enterVoiceStage() {
  document.body.classList.add("voice-mode");
  const screen = $("#voice-screen");
  if (screen) screen.classList.remove("hidden");
  if (!state.sphere && window.DotSphere3D) {
    state.sphere = new DotSphere3D($("#sphere-canvas"), { count: 780, radius: 0.34 });
  } else if (state.sphere) {
    state.sphere.resume();
  }
  renderStatuses();
  renderTranscript();
  syncSphere();
  loadSttModels();
  // первый кадр после того, как браузер посчитает размеры арены
  setTimeout(() => { if (state.sphere) state.sphere.resize(); }, 80);
}

function leaveVoiceStage() {
  document.body.classList.remove("voice-mode");
  const screen = $("#voice-screen");
  if (screen) screen.classList.add("hidden");
  if (state.sphere) state.sphere.pause();
}

/* ---- модели распознавания речи ---- */
async function loadSttModels() {
  try {
    const data = await api("/api/stt/models");
    state.sttModels = data.models || [];
    state.sttCurrent = data.current || "";
    state.sttReady = !!data.ready;
    state.sttStatus = data;
    fillSttSelect(data);
  } catch (err) {
    console.warn("[stt]", err);
  }
}

function fillSttSelect(data = {}) {
  if (data.device !== undefined || data.cuda !== undefined) {
    state.sttStatus = Object.assign({}, state.sttStatus || {}, data);
  }
  const select = $("#voice-stt-model");
  if (!select) return;
  const models = state.sttModels.length
    ? state.sttModels
    : [{ id: state.sttCurrent || "large-v3", label: state.sttCurrent || "large-v3" }];
  const current = state.sttCurrent || (data.current || "");
  select.innerHTML = models.map((m) => {
    const marks = [];
    if (m.recommended) marks.push("★");
    if (m.size) marks.push(m.size);
    const tail = marks.length ? ` · ${marks.join(" · ")}` : "";
    return `<option value="${escapeHtml(m.id)}" ${m.id === current ? "selected" : ""}>` +
      `${escapeHtml(m.id)}${escapeHtml(tail)}</option>`;
  }).join("");
  const selected = models.find((m) => m.id === current);
  const device = data.device ? (data.device === "cuda" ? "GPU" : "CPU") : "";
  const cudaError = (data.cuda && data.cuda.error) || "";
  select.title = (selected ? `${selected.label} · точность: ${selected.quality}` : "Модель распознавания речи") +
    (device ? ` · устройство: ${device}` : "") +
    (device === "CPU" && cudaError ? `\nCUDA недоступна: ${cudaError}` : "");
}

async function changeSttModel(model) {
  if (!model || model === state.sttCurrent) return;
  state.sttCurrent = model;
  fillSttSelect();
  pushStatus(`Переключаю распознавание на ${model}`, { kind: "system" });
  try {
    await api("/api/stt/model", { method: "POST", body: { model } });
  } catch (err) {
    toast(`Не удалось сменить модель: ${err.message}`, "err", 7000);
  }
}

function applySttModel(ev) {
  if (!ev) return;
  if (ev.model) state.sttCurrent = ev.model;
  state.sttReady = !!ev.ready;
  fillSttSelect({ current: state.sttCurrent });
  if (ev.loading) {
    pushStatus(`Загружаю модель распознавания ${ev.model}…`, { kind: "system" });
  } else if (ev.error) {
    toast(`Модель ${ev.model}: ${ev.error}`, "err", 9000);
  } else if (ev.ready) {
    pushStatus(`Готова модель распознавания ${ev.model}`, { kind: "system" });
    toast(`Распознавание речи: ${ev.label || ev.model}`, "ok", 3000);
  }
  refreshHealth();
}

/* ---- экран выбора режима ---- */
function modeFromUrl() {
  const mode = new URLSearchParams(location.search).get("mode");
  return mode === "text" || mode === "voice" ? mode : "";
}

function updateModeFoot() {
  const foot = $("#mode-foot");
  if (!foot) return;
  const ep = state.endpoint || {};
  const model = getCfg("lmstudio.model", "");
  if (ep.ok) {
    foot.textContent = `LM Studio: ${hostOf(ep.url) || ep.url}` + (model ? ` · модель ${model}` : " · модель не выбрана");
  } else if (ep.error) {
    foot.textContent = `LM Studio: ${ep.error}`;
  } else {
    foot.textContent = "проверяю LM Studio…";
  }
}

function showModeScreen() {
  document.body.classList.add("mode-picking");
  const screen = $("#mode-screen");
  if (screen) screen.classList.remove("hidden");
  if (!state.sphereMini && window.DotSphere3D) {
    const canvas = $("#mode-sphere");
    if (canvas) state.sphereMini = new DotSphere3D(canvas, { count: 240, radius: 0.40, spin: 0.32 });
  }
  updateModeFoot();
}

async function chooseMode(mode) {
  if (!["text", "voice"].includes(mode)) return;
  const box = $("#mode-remember");
  const remember = !!(box && box.checked);         // «не спрашивать больше»
  try {
    const data = await api("/api/ui/mode", { method: "POST", body: { mode, remember, ask: !remember } });
    state.nativeWindow = !!data.native_window;
    state.config.ui = Object.assign({}, state.config.ui || {}, {
      mode: remember ? mode : "",
      ask_mode: !remember,
      last_mode: mode,
    });
  } catch (err) {
    console.warn("[mode]", err);
  }
  if (state.sphereMini) {
    state.sphereMini.destroy();
    state.sphereMini = null;
  }
  enterMode(mode, { notify: true });
}

function enterMode(mode, opts = {}) {
  state.mode = mode === "voice" ? "voice" : "text";
  state.modeChosen = true;
  document.body.classList.remove("mode-picking");
  const screen = $("#mode-screen");
  if (screen) screen.classList.add("hidden");
  if (state.mode === "voice") enterVoiceStage();
  else leaveVoiceStage();
  send({ type: "mode", mode: state.mode });
  if (state.mode === "voice" && opts.notify && !state.nativeWindow) {
    toast("Голосовой режим. Для квадратного окна запускайте «python run.py --window»", "info", 6500);
  }
  if (state.mode === "text") setTimeout(() => { const input = $("#input"); if (input) input.focus(); }, 80);
}

async function switchMode(mode) {
  if (state.mode === mode) return;
  try {
    await api("/api/ui/mode", {
      method: "POST",
      body: { mode, remember: !getCfg("ui.ask_mode", true) },
    });
  } catch (_) {}
  enterMode(mode, { notify: true });
}

async function initMode() {
  const fromUrl = modeFromUrl();
  if (fromUrl) {
    enterMode(fromUrl, {});
    return;
  }
  const ask = getCfg("ui.ask_mode", true);
  const saved = getCfg("ui.mode", "") || "";
  if (!ask && saved) {
    enterMode(saved, {});
    return;
  }
  showModeScreen();
}

/* ---- отправка текста в голосовом режиме ---- */
function sendVoiceText() {
  const input = $("#voice-text-input");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  if (state.busy) {
    toast("Ассистент ещё выполняет задачу — нажми «Стоп», чтобы прервать", "warn");
    return;
  }
  const payload = { type: "message", text, modality: "voice" };
  if (state.session) payload.session_id = state.session.id;
  if (!send(payload)) return;
  input.value = "";
  appendTranscript("you", text);
  clearStatuses();
  setAppState("thinking");
}

/* ============================== настройки ============================== */
function getCfg(path, fallback = "") {
  const parts = path.split(".");
  let node = state.config;
  for (const p of parts) {
    if (node == null || typeof node !== "object") return fallback;
    node = node[p];
  }
  return node === undefined || node === null ? fallback : node;
}

function applyConfigToUi() {
  $("#tts-toggle").classList.toggle("off", !state.ttsEnabled);
  setStatusModel(getCfg("lmstudio.model", ""));
  updateTtsBadge();
}

function toggleTts() {
  state.ttsEnabled = !state.ttsEnabled;
  api("/api/config", { method: "POST", body: { patch: { voice: { tts_enabled: state.ttsEnabled } } } })
    .then(() => {
      state.config.voice = state.config.voice || {};
      state.config.voice.tts_enabled = state.ttsEnabled;
      $("#tts-toggle").classList.toggle("off", !state.ttsEnabled);
      toast(state.ttsEnabled
        ? "Озвучка включена"
        : "Озвучка выключена — ответы будут только текстом",
        state.ttsEnabled ? "ok" : "info", 2400);
      updateTtsBadge();
    })
    .catch((err) => toast(err.message, "err"));
}

function closeModal() {
  $("#modal-root").innerHTML = "";
  state.confirmOpen = false;
  if (state.confirmQueue.length) openNextConfirm();
}

function openModal(html, opts = {}) {
  const root = $("#modal-root");
  root.innerHTML = "";
  const backdrop = el("div", "modal-backdrop");
  backdrop.onclick = () => {
    if (!opts.sticky) closeModal();
  };
  const modal = el("div", "modal" + (opts.narrow ? " narrow" : ""));
  modal.innerHTML = html;
  root.appendChild(backdrop);
  root.appendChild(modal);
  return modal;
}

/* ---- подтверждение действий ---- */
function enqueueConfirm(ev) {
  state.confirmQueue.push(ev);
  if (!state.confirmOpen) openNextConfirm();
}

function openNextConfirm() {
  const ev = state.confirmQueue.shift();
  if (!ev) {
    state.confirmOpen = false;
    return;
  }
  state.confirmOpen = true;
  const icon = CATEGORY_ICON[ev.category || "misc"] || "!";
  const modal = openModal(`
    <div class="modal-header">
      <h2>Требуется подтверждение</h2>
    </div>
    <div class="modal-body">
      <div class="confirm-box">
        <div class="confirm-icon">${icon}</div>
        <div class="confirm-text">
          Ассистент хочет выполнить <b>${escapeHtml(ev.tool)}</b>.<br>
          <span style="color:var(--muted)">${escapeHtml(ev.summary || "")}</span>
        </div>
      </div>
      <div class="code-args">${escapeHtml(JSON.stringify(ev.args || {}, null, 2))}</div>
    </div>
    <div class="modal-footer">
      <span class="confirm-hint">Enter — разрешить · Esc — отклонить</span>
      <button class="btn subtle" data-act="deny">Отклонить</button>
      <button class="btn subtle" data-act="always">Разрешить всегда</button>
      <button class="btn primary" data-act="allow">Разрешить</button>
    </div>`, { narrow: true, sticky: true });

  let closed = false;
  const onKey = (e) => {
    if (closed) return;
    if (e.key === "Enter") {
      e.preventDefault();
      e.stopPropagation();
      finish(true);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      finish(false);
    }
  };
  document.addEventListener("keydown", onKey, true);
  const cleanup = () => document.removeEventListener("keydown", onKey, true);
  const finish = (approved) => {
    if (closed) return;
    closed = true;
    cleanup();
    send({ type: "confirm_response", id: ev.id, approved });
    closeModal();
  };
  modal.querySelector('[data-act="allow"]').onclick = () => finish(true);
  modal.querySelector('[data-act="deny"]').onclick = () => finish(false);
  modal.querySelector('[data-act="always"]').onclick = async () => {
    try {
      await api("/api/tools/permission", { method: "POST", body: { name: ev.tool, allowed: true } });
      toast(`«${ev.tool}» больше не будет спрашивать подтверждение`, "ok");
    } catch (err) {
      toast(err.message, "err");
    }
    finish(true);
  };
}

/* ---- окно настроек ---- */
async function openSettings(tab = "model") {
  const modal = openModal(`
    <div class="modal-header">
      <h2>Настройки</h2>
      <button class="icon-btn" data-act="close">✕</button>
    </div>
    <div class="tabs">
      <button class="tab" data-tab="model">Модель</button>
      <button class="tab" data-tab="voice">Голос</button>
      <button class="tab" data-tab="safety">Безопасность</button>
      <button class="tab" data-tab="about">О системе</button>
    </div>
    <div class="modal-body" id="settings-body"><div class="typing"><span></span><span></span><span></span></div></div>
    <div class="modal-footer">
      <button class="btn subtle" data-act="close">Закрыть</button>
      <button class="btn primary" data-act="save">Сохранить</button>
    </div>`);
  modal.querySelectorAll('[data-act="close"]').forEach((b) => (b.onclick = closeModal));
  modal.querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => renderSettingsTab(btn.dataset.tab, modal);
  });
  modal.querySelector('[data-act="save"]').onclick = () => saveSettings(modal);
  renderSettingsTab(tab, modal);
}

async function renderSettingsTab(tab, modal) {
  modal.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const body = modal.querySelector("#settings-body");
  // защита от гонки: пока вкладка ждёт ответа сервера, пользователь может уйти на другую.
  // Устаревший рендер не имеет права перерисовывать тело модалки.
  const token = (modal._renderToken = (modal._renderToken || 0) + 1);
  const stale = () => modal._renderToken !== token || !body.isConnected;
  if (stale()) return;
    body.innerHTML = `<div class="typing"><span></span><span></span><span></span></div>`;
  modal.dataset.tab = tab;

  if (tab === "model") {
    let endpointInfo = state.endpoint || null;
    try {
      const data = await api("/api/endpoints");
      endpointInfo = data.endpoint || endpointInfo;
      updateEndpoint(endpointInfo);
    } catch (_) {}
    const probes = {};
    ((endpointInfo && endpointInfo.probes) || []).forEach((probe) => {
      probes[probe.url] = probe;
    });
    state.endpointProbes = probes;

    try {
      const data = await api("/api/models");
      state.models = data.models || [];
    } catch (_) {
      state.models = [];
    }

    const current = getCfg("lmstudio.model", "");
    const candidates = (endpointInfo && endpointInfo.candidates) || getCfg("lmstudio.candidates", []) || [];
    const activeUrl = (endpointInfo && endpointInfo.url) || "";
    const list = state.models.length
      ? state.models.map((m) => `
        <div class="model-item ${m.id === current ? "active" : ""}" data-model="${escapeHtml(m.id)}">
          <div style="min-width:0;flex:1">
            <div class="m-name">${escapeHtml(m.id)}</div>
            ${m.context ? `<div class="help" style="margin:2px 0 0">контекст: ${m.context}</div>` : ""}
          </div>
          ${m.id === current ? '<span class="m-check">✓</span>' : ""}
        </div>`).join("")
      : `<div class="help">${endpointInfo && !endpointInfo.ok
          ? "Серверы LM Studio недоступны — включите Local Server в LM Studio и нажмите «Проверить серверы»."
          : "На сервере не загружено ни одной модели."}</div>`;

    if (stale()) return;
        body.innerHTML = `
      <div class="field">
        <label>Сервер LM Studio</label>
        <div class="switch-row" style="padding-top:0">
          <div>
            <div class="switch-label">Автовыбор сервера</div>
            <div class="switch-help">Сначала локальная сеть, если она недоступна — этот компьютер</div>
          </div>
          <label class="switch"><input type="checkbox" id="s-auto-detect" ${(endpointInfo ? endpointInfo.auto_detect : true) ? "checked" : ""}><span class="slider"></span></label>
        </div>
        <div id="s-candidates"></div>
        <div style="display:flex;gap:8px;margin-top:9px;flex-wrap:wrap">
          <button class="btn subtle" data-act="add-candidate">+ Добавить адрес</button>
          <button class="btn subtle" id="s-check-endpoints">Проверить серверы</button>
        </div>
        <div class="endpoint-note" id="s-endpoint-note"></div>
      </div>
      <div class="field">
        <label>Доступные модели</label>
        <div class="model-list" id="s-model-list">${list}</div>
      </div>
      <div class="field">
        <label>Зрение (скриншоты модели)</label>
        <div class="switch-row" style="padding-top:0">
          <div>
            <div class="switch-label">Передавать снимки окна модели</div>
            <div class="switch-help">После каждого действия в окне агент прикладывает его снимок к запросу.
              Нужна vision-модель (Qwen2.5-VL, Gemma 3, LLaVA).</div>
          </div>
          <label class="switch"><input type="checkbox" id="s-auto-window-shot" ${getCfg("agent.auto_window_screenshot", true) ? "checked" : ""}><span class="slider"></span></label>
        </div>
        <div class="field-row" style="margin-top:10px">
          <div class="field" style="margin-bottom:0">
            <label>Режим</label>
            <select id="s-vision-mode">
              <option value="auto" ${getCfg("lmstudio.vision", "auto") === "auto" ? "selected" : ""}>auto — пробовать, иначе текст</option>
              <option value="on" ${getCfg("lmstudio.vision", "auto") === "on" ? "selected" : ""}>on — всегда передавать</option>
              <option value="off" ${getCfg("lmstudio.vision", "auto") === "off" ? "selected" : ""}>off — только текст</option>
            </select>
          </div>
          <div class="field" style="margin-bottom:0">
            <label>Размер снимка, px</label>
            <input type="number" id="s-vision-px" min="256" max="2048" step="128" value="${getCfg("lmstudio.vision_max_px", 1024)}">
          </div>
        </div>
        <div style="display:flex;gap:9px;align-items:center;margin-top:9px;flex-wrap:wrap">
          <button class="btn subtle" id="s-vision-test">Проверить зрение</button>
          <span id="s-vision-status" class="help" style="margin:0"></span>
        </div>
      </div>
      <div class="field">
        <label>Разметка и проверка результата</label>
        <div class="switch-row" style="padding-top:0">
          <div>
            <div class="switch-label">Проверять результат по снимку</div>
            <div class="switch-help">Сравнивать экран до и после действия. Если ничего не изменилось,
              агент не будет считать задачу выполненной, а попробует другой способ.</div>
          </div>
          <label class="switch"><input type="checkbox" id="s-visual-verify" ${getCfg("agent.visual_verify", true) ? "checked" : ""}><span class="slider"></span></label>
        </div>
        <div class="switch-row" style="padding-top:0">
          <div>
            <div class="switch-label">Останавливаться на капче</div>
            <div class="switch-help">Проверку «ты человек» решает только пользователь — агент не пытается её обойти.</div>
          </div>
          <label class="switch"><input type="checkbox" id="s-stop-captcha" ${getCfg("agent.stop_on_captcha", true) ? "checked" : ""}><span class="slider"></span></label>
        </div>
        <div class="field-row" style="margin-top:10px">
          <div class="field" style="margin-bottom:0">
            <label>Шаг координатной сетки, px</label>
            <input type="number" id="s-grid-step" min="0" max="500" step="25" value="${getCfg("agent.grid_step", 100)}">
            <div class="help">0 — подбирать автоматически. Сетка и метки появляются в снимках see_window.</div>
          </div>
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Температура</label>
          <input type="number" id="s-temp" step="0.05" min="0" max="2" value="${getCfg("lmstudio.temperature", 0.35)}">
        </div>
        <div class="field">
          <label>Максимум токенов ответа</label>
          <input type="number" id="s-maxtok" min="128" max="8000" value="${getCfg("lmstudio.max_tokens", 1400)}">
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Максимум шагов агента</label>
          <input type="number" id="s-maxsteps" min="1" max="20" value="${getCfg("agent.max_steps", 6)}">
          <div class="help">Сколько действий подряд может выполнить ИИ в одной задаче.</div>
        </div>
        <div class="field">
          <label>Сообщений в контексте</label>
          <input type="number" id="s-ctx" min="4" max="80" value="${getCfg("lmstudio.context_messages", 24)}">
        </div>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Автоматически называть чаты</div>
        <div class="switch-help">Модель придумает короткое название по первому сообщению</div></div>
        <label class="switch"><input type="checkbox" id="s-autotitle" ${getCfg("agent.auto_title", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>`;

    const candHost = body.querySelector("#s-candidates");

    const refreshNote = () => {
      const note = body.querySelector("#s-endpoint-note");
      if (!note) return;
      const auto = body.querySelector("#s-auto-detect").checked;
      const active = state.endpoint || {};
      if (!auto) {
        note.innerHTML = "Автовыбор выключен: используется первый адрес из списка.";
        return;
      }
      note.innerHTML = active.ok
        ? `Сейчас используется <b style="color:var(--text)">${escapeHtml(active.url)}</b> (${escapeHtml(active.kind_label || "")}), моделей: ${active.models_count || 0}.`
        : `<span style="color:var(--warn)">${escapeHtml(active.error || "Ни один сервер не отвечает.")}</span>`;
    };

    const addRow = (url) => {
      const row = document.createElement("div");
      row.className = "endpoint-row";
      row.innerHTML = `
        <span class="dot"></span>
        <input class="cand-input" type="text" spellcheck="false" placeholder="http://192.168.0.32:1234"
               value="${escapeHtml(url || "")}">
        <span class="ep-status"></span>
        <button class="icon-btn" data-act="remove-candidate" title="Убрать адрес">
          <svg viewBox="0 0 24 24" width="14" height="14"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
        </button>`;
      row.querySelector('[data-act="remove-candidate"]').onclick = () => {
        row.remove();
        refreshNote();
      };
      candHost.appendChild(row);
      paintRow(row, url);
      return row;
    };

    const paintRow = (row, url) => {
      const probe = state.endpointProbes[url] || state.endpointProbes[normalizeEp(url)];
      const dot = row.querySelector(".dot");
      const statusEl = row.querySelector(".ep-status");
      const isActive = state.endpoint && state.endpoint.url && normalizeEp(state.endpoint.url) === normalizeEp(url);
      dot.className = "dot " + (!probe ? "" : probe.ok ? "ok" : "err");
      statusEl.textContent = probe
        ? (probe.ok ? `${probe.latency_ms} мс · ${probe.models_count} мод.` : "нет ответа")
        : "не проверен";
      let badge = row.querySelector(".ep-active");
      if (isActive && !badge) {
        badge = el("span", "ep-active", "активен");
        row.insertBefore(badge, row.querySelector('[data-act="remove-candidate"]'));
      } else if (!isActive && badge) {
        badge.remove();
      }
    };

    const paintAll = () => {
      candHost.querySelectorAll(".endpoint-row").forEach((row) => {
        paintRow(row, row.querySelector(".cand-input").value.trim());
      });
      refreshNote();
    };

    candHost.innerHTML = "";
    const initial = candidates.length ? candidates : ["http://192.168.0.32:1234/v1", "http://127.0.0.1:1234/v1"];
    initial.forEach((url) => addRow(normalizeEp(url)));
    refreshNote();

    body.querySelector('[data-act="add-candidate"]').onclick = () => {
      addRow("");
      const inputs = candHost.querySelectorAll(".cand-input");
      if (inputs.length) inputs[inputs.length - 1].focus();
    };
    body.querySelector("#s-auto-detect").onchange = () => refreshNote();

    body.querySelector("#s-check-endpoints").onclick = async () => {
      const btn = body.querySelector("#s-check-endpoints");
      btn.disabled = true;
      btn.textContent = "Проверяю…";
      try {
        const data = await api("/api/endpoints?check=true");
        const info = data.endpoint || {};
        state.endpointProbes = {};
        (info.probes || []).forEach((probe) => {
          state.endpointProbes[probe.url] = probe;
        });
        updateEndpoint(info);
        paintAll();
        if (info.ok) toast(`Сервер: ${hostOf(info.url)} (${info.kind_label})`, "ok", 3500);
        else toast(info.error || "Ни один сервер не отвечает", "err", 6000);
        // перечитаем модели с найденного сервера
        try {
          const models = await api("/api/models");
          state.models = models.models || [];
          const currentModel = getCfg("lmstudio.model", "");
          const listEl = body.querySelector("#s-model-list");
          if (listEl) {
            listEl.innerHTML = state.models.length
              ? state.models.map((m) => `
                  <div class="model-item ${m.id === currentModel ? "active" : ""}" data-model="${escapeHtml(m.id)}">
                    <div style="min-width:0;flex:1"><div class="m-name">${escapeHtml(m.id)}</div></div>
                    ${m.id === currentModel ? '<span class="m-check">✓</span>' : ""}
                  </div>`).join("")
              : '<div class="help">Моделей не найдено.</div>';
            bindModelItems(body);
          }
        } catch (_) {}
        loadModels();
      } catch (err) {
        toast(err.message, "err");
      } finally {
        btn.disabled = false;
        btn.textContent = "Проверить серверы";
      }
    };

    bindModelItems(body);

    const visionStatus = body.querySelector("#s-vision-status");
    const paintVision = (data) => {
      if (!visionStatus) return;
      if (data.supported === true) {
        visionStatus.innerHTML = `<span style="color:var(--ok)">✓ модель видит изображения</span>` +
          (data.answer ? ` — «${escapeHtml(String(data.answer).slice(0, 60))}»` : "");
      } else if (data.supported === false) {
        visionStatus.innerHTML = `<span style="color:var(--warn)">✕ модель не принимает изображения</span>`;
      } else {
        visionStatus.textContent = "";
      }
    };
    if (state.vision) paintVision(state.vision);

    const visionTestBtn = body.querySelector("#s-vision-test");
    if (visionTestBtn) {
      visionTestBtn.onclick = async () => {
        visionTestBtn.disabled = true;
        visionTestBtn.textContent = "Проверяю…";
        try {
          const data = await api("/api/vision/test", { method: "POST" });
          paintVision(data);
          state.vision = Object.assign({}, state.vision || {}, {
            supported: data.supported === undefined ? null : data.supported,
            mode: getCfg("lmstudio.vision", "auto"),
            last_error: data.error || "",
          });
          updateVisionBadge();
          if (data.ok) toast("Модель видит изображения ✓", "ok", 3500);
          else toast(data.hint || data.error || "Модель не видит изображения", "warn", 7000);
        } catch (err) {
          toast(err.message, "err");
        } finally {
          visionTestBtn.disabled = false;
          visionTestBtn.textContent = "Проверить зрение";
        }
      };
    }

    // если часть адресов ещё не проверялась — до-проверим в фоне и раскрасим строки
    const needProbes = initial.some((url) => !state.endpointProbes[normalizeEp(url)]);
    if (needProbes && endpointInfo && endpointInfo.auto_detect !== false) {
      (async () => {
        try {
          const data = await api("/api/endpoints?check=true");
          const info = data.endpoint || {};
          state.endpointProbes = {};
          (info.probes || []).forEach((probe) => {
            state.endpointProbes[probe.url] = probe;
          });
          updateEndpoint(info);
          if (!stale()) paintAll();
        } catch (_) {}
      })();
    }
    return;
  }

  if (tab === "voice") {
    let voices = [];
    try {
      const data = await api("/api/voices?locale=ru");
      voices = data.voices || [];
    } catch (_) {}
    let devices = { input: [], output: [] };
    try {
      devices = await api("/api/audio/devices");
    } catch (_) {}
    const currentVoice = getCfg("voice.tts_voice", "ru-RU-DmitryNeural");
    const voiceOptions = voices.map((v) =>
      `<option value="${escapeHtml(v.id)}" ${v.id === currentVoice ? "selected" : ""}>${escapeHtml(v.name)} — ${escapeHtml(v.locale)}</option>`).join("");

    const deviceOptions = (arr, current) =>
      `<option value="">По умолчанию</option>` + arr.map((d) =>
        `<option value="${d.index}" ${String(current) === String(d.index) ? "selected" : ""}>${escapeHtml(d.name)}${d.default_input ? " (по умолч.)" : ""}</option>`).join("");

    let sttInfo = { models: [], current: getCfg("voice.whisper_model", "large-v3") };
    try {
      sttInfo = await api("/api/stt/models");
    } catch (_) {}
    const whisperModels = (sttInfo.models && sttInfo.models.length)
      ? sttInfo.models
      : [
        { id: "large-v3", size: "~3.1 ГБ", quality: "максимальная", recommended: true },
        { id: "large-v3-turbo", size: "~1.6 ГБ", quality: "почти как large-v3" },
        { id: "medium", size: "~1.5 ГБ", quality: "средняя" },
        { id: "small", size: "~460 МБ", quality: "низкая" },
        { id: "tiny", size: "~75 МБ", quality: "очень низкая" },
      ];
    const curWhisper = sttInfo.current || getCfg("voice.whisper_model", "large-v3");
    const whisperOptions = whisperModels.map((m) => {
      const id = m.id || m;
      const label = m.label || id;
      const size = m.size ? ` · ${m.size}` : "";
      const rec = m.recommended ? " ★ рекомендуется" : "";
      return `<option value="${escapeHtml(id)}" ${id === curWhisper ? "selected" : ""}>${escapeHtml(label)}${escapeHtml(size)}${rec}</option>`;
    }).join("");
    const cudaError = (sttInfo.cuda && sttInfo.cuda.error) || "";
    const deviceSetting = sttInfo.device_setting || getCfg("voice.whisper_device", "auto");
    const deviceUsed = sttInfo.device || "";
    const sttDeviceInfo = deviceUsed
      ? `Устройство: <b>${deviceUsed === "cuda" ? "видеокарта (CUDA)" : "процессор"}</b>` +
        (sttInfo.compute_type && sttInfo.compute_type !== "auto" ? ` · ${escapeHtml(sttInfo.compute_type)}` : "") +
        (deviceUsed !== "cuda" && cudaError
          ? `<br><span style="color:var(--warn)">Видеокарта не используется: ${escapeHtml(cudaError)}</span>`
          : "")
      : "Устройство: определится при первой расшифровке";
    const sttStatus = sttInfo.loading
      ? "загружаю модель…"
      : sttInfo.ready
        ? `готова: ${escapeHtml(sttInfo.loaded || sttInfo.current || "")}`
        : (sttInfo.error ? `<span style="color:var(--warn)">${escapeHtml(sttInfo.error)}</span>` : "ещё не загружена");

    const modeStart = getCfg("ui.ask_mode", true) ? "ask" : (getCfg("ui.mode", "") || "ask");

    if (stale()) return;
        body.innerHTML = `
      <div class="field">
        <label>Режим при запуске</label>
        <select id="s-mode-start">
          <option value="ask" ${modeStart === "ask" ? "selected" : ""}>спрашивать каждый раз (обычный или голосовой)</option>
          <option value="text" ${modeStart === "text" ? "selected" : ""}>сразу обычный чат</option>
          <option value="voice" ${modeStart === "voice" ? "selected" : ""}>сразу голосовой режим (сфера)</option>
        </select>
        <div class="help">Голосовой режим: квадратное окно, сфера из точек, постоянное прослушивание
          и блок статусов. Сменить режим можно кнопкой в шапке.</div>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Квадратное окно для голосового режима</div>
        <div class="switch-help">Работает при запуске в отдельном окне: <code>python run.py --window</code></div></div>
        <label class="switch"><input type="checkbox" id="s-square" ${getCfg("ui.voice_square_window", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Слушать постоянно в голосовом режиме</div>
        <div class="switch-help">Микрофон выключается на время ответа и выполнения действий</div></div>
        <label class="switch"><input type="checkbox" id="s-continuous" ${getCfg("voice.continuous_listen", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Озвучивать ответы</div>
        <div class="switch-help">Голосовые ответы: edge-tts, при недоступности — офлайн-голос Windows</div></div>
        <label class="switch"><input type="checkbox" id="s-tts" ${getCfg("voice.tts_enabled", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Озвучивать и текстовые ответы</div>
        <div class="switch-help">Иначе голос звучит только при голосовом вводе</div></div>
        <label class="switch"><input type="checkbox" id="s-tts-always" ${getCfg("voice.tts_always", false) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="field" style="margin-top:14px">
        <label>Движок озвучки</label>
        <select id="s-tts-engine">
          <option value="auto" ${getCfg("voice.tts_engine", "auto") === "auto" ? "selected" : ""}>авто — edge-tts, при сбое офлайн-голос Windows</option>
          <option value="edge-tts" ${getCfg("voice.tts_engine", "auto") === "edge-tts" ? "selected" : ""}>edge-tts — нейроголос (нужен интернет)</option>
          <option value="sapi" ${getCfg("voice.tts_engine", "auto") === "sapi" ? "selected" : ""}>офлайн-голос Windows (SAPI)</option>
          <option value="pyttsx3" ${getCfg("voice.tts_engine", "auto") === "pyttsx3" ? "selected" : ""}>pyttsx3 (если установлен)</option>
        </select>
        <div class="help" id="s-tts-engines">проверяю движки…</div>
      </div>
      <div class="field">
        <label>Голос</label>
        <select id="s-voice">${voiceOptions || `<option value="${escapeHtml(currentVoice)}">${escapeHtml(currentVoice)}</option>`}</select>
        <div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="btn subtle" id="s-test-voice">▶ Проверить озвучку</button>
          <span id="s-tts-result" class="help" style="margin:0"></span>
        </div>
      </div>
      <div class="field">
        <label>Голос Windows для офлайна (необязательно)</label>
        <input type="text" id="s-sapi-voice" value="${escapeHtml(getCfg("voice.sapi_voice", ""))}"
               placeholder="например Microsoft Irina Desktop">
        <div class="help">Пусто — системный голос Windows по умолчанию. Полный список:
          PowerShell → <code>Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | % { $_.VoiceInfo.Name }</code></div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Скорость речи</label>
          <input type="text" id="s-rate" value="${escapeHtml(getCfg("voice.tts_rate", "+8%"))}" placeholder="+8%">
        </div>
        <div class="field">
          <label>Громкость</label>
          <input type="text" id="s-volume" value="${escapeHtml(getCfg("voice.tts_volume", "+0%"))}" placeholder="+0%">
        </div>
      </div>
      <div class="field">
        <label>Модель распознавания речи (faster-whisper)</label>
        <select id="s-whisper">${whisperOptions}</select>
        <div class="help" id="s-stt-status">Статус: ${sttStatus}</div>
        <div class="help" id="s-stt-device-info">${sttDeviceInfo}</div>
        <div class="help"><b>large-v3</b> — самая точная: меньше путаницы в словах и командах
          (~3 ГБ, скачается один раз; на CPU расшифровка 5–10 секунд речи занимает несколько секунд).
          «small» и «tiny» заметно быстрее, но часто путают слова — их лучше не выбирать.</div>
        <div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="btn subtle" id="s-stt-apply">⟳ Скачать и применить модель</button>
          <span class="help" id="s-stt-apply-result" style="margin:0"></span>
        </div>
        <div class="help" id="s-stt-progress"></div>
      </div>
      <div class="field">
        <label>Устройство распознавания</label>
        <select id="s-stt-device">
          <option value="auto" ${deviceSetting === "auto" ? "selected" : ""}>авто — видеокарта, если CUDA настроена, иначе процессор</option>
          <option value="cpu" ${deviceSetting === "cpu" ? "selected" : ""}>процессор — работает всегда, медленнее</option>
          <option value="cuda" ${deviceSetting === "cuda" ? "selected" : ""}>видеокарта (CUDA 12 + cuDNN 9)</option>
        </select>
        <div class="help">Ошибка <code>cublas64_12.dll is not found</code> значит, что видеокарта есть,
          а библиотек CUDA 12 нет: Astra сама переходит на процессор и продолжает работать.
          Библиотеки CUDA 12 можно поставить одним нажатием — <b>CUDA 13 при этом не мешает</b>,
          движок распознавания требует именно 12-ю версию, и Astra сама подключит её.
          «Процессор» — всегда стабильный вариант.</div>
        <div style="margin-top:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <button class="btn subtle" id="s-cuda-check">🔍 Проверить видеокарту</button>
          <button class="btn subtle" id="s-cuda-install">⬇ Установить CUDA 12 (cuBLAS + cuDNN)</button>
        </div>
        <div class="help" id="s-cuda-info"></div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Микрофон</label>
          <select id="s-input-device">${deviceOptions(devices.input || [], getCfg("voice.input_device", ""))}</select>
        </div>
        <div class="field">
          <label>Устройство вывода звука</label>
          <select id="s-output-device">${deviceOptions(devices.output || [], getCfg("voice.output_device", ""))}</select>
        </div>
      </div>
      <div class="field-row">
        <div class="field">
          <label>Тишина до остановки записи (мс)</label>
          <input type="number" id="s-silence" min="300" max="5000" step="100" value="${getCfg("voice.silence_ms", 1100)}">
        </div>
        <div class="field">
          <label>Максимум записи (сек)</label>
          <input type="number" id="s-maxrec" min="5" max="120" value="${getCfg("voice.max_record_s", 30)}">
        </div>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Отправлять распознанное сразу</div>
        <div class="switch-help">Выключи, если хочешь сначала поправить текст в поле ввода</div></div>
        <label class="switch"><input type="checkbox" id="s-autosend" ${getCfg("voice.auto_send_voice", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Включать микрофон при запуске</div></div>
        <label class="switch"><input type="checkbox" id="s-listen-start" ${getCfg("voice.listen_on_start", false) ? "checked" : ""}><span class="slider"></span></label>
      </div>`;

    const enginesInfo = body.querySelector("#s-tts-engines");
    const resultInfo = body.querySelector("#s-tts-result");
    const paintTts = (data) => {
      const engines = (data && data.engines) || {};
      const order = [["edge-tts", "edge-tts"], ["sapi", "офлайн Windows"], ["pyttsx3", "pyttsx3"]];
      if (enginesInfo) {
        enginesInfo.textContent = order
          .map(([k, label]) => (engines[k] ? "✓ " : "— ") + label)
          .join("   ");
      }
      const problems = [];
      if (data && data.enabled === false) problems.push("озвучка выключена в настройках");
      if (data && data.any_available === false) problems.push("не найден ни один движок озвучки");
      if (data && data.audio_output_ok === false) problems.push("нет доступа к устройству вывода звука");
      if (resultInfo && problems.length) {
        resultInfo.innerHTML = `<span style="color:var(--warn)">${escapeHtml(problems.join("; "))}</span>`;
      }
    };
    (async () => {
      try { paintTts(await api("/api/tts/status")); } catch (_) {}
    })();

    /* ---- видеокарта: диагностика и установка CUDA 12 ---- */
    const cudaInfo = body.querySelector("#s-cuda-info");
    const cudaCheck = body.querySelector("#s-cuda-check");
    const cudaInstall = body.querySelector("#s-cuda-install");

    const paintCuda = (data) => {
      if (!cudaInfo || !data) return;
      if (data.usable) {
        cudaInfo.innerHTML = `<span style="color:var(--ok)">✓ видеокарта доступна` +
          (data.device ? ` (движок: ${escapeHtml(data.device)})` : "") + `</span>`;
        return;
      }
      const parts = [`<span style="color:var(--warn)">Видеокарта сейчас не используется.</span>`];
      if (data.cublas13 && !data.cublas12) {
        parts.push(`Найдена CUDA 13 (${escapeHtml(data.cublas13.split(/[\\/]/).pop())}), ` +
          `а движку нужна CUDA 12 — это разные библиотеки.`);
      } else if (!data.cublas12) {
        parts.push("Библиотеки CUDA 12 (cuBLAS/cuDNN) не найдены.");
      }
      if (data.error) parts.push(`<code>${escapeHtml(data.error)}</code>`);
      if (data.install_running) {
        parts.push(`<b>Устанавливаю…</b> это займёт несколько минут`);
      } else if (data.install_ok === false && data.install_output) {
        parts.push(`<span style="color:var(--err)">pip вернул ошибку:</span>` +
          `<pre style="white-space:pre-wrap;font-size:11px;max-height:120px;overflow:auto">` +
          `${escapeHtml(data.install_output)}</pre>`);
      }
      cudaInfo.innerHTML = parts.join("<br>");
    };

    const cudaState = async () => {
      try {
        paintCuda(await api("/api/stt/cuda"));
      } catch (_) {}
    };
    cudaState();
    if (cudaCheck) cudaCheck.onclick = async () => {
      cudaCheck.disabled = true;
      cudaCheck.textContent = "Проверяю…";
      try {
        const data = await api("/api/stt/cuda/recheck", { method: "POST" });
        paintCuda(data);
        toast(data.usable ? "Видеокарта доступна — распознавание пойдёт на GPU"
          : "Видеокарта недоступна: распознавание работает на процессоре",
          data.usable ? "ok" : "info", 5000);
      } catch (err) {
        toast(err.message, "err");
      } finally {
        cudaCheck.disabled = false;
        cudaCheck.textContent = "🔍 Проверить видеокарту";
      }
    };
    if (cudaInstall) cudaInstall.onclick = async () => {
      if (!confirm("Скачать библиотеки CUDA 12 для видеокарты?\n\n" +
                   "Будут установлены пакеты nvidia-cublas-cu12 и nvidia-cudnn-cu12 (~1 ГБ).\n" +
                   "Установленная CUDA 13 не пострадает — они живут рядом, внутри Python-окружения.")) return;
      cudaInstall.disabled = true;
      cudaInstall.textContent = "Устанавливаю…";
      try {
        await api("/api/stt/cuda/install", { method: "POST" });
        toast("Пошла установка. Это несколько минут — прогресс виден здесь же.", "info", 8000);
        const started = Date.now();
        const timer = setInterval(async () => {
          try {
            const data = await api("/api/stt/cuda");
            paintCuda(data);
            if (!data.install_running) {
              clearInterval(timer);
              cudaInstall.disabled = false;
              cudaInstall.textContent = "⬇ Установить CUDA 12 (cuBLAS + cuDNN)";
              if (data.usable) toast("Готово: распознавание пойдёт на видеокарте", "ok", 6000);
            } else if (Date.now() - started > 1800000) {
              clearInterval(timer);
              cudaInstall.disabled = false;
              cudaInstall.textContent = "⬇ Установить CUDA 12 (cuBLAS + cuDNN)";
            }
          } catch (_) {}
        }, 4000);
      } catch (err) {
        cudaInstall.disabled = false;
        cudaInstall.textContent = "⬇ Установить CUDA 12 (cuBLAS + cuDNN)";
        toast(err.message, "err");
      }
    };

    const sttApply = body.querySelector("#s-stt-apply");
    const sttResult = body.querySelector("#s-stt-apply-result");
    const sttProgress = body.querySelector("#s-stt-progress");
    if (sttApply) sttApply.onclick = async () => {
      const select = body.querySelector("#s-whisper");
      const model = select ? select.value : "";
      if (!model) return;
      sttApply.disabled = true;
      sttApply.textContent = "Скачиваю…";
      if (sttResult) sttResult.textContent = "";
      if (sttProgress) {
        sttProgress.innerHTML = `Модель <b>${escapeHtml(model)}</b> скачивается и загружается — ` +
          "это может занять несколько минут (large-v3 ~3 ГБ). Прогресс виден в консоли сервера.";
      }
      try {
        await api("/api/stt/model", { method: "POST", body: { model } });
        state.config.voice = Object.assign({}, state.config.voice || {}, { whisper_model: model });
        const started = Date.now();
        const timer = setInterval(async () => {
          try {
            const info = await api("/api/stt/models");
            state.sttCurrent = info.current || model;
            state.sttReady = !!info.ready;
            fillSttSelect(info);
            if (info.ready && info.loaded === model) {
              clearInterval(timer);
              sttApply.disabled = false;
              sttApply.textContent = "⟳ Скачать и применить модель";
              if (sttResult) sttResult.innerHTML = `<span style="color:var(--ok)">✓ модель ${escapeHtml(model)} загружена</span>`;
              if (sttProgress) sttProgress.textContent = "";
            } else if (info.error) {
              clearInterval(timer);
              sttApply.disabled = false;
              sttApply.textContent = "⟳ Скачать и применить модель";
              if (sttResult) sttResult.innerHTML = `<span style="color:var(--err)">✕ ${escapeHtml(info.error)}</span>`;
              if (sttProgress) sttProgress.textContent = "";
            } else if (Date.now() - started > 900000) {
              clearInterval(timer);
              sttApply.disabled = false;
              sttApply.textContent = "⟳ Скачать и применить модель";
              if (sttProgress) sttProgress.textContent = "Загрузка идёт дольше 15 минут — проверьте консоль сервера.";
            }
          } catch (_) {}
        }, 2500);
      } catch (err) {
        sttApply.disabled = false;
        sttApply.textContent = "⟳ Скачать и применить модель";
        if (sttResult) sttResult.innerHTML = `<span style="color:var(--err)">✕ ${escapeHtml(err.message)}</span>`;
      }
    };

    const testBtn = body.querySelector("#s-test-voice");
    testBtn.onclick = async () => {
      testBtn.disabled = true;
      testBtn.textContent = "Проверяю…";
      if (resultInfo) resultInfo.textContent = "";
      try {
        const data = await api("/api/tts/test", { method: "POST" });
        paintTts(data.status || {});
        // бейдж в шапке тоже должен показать, каким движком озвучили
        state.tts = Object.assign({}, state.tts || {}, data.status || {}, {
          ok: !!data.ok,
          engine: data.engine || "",
          label: data.label || "",
          error: data.ok ? "" : (data.error || ""),
        });
        if (data.ok && data.engine) state.tts.last_engine = data.engine;
        updateTtsBadge();
        if (data.ok) {
          if (resultInfo) {
            resultInfo.innerHTML = `<span style="color:var(--ok)">✓ озвучено: ${escapeHtml(data.label || data.engine || "")}</span>` +
              (data.fallback_from
                ? ` <span style="color:var(--muted-2)">(${escapeHtml(data.fallback_from)} недоступен)</span>`
                : "");
          }
        } else if (resultInfo) {
          resultInfo.innerHTML = `<span style="color:var(--err)">✕ ${escapeHtml(data.hint || data.error || "не удалось озвучить")}</span>`;
          console.warn("[tts]", data.error);
        }
      } catch (err) {
        toast(err.message, "err");
      } finally {
        testBtn.disabled = false;
        testBtn.textContent = "▶ Проверить озвучку";
      }
    };
    return;
  }

  if (tab === "safety") {
    let tools = [];
    let allowed = [];
    try {
      const data = await api("/api/tools");
      tools = data.tools || [];
      allowed = data.allowed_without_confirmation || [];
    } catch (_) {}
    const groups = {};
    tools.forEach((t) => {
      groups[t.category] = groups[t.category] || [];
      groups[t.category].push(t);
    });
    const catTitles = { apps: "Программы", input: "Клавиатура и мышь", system: "Система", files: "Файлы", shell: "Командная строка", web: "Интернет" };
    const toolsHtml = Object.entries(groups).map(([cat, list]) => `
      <div class="field">
        <label>${escapeHtml(catTitles[cat] || cat)}</label>
        <div class="tool-list">
          ${list.map((t) => `
            <div class="tool-item">
              <div style="min-width:0;flex:1">
                <div class="t-name">${escapeHtml(t.signature)}</div>
                <div class="t-desc">${escapeHtml(t.summary)}</div>
              </div>
              <span class="badge ${t.danger === "confirm" ? "confirm" : "safe"}">${t.danger === "confirm" ? "спрашивать" : "свободно"}</span>
              ${t.danger === "confirm" ? `<label class="switch" title="Не спрашивать разрешение"><input type="checkbox" data-tool="${escapeHtml(t.name)}" ${allowed.includes(t.name) ? "checked" : ""}><span class="slider"></span></label>` : ""}
            </div>`).join("")}
        </div>
      </div>`).join("");

    if (stale()) return;
        body.innerHTML = `
      <div class="switch-row">
        <div><div class="switch-label">Подтверждать команды в консоли</div>
        <div class="switch-help">PowerShell/bash — потенциально опасно</div></div>
        <label class="switch"><input type="checkbox" id="s-confirm-shell" ${getCfg("safety.confirm_shell", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Подтверждать выключение и блокировку</div></div>
        <label class="switch"><input type="checkbox" id="s-confirm-power" ${getCfg("safety.confirm_power", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div><div class="switch-label">Подтверждать запись и удаление файлов</div></div>
        <label class="switch"><input type="checkbox" id="s-confirm-files" ${getCfg("safety.confirm_files", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="switch-row" style="margin-bottom:14px">
        <div><div class="switch-label">Подтверждать ввод текста и клики</div>
        <div class="switch-help">ИИ может печатать в активном окне</div></div>
        <label class="switch"><input type="checkbox" id="s-confirm-typing" ${getCfg("safety.confirm_typing", true) ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="help" style="margin-bottom:10px">Отдельные инструменты без подтверждения (переключатели справа):</div>
      ${toolsHtml}`;

    body.querySelectorAll("[data-tool]").forEach((chk) => {
      chk.onchange = async () => {
        try {
          await api("/api/tools/permission", {
            method: "POST",
            body: { name: chk.dataset.tool, allowed: chk.checked },
          });
        } catch (err) {
          toast(err.message, "err");
        }
      };
    });
    return;
  }

  // about
  let health = {};
  try {
    health = await api("/api/health");
  } catch (_) {}
  let system = {};
  try {
    const data = await api("/api/system");
    system = data.info || {};
  } catch (_) {}
  if (stale()) return;
    body.innerHTML = `
    <div class="field">
      <label>Состояние</label>
      <div class="tool-list">
        <div class="tool-item"><div style="flex:1"><div class="t-name">LM Studio</div>
          <div class="t-desc">${escapeHtml(getCfg("lmstudio.base_url", ""))}</div></div>
          <span class="badge ${health.model ? "safe" : "confirm"}">${health.model ? escapeHtml(health.model) : "модель не выбрана"}</span></div>
        <div class="tool-item"><div style="flex:1"><div class="t-name">Распознавание речи</div>
          <div class="t-desc">faster-whisper · ${escapeHtml(String(health.stt?.model || ""))}</div></div>
          <span class="badge ${health.stt?.ready ? "safe" : "confirm"}">${health.stt?.ready ? "загружено" : "не загружено"}</span></div>
        <div class="tool-item"><div style="flex:1"><div class="t-name">Звук (микрофон/динамики)</div>
          <div class="t-desc">${escapeHtml(health.audio?.error || "sounddevice в порядке")}</div></div>
          <span class="badge ${health.audio?.ok ? "safe" : "confirm"}">${health.audio?.ok ? "ок" : "нет доступа"}</span></div>
      </div>
    </div>
    <div class="field">
      <label>Система</label>
      <div class="code-args">${escapeHtml(JSON.stringify(system, null, 2))}</div>
    </div>
    <div class="field">
      <label>Подсказки</label>
      <div class="help">
        • Модель должна поддерживать инструкции (Qwen2.5-Instruct, Llama-3.1-Instruct, Mistral-Instruct и т.п.).<br>
        • Для стабильного JSON-планирования лучше модели от 7B.<br>
        • В LM Studio включите «Developer → Local Server» и загрузите модель.<br>
        • App версия ${escapeHtml(health.version || "")}
      </div>
    </div>`;
}

async function saveSettings(modal) {
  const tab = modal.dataset.tab;
  const previousStt = getCfg("voice.whisper_model", "large-v3");
  const previousDevice = getCfg("voice.whisper_device", "auto");
  const body = modal.querySelector("#settings-body");
  const patch = {};
  const num = (id, fallback) => {
    const node = body.querySelector(id);
    if (!node) return fallback;
    const v = Number(node.value);
    return Number.isFinite(v) ? v : fallback;
  };
  const val = (id) => {
    const node = body.querySelector(id);
    return node ? node.value : undefined;
  };
  const chk = (id) => {
    const node = body.querySelector(id);
    return node ? node.checked : undefined;
  };

  if (tab === "model") {
    const candidates = [];
    $$("#s-candidates .cand-input", body).forEach((input) => {
      const url = normalizeEp(input.value);
      if (url && !candidates.includes(url)) candidates.push(url);
    });
    const autoDetect = chk("#s-auto-detect");
    patch.lmstudio = {
      vision: val("#s-vision-mode") || "auto",
      vision_max_px: num("#s-vision-px", 1024),
      temperature: num("#s-temp", 0.35),
      max_tokens: num("#s-maxtok", 1400),
      context_messages: num("#s-ctx", 24),
      auto_detect: autoDetect !== false,
      candidates: candidates.length ? candidates : ["http://127.0.0.1:1234/v1"],
    };
    if (!autoDetect) patch.lmstudio.base_url = patch.lmstudio.candidates[0];
    patch.agent = {
      max_steps: num("#s-maxsteps", 6),
      auto_title: chk("#s-autotitle"),
      auto_window_screenshot: chk("#s-auto-window-shot"),
      visual_verify: chk("#s-visual-verify"),
      stop_on_captcha: chk("#s-stop-captcha"),
      grid_step: num("#s-grid-step", 100),
    };
  } else if (tab === "voice") {
    const modeStart = val("#s-mode-start");
    patch.ui = {
      ask_mode: modeStart === "ask",
      mode: modeStart === "ask" ? "" : modeStart,
      voice_square_window: chk("#s-square"),
    };
    patch.voice = {
      tts_enabled: chk("#s-tts"),
      tts_always: chk("#s-tts-always"),
      tts_voice: val("#s-voice"),
      tts_engine: val("#s-tts-engine") || "auto",
      sapi_voice: val("#s-sapi-voice") || "",
      tts_rate: val("#s-rate"),
      tts_volume: val("#s-volume"),
      whisper_model: val("#s-whisper"),
      whisper_device: val("#s-stt-device") || "auto",
      input_device: val("#s-input-device"),
      output_device: val("#s-output-device"),
      silence_ms: num("#s-silence", 1100),
      max_record_s: num("#s-maxrec", 30),
      auto_send_voice: chk("#s-autosend"),
      listen_on_start: chk("#s-listen-start"),
      continuous_listen: chk("#s-continuous"),
    };
  } else if (tab === "safety") {
    patch.safety = {
      confirm_shell: chk("#s-confirm-shell"),
      confirm_power: chk("#s-confirm-power"),
      confirm_files: chk("#s-confirm-files"),
      confirm_typing: chk("#s-confirm-typing"),
    };
  } else {
    closeModal();
    return;
  }
  try {
    const data = await api("/api/config", { method: "POST", body: { patch } });
    state.config = data.config;
    state.ttsEnabled = !!getCfg("voice.tts_enabled");
    applyConfigToUi();
    toast("Настройки сохранены", "ok", 1800);
    if (tab === "voice" && patch.voice.whisper_model && patch.voice.whisper_model !== previousStt) {
      api("/api/stt/model", { method: "POST", body: { model: patch.voice.whisper_model } }).catch(() => {});
    } else if (tab === "voice" && patch.voice.whisper_device !== previousDevice) {
      // сменили устройство — перезагружаем модель (и заново проверяем CUDA)
      toast("Перезагружаю распознавание речи…", "info", 2000);
      api("/api/stt/reload", { method: "POST" }).catch(() => {});
    }
    closeModal();
    if (tab === "model") {
      try {
        const data = await api("/api/endpoints?check=true");
        updateEndpoint(data.endpoint);
        loadModels();
      } catch (_) {}
    }
    refreshHealth();
  } catch (err) {
    toast(`Не удалось сохранить: ${err.message}`, "err");
  }
}

/* ============================== модели и здоровье ============================== */
async function loadModels() {
  const select = $("#model-select");
  try {
    const data = await api("/api/models");
    state.models = data.models || [];
    if (data.endpoint) updateEndpoint(data.endpoint);
    const current = data.current || getCfg("lmstudio.model", "");
    select.innerHTML = "";
    if (!state.models.length) {
      select.innerHTML = current
        ? `<option value="${escapeHtml(current)}">${escapeHtml(current)}</option>`
        : `<option value="">LM Studio не найден</option>`;
      const where = ((data.endpoint && data.endpoint.candidates) || getCfg("lmstudio.candidates", []))
        .map((u) => hostOf(u)).join(" и ");
      setBanner(
        data.error
          ? `${data.error}`
          : `LM Studio не отвечает${where ? " (" + where + ")" : ""}. Запустите LM Studio → Developer → Local Server.`,
        "warn"
      );
    } else {
      for (const m of state.models) {
        const option = el("option");
        option.value = m.id;
        option.textContent = m.id;
        if (m.id === current) option.selected = true;
        select.appendChild(option);
      }
      if (!current && state.models[0]) {
        select.value = state.models[0].id;
      }
      setBanner("");
    }
    setStatusModel(current || (state.models[0] && state.models[0].id) || "");
  } catch (err) {
    setBanner(`Не удалось получить модели: ${err.message}`, "err");
  }
}

$("#model-select")?.addEventListener("change", async (e) => {
  const model = e.target.value;
  if (!model) return;
  try {
    await api("/api/config", { method: "POST", body: { patch: { lmstudio: { model } } } });
    state.config.lmstudio = Object.assign({}, state.config.lmstudio, { model });
    setStatusModel(model);
    toast(`Модель: ${model}`, "ok", 2200);
    refreshHealth();
  } catch (err) {
    toast(err.message, "err");
  }
});

async function refreshHealth() {
  try {
    const health = await api("/api/health");
    state.health = health;
    if (health.endpoint) updateEndpoint(health.endpoint);
    if (health.tts) {
      state.tts = Object.assign({}, state.tts || {}, health.tts);
      updateTtsBadge();
    }
    if (health.vision) {
      state.vision = Object.assign({ supported: null }, health.vision);
      updateVisionBadge();
    }
    const dot = $("#health-dot");
    const text = $("#health-text");
    if (health.model && state.connected) {
      dot.className = "dot ok";
      text.textContent = health.model.length > 26 ? health.model.slice(0, 26) + "…" : health.model;
    } else if (state.connected) {
      dot.className = "dot warn";
      text.textContent = health.endpoint && !health.endpoint.ok ? "нет LM Studio" : "выберите модель";
    } else {
      dot.className = "dot err";
      text.textContent = "нет связи";
    }
    if (health.audio && health.audio.ok === false) {
      console.warn("audio:", health.audio.error);
    }
  } catch (_) {
    $("#health-dot").className = "dot err";
    $("#health-text").textContent = "нет связи";
  }
}

function setBanner(message, level = "warn") {
  const banner = $("#banner");
  if (!message) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  banner.className = `banner ${level === "err" ? "err" : ""}`;
  banner.innerHTML = `<span>${escapeHtml(message)}</span>
    <button class="btn subtle" id="banner-settings">Настройки</button>`;
  banner.classList.remove("hidden");
  const btn = $("#banner-settings");
  if (btn) btn.onclick = () => openSettings("model");
}

/* ============================== события UI ============================== */
function bindUi() {
  $("#send-btn").onclick = sendMessage;
  $("#mic-btn").onclick = toggleMic;
  $("#stop-btn").onclick = () => {
    if (state.recording) send({ type: "record_cancel" });
    else send({ type: "cancel" });
  };
  $("#orb-stop").onclick = () => {
    if (state.appState === "listening") send({ type: "record_cancel" });
    else send({ type: "stop_speech" });
  };
  $("#new-chat").onclick = newChat;
  $("#open-settings").onclick = () => openSettings("model");
  $("#refresh-models").onclick = async () => {
    toast("Проверяю серверы LM Studio…", "info", 1500);
    try {
      const data = await api("/api/endpoints?check=true");
      updateEndpoint(data.endpoint);
      if (data.endpoint && data.endpoint.ok) setBanner("");
    } catch (_) {}
    loadModels();
  };
  const endpointBadge = $("#endpoint-badge");
  if (endpointBadge) endpointBadge.onclick = () => openSettings("model");
  $("#tts-toggle").onclick = toggleTts;
  $("#switch-to-voice").onclick = () => switchMode("voice");

  /* ---- экран выбора режима ---- */
  const cardText = $("#mode-card-text");
  if (cardText) cardText.onclick = () => chooseMode("text");
  const cardVoice = $("#mode-card-voice");
  if (cardVoice) cardVoice.onclick = () => chooseMode("voice");
  document.querySelectorAll("#mode-screen .mode-card").forEach((card) => {
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        chooseMode(card.dataset.mode);
      }
    });
  });

  /* ---- голосовой экран ---- */
  const voiceToText = $("#voice-to-text");
  if (voiceToText) voiceToText.onclick = () => switchMode("text");
  const voiceSettings = $("#voice-settings");
  if (voiceSettings) voiceSettings.onclick = () => openSettings("voice");
  const voiceMic = $("#voice-mic");
  if (voiceMic) voiceMic.onclick = () => {
    if (state.recording) send({ type: "record_cancel" });
    else send({ type: "record_start" });
  };
  const voiceStop = $("#voice-stop");
  if (voiceStop) voiceStop.onclick = () => {
    if (state.recording) send({ type: "record_cancel" });
    else send({ type: "cancel" });
  };
  const voiceStt = $("#voice-stt-model");
  if (voiceStt) voiceStt.onchange = () => changeSttModel(voiceStt.value);
  const voiceSttReload = $("#voice-stt-reload");
  if (voiceSttReload) voiceSttReload.onclick = async () => {
    toast("Перезагружаю модель распознавания…", "info", 1800);
    try {
      const data = await api("/api/stt/reload", { method: "POST" });
      if (!data.ok) toast(data.error || "Не удалось загрузить модель", "err", 8000);
    } catch (err) {
      toast(err.message, "err");
    }
  };
  const voiceInput = $("#voice-text-input");
  if (voiceInput) voiceInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      sendVoiceText();
    }
  });
  $("#export-chat").onclick = () => {
    if (state.session) window.open(`/api/sessions/${state.session.id}/export`, "_blank");
    else toast("Пока нечего экспортировать", "warn");
  };
  $("#health-badge").onclick = () => openSettings("about");
  $("#sidebar-collapse").onclick = () => document.body.classList.toggle("sidebar-collapsed");
  $("#sidebar-open").onclick = () => document.body.classList.add("sidebar-open");

  document.addEventListener("click", (e) => {
    if (document.body.classList.contains("sidebar-open") && !e.target.closest("#sidebar") && !e.target.closest("#sidebar-open")) {
      document.body.classList.remove("sidebar-open");
    }
  });

  const input = $("#input");
  input.addEventListener("input", autoGrow);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  let searchTimer = null;
  $("#session-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadSessions, 220);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ($("#modal-root").children.length) {
        closeModal();
        return;
      }
      if (state.appState === "listening") {
        send({ type: "record_cancel" });
        return;
      }
      if (state.busy) {
        send({ type: "cancel" });
        return;
      }
    }
    if (e.code === "Space" && e.ctrlKey) {
      e.preventDefault();
      toggleMic();
    }
    if (e.key === "n" && (e.ctrlKey || e.metaKey) && e.shiftKey) {
      e.preventDefault();
      newChat();
    }
  });

  const container = $("#messages");
  const scrollBtn = $("#scroll-bottom");
  container.addEventListener("scroll", () => {
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    state.stickToBottom = distance < 120;
    scrollBtn.classList.toggle("hidden", state.stickToBottom);
    if (state.stickToBottom) {
      const badge = $("#scroll-badge");
      badge.classList.add("hidden");
      state.unseen = 0;
    }
  });
  scrollBtn.onclick = () => {
    state.stickToBottom = true;
    container.scrollTo({ top: container.scrollHeight, behavior: "smooth" });
    const badge = $("#scroll-badge");
    badge.classList.add("hidden");
    state.unseen = 0;
  };
  makeTitleEditable();
}

/* ============================== запуск ============================== */
async function boot() {
  bindUi();
  state.orb = new Orb($("#orb"));
  const welcomeOrbEl = $("#welcome-orb");
  if (welcomeOrbEl) new Orb(welcomeOrbEl).setMode("idle");

  try {
    const data = await api("/api/config");
    state.config = data.config || {};
  } catch (_) {}
  state.ttsEnabled = !!getCfg("voice.tts_enabled", true);
  applyConfigToUi();

  connect();
  await loadSessions();
  await loadModels();
  refreshHealth();
  await initMode();
  setInterval(refreshHealth, 30000);
  setInterval(() => {
    if (state.connected) send({ type: "ping" });
  }, 25000);
  setAppState("idle");
  $("#input").focus();
}

document.addEventListener("DOMContentLoaded", boot);
