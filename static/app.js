/*
 * اسکلت فاز ۰ — مصرف‌کننده‌ی قرارداد SSE در app/contracts.py
 *
 * نکته‌ای که وقت زیادی از تیم‌ها می‌گیرد:
 * EventSource مرورگر فقط GET می‌زند و بدنه قبول نمی‌کند. چون قرارداد ما POST
 * است، باید با fetch + ReadableStream خودمان SSE را پارس کنیم.
 * تابع streamChat() پایین دقیقاً همین کار را می‌کند و آماده است.
 */

const logEl   = document.getElementById("log");
const formEl  = document.getElementById("f");
const inputEl = document.getElementById("q");
const sendEl  = document.getElementById("send");
const modeEl  = document.getElementById("mode");

const SESSION_ID = crypto.randomUUID();

/* ---------------------------------------------------------------- SSE */

async function* streamChat(message, signal) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: SESSION_ID }),
    signal,
  });

  if (!res.ok) {
    let msg = "خطای سرور";
    try { msg = (await res.json()).message || msg; } catch (_) {}
    yield { event: "error", data: { message: msg, code: String(res.status) } };
    return;
  }

  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // رویدادها با یک خط خالی از هم جدا می‌شوند
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

/* ------------------------------------------------------------ رندر */

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const BLOCK_OPEN  = "⸢BLK";
const BLOCK_CLOSE = "⸣";

/**
 * رندر حداقلی مارک‌داون: بلوک کد، کد درون‌خطی، بولد.
 * ابوالفضل: اگر کتابخانه‌ای خواستی، حتماً vendor کن — بدون CDN.
 */
function renderMarkdown(text) {
  const blocks = [];

  // بلوک‌های کد کنار گذاشته می‌شوند تا escape و بقیه قواعد رویشان اجرا نشود.
  // جانگهدار عمداً کاراکتری است که در متن فارسی هرگز نمی‌آید.
  let out = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(
      '<pre><code class="lang-' + lang + '">' + escapeHtml(code.trim()) + "</code></pre>"
    );
    return BLOCK_OPEN + (blocks.length - 1) + BLOCK_CLOSE;
  });

  out = escapeHtml(out)
    .replace(/`([^`\n]+)`/g, (_, c) => "<code>" + c + "</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");

  const re = new RegExp(BLOCK_OPEN + "(\\d+)" + BLOCK_CLOSE, "g");
  return out.replace(re, (_, i) => blocks[Number(i)]);
}

function scrollDown() {
  logEl.scrollTop = logEl.scrollHeight;
}

function addMessage(who, cls) {
  const wrap = document.createElement("div");
  wrap.className = "msg " + (cls || "");
  wrap.innerHTML =
    '<div class="who">' + who + "</div>" +
    '<div class="tools"></div>' +
    '<div class="bubble"></div>' +
    '<div class="sources"></div>' +
    '<div class="meta"></div>';
  logEl.appendChild(wrap);
  scrollDown();
  return {
    tools:   wrap.querySelector(".tools"),
    bubble:  wrap.querySelector(".bubble"),
    sources: wrap.querySelector(".sources"),
    meta:    wrap.querySelector(".meta"),
  };
}

const TOOL_LABELS = {
  search_docs:         "جستجوی مستندات",
  diagnose_error:      "تحلیل خطا",
  generate_liara_json: "ساخت فایل liara.json",
  estimate_cost:       "تخمین هزینه",
};

function renderTool(container, ev) {
  const id = "tool-" + ev.name;
  let el = container.querySelector('[data-id="' + id + '"]');
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
    items.map(s =>
      '<a class="src" href="' + escapeHtml(s.url) + '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(s.title) +
        (s.section ? '<span class="sec"> › ' + escapeHtml(s.section) + "</span>" : "") +
      "</a>"
    ).join("");
  scrollDown();
}

/* ------------------------------------------------------------ جریان */

let busy = false;

async function ask(message) {
  if (busy || !message.trim()) return;
  busy = true;
  sendEl.disabled = true;

  const hints = document.querySelector(".hints");
  if (hints) hints.remove();

  addMessage("شما", "user").bubble.textContent = message;

  const ui = addMessage("دستیار");
  let answer = "";

  try {
    for await (const ev of streamChat(message)) {
      const data = ev.data;
      switch (ev.event) {
        case "token":
          answer += data.t;
          ui.bubble.innerHTML = renderMarkdown(answer);
          scrollDown();
          break;
        case "tool":
          renderTool(ui.tools, data);
          break;
        case "sources":
          renderSources(ui.sources, data.items);
          break;
        case "done":
          ui.meta.textContent =
            data.tokens_used + " توکن · " + data.latency_ms + " میلی‌ثانیه" +
            (data.cached ? " · از کش" : "");
          break;
        case "error":
          ui.bubble.classList.add("err");
          ui.bubble.textContent = data.message;
          break;
      }
    }
  } catch (e) {
    ui.bubble.classList.add("err");
    ui.bubble.textContent = "ارتباط با سرور قطع شد. لطفاً دوباره تلاش کنید.";
    console.error(e);
  } finally {
    busy = false;
    sendEl.disabled = false;
    inputEl.focus();
  }
}

/* ------------------------------------------------------------ رویدادها */

formEl.addEventListener("submit", e => {
  e.preventDefault();
  const v = inputEl.value;
  inputEl.value = "";
  inputEl.style.height = "auto";
  ask(v);
});

inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
});

inputEl.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    formEl.requestSubmit();
  }
});

logEl.addEventListener("click", e => {
  if (e.target.classList.contains("hint")) ask(e.target.textContent);
});

fetch("/health")
  .then(r => r.json())
  .then(h => { modeEl.textContent = h.mock ? "حالت نمونه (mock)" : "متصل به LLM"; })
  .catch(() => { modeEl.textContent = "سرور در دسترس نیست"; });
