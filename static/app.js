const logEl      = document.getElementById("log");
const messagesEl = document.getElementById("messages");
const formEl     = document.getElementById("f");
const inputEl    = document.getElementById("q");
const sendEl     = document.getElementById("send");
const modeEl     = document.getElementById("mode");
const listEl     = document.getElementById("sessions");
const titleEl    = document.getElementById("chat-title");
const newChatEl  = document.getElementById("new-chat");

const CLIENT_KEY = "liara_client_id";
const SESSION_KEY = "liara_active_session";
const CLIENT_ID = localStorage.getItem(CLIENT_KEY) || crypto.randomUUID();
localStorage.setItem(CLIENT_KEY, CLIENT_ID);

let activeSessionId = localStorage.getItem(SESSION_KEY);
let sessionItems = [];
let busy = false;
let draftSession = false;

/* ------------------------------------------------------------ API */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let message = "خطا در دریافت اطلاعات";
    try {
      const body = await res.json();
      message = body.detail || body.message || message;
    } catch (_) {}
    throw new Error(message);
  }
  return res.status === 204 ? null : res.json();
}

async function createSession() {
  // تا وقتی گفتگوی فعلی خالی است، همان را استفاده می‌کنیم.
  if (currentSessionIsEmpty()) {
    closeSidebar();
    inputEl.focus();
    return;
  }
  // پیش‌نویس محلی است و تا اولین پیام وارد دیتابیس و تاریخچه نمی‌شود.
  activeSessionId = crypto.randomUUID();
  draftSession = true;
  localStorage.setItem(SESSION_KEY, activeSessionId);
  clearMessages();
  titleEl.textContent = "گفتگوی جدید";
  renderSessionList();
  syncNewChatButton();
  closeSidebar();
  inputEl.focus();
}

async function refreshSessions() {
  const data = await api("/api/sessions?client_id=" + encodeURIComponent(CLIENT_ID));
  // سشن‌های بدون پیام جزو تاریخچه محسوب نمی‌شوند.
  sessionItems = (data.items || []).filter(item => item.message_count > 0);
  if (sessionItems.some(item => item.id === activeSessionId)) draftSession = false;
  renderSessionList();
  syncNewChatButton();
}

function currentSessionIsEmpty() {
  if (draftSession) return true;
  const current = sessionItems.find(item => item.id === activeSessionId);
  return Boolean(current && current.message_count === 0);
}

function syncNewChatButton() {
  const empty = currentSessionIsEmpty();
  newChatEl.disabled = busy || empty;
  newChatEl.title = empty
    ? "ابتدا در همین گفتگوی جدید یک پیام بفرستید"
    : "ساخت گفتگوی جدید";
}

async function openSession(id) {
  if (busy || id === activeSessionId) {
    closeSidebar();
    return;
  }
  const data = await api(
    "/api/sessions/" + encodeURIComponent(id) +
    "?client_id=" + encodeURIComponent(CLIENT_ID)
  );
  activeSessionId = id;
  draftSession = false;
  localStorage.setItem(SESSION_KEY, id);
  titleEl.textContent = data.title;
  clearMessages(false);
  for (const message of data.messages) {
    const ui = addMessage(message.role === "user" ? "شما" : "دستیار",
                          message.role === "user" ? "user" : "");
    if (message.role === "assistant") {
      ui.bubble.innerHTML = renderMarkdown(message.content);
      renderSources(ui.sources, message.sources);
    } else {
      ui.bubble.textContent = message.content;
    }
  }
  if (!data.messages.length) showEmpty();
  renderSessionList();
  syncNewChatButton();
  scrollDown();
  closeSidebar();
  inputEl.focus();
}

async function renameSession(id) {
  const item = sessionItems.find(s => s.id === id);
  const title = prompt("نام گفتگو", item?.title || "");
  if (!title || !title.trim()) return;
  await api("/api/sessions/" + encodeURIComponent(id), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: CLIENT_ID, title: title.trim() }),
  });
  if (id === activeSessionId) titleEl.textContent = title.trim();
  await refreshSessions();
}

async function deleteSession(id) {
  if (!confirm("این گفتگو حذف شود؟ این کار قابل بازگشت نیست.")) return;
  await api(
    "/api/sessions/" + encodeURIComponent(id) +
    "?client_id=" + encodeURIComponent(CLIENT_ID),
    { method: "DELETE" }
  );
  if (id === activeSessionId) {
    activeSessionId = null;
    draftSession = false;
    localStorage.removeItem(SESSION_KEY);
    await createSession();
  } else {
    await refreshSessions();
  }
}

/* ------------------------------------------------------------ history UI */

function renderSessionList() {
  if (!sessionItems.length) {
    listEl.innerHTML = '<div class="history-label">هنوز گفتگویی ندارید.</div>';
    return;
  }
  listEl.innerHTML = sessionItems.map(item => {
    const active = item.id === activeSessionId ? " active" : "";
    const date = new Date(item.updated_at * 1000).toLocaleDateString("fa-IR", {
      month: "short", day: "numeric",
    });
    return (
      '<div class="session' + active + '" data-id="' + escapeHtml(item.id) + '">' +
        '<button class="session-main" data-action="open">' +
          '<span class="session-title">' + escapeHtml(item.title) + "</span>" +
          '<span class="session-time">' + escapeHtml(date) +
            " · " + Number(item.message_count).toLocaleString("fa-IR") + " پیام</span>" +
        "</button>" +
        '<span class="session-actions">' +
          '<button class="icon-btn" data-action="rename" title="تغییر نام">✎</button>' +
          '<button class="icon-btn danger" data-action="delete" title="حذف">×</button>' +
        "</span>" +
      "</div>"
    );
  }).join("");
}

function openSidebar() { document.body.classList.add("sidebar-open"); }
function closeSidebar() { document.body.classList.remove("sidebar-open"); }

function showEmpty() {
  messagesEl.innerHTML =
    '<div class="empty">' +
      "<h2>چطور می‌تونم کمکت کنم؟</h2>" +
      "<div>از مستندات رسمی لیارا جواب می‌دم و منبع دقیق هم نشون می‌دم.</div>" +
      '<div class="hints">' +
        '<button class="hint">نسخه پایتون رو کجا تعیین کنم؟</button>' +
        '<button class="hint">می‌خوام جنگو دیپلوی کنم</button>' +
        '<button class="hint">خطا: could not install packages</button>' +
      "</div>" +
    "</div>";
}

function clearMessages(showWelcome = true) {
  messagesEl.innerHTML = "";
  if (showWelcome) showEmpty();
}

/* ------------------------------------------------------------ SSE */

async function* streamChat(message, signal) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: activeSessionId,
      client_id: CLIENT_ID,
    }),
    signal,
  });
  if (!res.ok) {
    let msg = "خطای سرور";
    try { msg = (await res.json()).message || msg; } catch (_) {}
    yield { event: "error", data: { message: msg, code: String(res.status) } };
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let sep;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message", data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!data) continue;
      try { yield { event, data: JSON.parse(data) }; }
      catch (_) { console.warn("رویداد نامعتبر:", raw); }
    }
  }
}

/* ------------------------------------------------------------ message rendering */

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const BLOCK_OPEN = "⸢BLK";
const BLOCK_CLOSE = "⸣";

function renderMarkdown(text) {
  const blocks = [];
  let out = String(text).replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(
      '<pre><code class="lang-' + escapeHtml(lang) + '">' +
      escapeHtml(code.trim()) + "</code></pre>"
    );
    return BLOCK_OPEN + (blocks.length - 1) + BLOCK_CLOSE;
  });
  // پاسخ‌های قدیمی یا مدل‌های ناسازگار ممکن است heading بسازند؛ # خام نمایش نده.
  out = out.replace(/^#{1,6}\s+(.+)$/gm, "**$1**");
  out = escapeHtml(out)
    .replace(/`([^`\n]+)`/g, (_, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  return out.replace(new RegExp(BLOCK_OPEN + "(\\d+)" + BLOCK_CLOSE, "g"),
                     (_, i) => blocks[Number(i)]);
}

function scrollDown() { logEl.scrollTop = logEl.scrollHeight; }

function addMessage(who, cls = "") {
  const welcome = messagesEl.querySelector(".empty");
  if (welcome) welcome.remove();
  const wrap = document.createElement("div");
  wrap.className = "msg " + cls;
  wrap.innerHTML =
    '<div class="who">' + who + "</div>" +
    '<div class="tools"></div><div class="bubble"></div>' +
    '<div class="sources"></div><div class="meta"></div>';
  messagesEl.appendChild(wrap);
  scrollDown();
  return {
    tools: wrap.querySelector(".tools"),
    bubble: wrap.querySelector(".bubble"),
    sources: wrap.querySelector(".sources"),
    meta: wrap.querySelector(".meta"),
  };
}

const TOOL_LABELS = {
  understand: "درک سؤال", search_docs: "جستجوی مستندات",
  list_variants: "بررسی روش‌های موجود", diagnose_error: "تحلیل خطا",
};

function renderTool(container, ev) {
  const id = "tool-" + ev.name + "-" + (ev.status === "done" ? "" : ev.detail || "");
  let el = container.querySelector('[data-id="' + CSS.escape(id) + '"]')
    || (ev.status === "done"
      ? container.querySelector('[data-id^="tool-' + ev.name + '"]:not(.done)') : null);
  if (!el) {
    el = document.createElement("div");
    el.dataset.id = id;
    container.appendChild(el);
  }
  el.className = "tool " + ev.status;
  el.textContent = (TOOL_LABELS[ev.name] || ev.name) + (ev.detail ? " — " + ev.detail : "");
  scrollDown();
}

function renderSources(container, items) {
  if (!items || !items.length) return;
  container.innerHTML =
    '<div class="label">منابع</div>' +
    items.map(source =>
      '<a class="src" href="' + escapeHtml(source.url) +
      '" target="_blank" rel="noopener noreferrer">' +
      escapeHtml(source.title) +
      (source.section ? '<span class="sec"> › ' + escapeHtml(source.section) + "</span>" : "") +
      "</a>"
    ).join("");
}

async function ask(message) {
  if (busy || !message.trim() || !activeSessionId) return;
  busy = true;
  sendEl.disabled = true;
  newChatEl.disabled = true;
  addMessage("شما", "user").bubble.textContent = message;
  const ui = addMessage("دستیار");
  let answer = "";

  try {
    for await (const ev of streamChat(message)) {
      const data = ev.data;
      if (ev.event === "token") {
        answer += data.t;
        ui.bubble.innerHTML = renderMarkdown(answer);
        scrollDown();
      } else if (ev.event === "tool") {
        renderTool(ui.tools, data);
      } else if (ev.event === "sources") {
        renderSources(ui.sources, data.items);
      } else if (ev.event === "done") {
        ui.meta.textContent =
          Number(data.tokens_used).toLocaleString("fa-IR") + " توکن · " +
          Number(data.latency_ms).toLocaleString("fa-IR") + " میلی‌ثانیه";
      } else if (ev.event === "error") {
        ui.bubble.classList.add("err");
        ui.bubble.textContent = data.message;
      }
    }
  } catch (error) {
    ui.bubble.classList.add("err");
    ui.bubble.textContent = "ارتباط با سرور قطع شد. لطفاً دوباره تلاش کنید.";
    console.error(error);
  } finally {
    busy = false;
    sendEl.disabled = false;
    try {
      await refreshSessions();
      const current = sessionItems.find(s => s.id === activeSessionId);
      if (current) titleEl.textContent = current.title;
    } catch (error) {
      console.error(error);
      syncNewChatButton();
    }
    inputEl.focus();
  }
}

/* ------------------------------------------------------------ events and startup */

formEl.addEventListener("submit", event => {
  event.preventDefault();
  const value = inputEl.value;
  inputEl.value = "";
  inputEl.style.height = "auto";
  ask(value);
});

inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
});

inputEl.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    formEl.requestSubmit();
  }
});

messagesEl.addEventListener("click", event => {
  if (event.target.classList.contains("hint")) ask(event.target.textContent);
});

listEl.addEventListener("click", event => {
  const button = event.target.closest("[data-action]");
  const row = event.target.closest(".session");
  if (!button || !row || busy) return;
  const id = row.dataset.id;
  const action = button.dataset.action;
  const task = action === "open" ? openSession(id)
    : action === "rename" ? renameSession(id)
    : action === "delete" ? deleteSession(id) : null;
  if (task) task.catch(error => alert(error.message));
});

newChatEl.addEventListener("click", () => {
  if (!busy && !currentSessionIsEmpty()) {
    createSession().catch(error => alert(error.message));
  }
});
document.getElementById("menu").addEventListener("click", openSidebar);
document.getElementById("scrim").addEventListener("click", closeSidebar);

async function start() {
  try {
    // سشن خالیِ باقی‌مانده از refresh یا بسته‌شدن صفحه ارزشی ندارد.
    await api(
      "/api/sessions/empty?client_id=" + encodeURIComponent(CLIENT_ID),
      { method: "DELETE" }
    );
    await refreshSessions();
    const remembered = sessionItems.find(item => item.id === activeSessionId);
    if (remembered) {
      const id = activeSessionId;
      activeSessionId = null;
      await openSession(id);
    } else {
      activeSessionId = null;
      draftSession = false;
      await createSession();
    }
  } catch (error) {
    clearMessages();
    addMessage("سیستم").bubble.textContent = "بارگذاری تاریخچه ممکن نشد: " + error.message;
  }
  fetch("/health")
    .then(response => response.json())
    .then(health => { modeEl.textContent = health.mock ? "حالت نمونه" : "متصل به LLM"; })
    .catch(() => { modeEl.textContent = "سرور در دسترس نیست"; });
}

window.addEventListener("pagehide", () => {
  if (!busy && activeSessionId && currentSessionIsEmpty()) {
    fetch(
      "/api/sessions/" + encodeURIComponent(activeSessionId) +
      "?client_id=" + encodeURIComponent(CLIENT_ID),
      { method: "DELETE", keepalive: true }
    ).catch(() => {});
  }
});

start();
