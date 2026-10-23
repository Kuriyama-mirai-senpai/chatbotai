/* ==========================================================================
 * app.js - Logic Frontend cho "AI Giải Đáp Công Nghệ"
 *
 * Chức năng:
 *   - Quản lý phiên chat (tạo, chọn, đổi tên, xóa) qua REST API
 *   - Gửi câu hỏi và nhận phản hồi STREAMING qua Server-Sent Events
 *   - Render Markdown + tô sáng cú pháp + nút Copy Code
 *   - Auto-scroll mượt khi AI đang trả lời
 *   - Xử lý lỗi thân thiện, không làm đơ trang
 * ========================================================================== */
"use strict";

/* ---------- 1. Hằng số & trạng thái ---------- */
const API = {
  health: "/api/health",
  sessions: "/api/sessions",
  chat: "/api/chat",
  knowledge: "/api/knowledge",
};

const state = {
  sessionId: null,
  sessions: [],
  isStreaming: false,
  controller: null,        // AbortController của request hiện tại
  autoScroll: true,        // tắt khi người dùng tự cuộn lên
  useKnowledge: true,
  modelName: "claude-opus-5.5",
};

/* ---------- 2. Tiện ích DOM ---------- */
const $ = (id) => document.getElementById(id);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
};

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* ---------- 3. Toast ---------- */
function showToast(message, type = "info", timeout = 5200) {
  const container = $("toastContainer");
  if (!container) return;

  const icons = { info: "ℹ️", success: "✅", error: "⚠️", warn: "🔔" };
  const toast = el("div", `toast ${type}`);
  const icon = el("span", "toast-icon", icons[type] || icons.info);
  const body = el("span", "toast-body", message);
  toast.append(icon, body);
  container.appendChild(toast);

  const remove = () => {
    toast.classList.add("leaving");
    setTimeout(() => toast.remove(), 260);
  };
  toast.addEventListener("click", remove);
  setTimeout(remove, timeout);
}

/* ---------- 4. Cấu hình Markdown ---------- */
function configureMarked() {
  if (typeof marked === "undefined") {
    console.warn("marked.js chưa nạp - sẽ hiển thị văn bản thuần.");
    return;
  }
  marked.setOptions({
    breaks: true,      // Xuống dòng đơn => <br> (thân thiện với chat)
    gfm: true,         // GitHub Flavored Markdown: bảng, danh sách công việc
  });
}

function sanitizeHtml(html) {
  if (typeof DOMPurify !== "undefined") {
    return DOMPurify.sanitize(html, {
      ADD_ATTR: ["target", "rel"],
      FORBID_TAGS: ["style", "iframe", "form", "input"],
    });
  }
  return html;
}

function renderMarkdown(rawText) {
  const source = typeof rawText === "string" ? rawText : "";
  if (typeof marked === "undefined") {
    return `<p>${escapeHtml(source).replace(/\n/g, "<br>")}</p>`;
  }
  let html = "";
  try {
    html = marked.parse(source);
  } catch (err) {
    console.error("Lỗi parse Markdown:", err);
    html = `<p>${escapeHtml(source).replace(/\n/g, "<br>")}</p>`;
  }
  return sanitizeHtml(html);
}

/* ---------- 5. Nâng cấp khối code: tô sáng + nút Copy ---------- */
function enhanceCodeBlocks(container) {
  container.querySelectorAll("pre > code").forEach((codeEl) => {
    const pre = codeEl.parentElement;
    if (pre.parentElement && pre.parentElement.classList.contains("code-block")) return;

    // Xác định ngôn ngữ
    let language = "";
    const match = /language-([\w+#-]+)/.exec(codeEl.className || "");
    if (match) language = match[1].toLowerCase();

    // Tô sáng cú pháp
    if (typeof hljs !== "undefined") {
      try {
        const result = language && hljs.getLanguage(language)
          ? hljs.highlight(codeEl.textContent, { language, ignoreIllegals: true })
          : hljs.highlightAuto(codeEl.textContent);
        codeEl.innerHTML = result.value;
        codeEl.classList.add("hljs");
        if (!language && result.language) language = result.language;
      } catch (err) {
        console.warn("Không tô sáng được cú pháp:", err);
      }
    }

    // Bọc khối code với thanh tiêu đề + nút Copy
    const wrapper = el("div", "code-block");
    const head = el("div", "code-head");
    const label = el("span", "code-lang", language || "code");
    const copyBtn = el("button", "copy-code-btn", "⧉ Copy");
    copyBtn.type = "button";

    copyBtn.addEventListener("click", async () => {
      const original = codeEl.textContent;
      const ok = await copyToClipboard(original);
      if (ok) {
        copyBtn.textContent = "✓ Đã copy";
        copyBtn.classList.add("copied");
        setTimeout(() => {
          copyBtn.textContent = "⧉ Copy";
          copyBtn.classList.remove("copied");
        }, 1800);
      } else {
        showToast("Không thể copy vào clipboard của trình duyệt.", "warn");
      }
    });

    head.append(label, copyBtn);
    pre.parentNode.insertBefore(wrapper, pre);
    wrapper.append(head, pre);
  });
}

async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // Fallback: http://127.0.0.1 có thể không phải secure context ở vài trình duyệt
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch (err) {
    console.error("Lỗi copy:", err);
    return false;
  }
}

/* ---------- 6. Render công thức toán (KaTeX) ---------- */
function renderMath(container) {
  if (typeof renderMathInElement !== "function") return;
  try {
    renderMathInElement(container, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
    });
  } catch (err) {
    console.warn("Lỗi render công thức:", err);
  }
}

/* ---------- 7. Auto-scroll mượt ---------- */
function scrollToBottom(behavior = "smooth") {
  const scrollBox = $("chatScroll");
  if (!scrollBox || !state.autoScroll) return;
  // requestAnimationFrame giúp cuộn mượt ngay cả khi DOM vừa cập nhật
  requestAnimationFrame(() => {
    scrollBox.scrollTo({ top: scrollBox.scrollHeight, behavior });
  });
}

function setupAutoScrollDetection() {
  const scrollBox = $("chatScroll");
  if (!scrollBox) return;
  scrollBox.addEventListener("scroll", () => {
    const distanceFromBottom =
      scrollBox.scrollHeight - scrollBox.scrollTop - scrollBox.clientHeight;
    // Nếu người dùng cuộn lên hơn 90px thì tạm dừng auto-scroll
    state.autoScroll = distanceFromBottom < 90;
  });
}

/* ---------- 8. Tạo phần tử tin nhắn ---------- */
function createMessageElement(role, contentHtml, options = {}) {
  const { sources = [], timestamp = "", model = "", isStreaming = false } = options;

  const article = el("article", `message ${role}`);
  if (options.isError) article.classList.add("error");

  const avatar = el("div", "avatar", role === "user" ? "Bạn" : "AI");

  const wrap = el("div", "bubble-wrap");
  const bubble = el("div", "bubble");

  if (role === "user") {
    bubble.textContent = contentHtml;
  } else {
    const md = el("div", "md");
    md.innerHTML = contentHtml;
    bubble.appendChild(md);
    if (isStreaming) md.classList.add("cursor-blink");
  }

  wrap.appendChild(bubble);

  // Nguồn tri thức
  if (sources && sources.length) {
    const sourcesBox = el("div", "sources");
    sourcesBox.appendChild(el("span", "sources-label", "Nguồn tri thức:"));
    sources.forEach((src) => {
      const chip = el("span", "source-chip", `📄 ${src.title || src.id}`);
      chip.title = (src.source || "Tài liệu nội bộ") + (src.score ? ` · điểm ${src.score}` : "");
      sourcesBox.appendChild(chip);
    });
    wrap.appendChild(sourcesBox);
  }

  // Meta + hành động
  const meta = el("div", "message-meta");
  const timeLabel = el("span", "", timestamp || formatTime(new Date()));
  const actions = el("div", "msg-actions");

  if (role === "assistant") {
    const copyBtn = el("button", "mini-btn", "⧉ Copy");
    copyBtn.type = "button";
    copyBtn.addEventListener("click", async () => {
      const plain = bubble.innerText || "";
      const ok = await copyToClipboard(plain);
      showToast(ok ? "Đã copy câu trả lời." : "Không thể copy.", ok ? "success" : "warn", 2400);
    });
    actions.appendChild(copyBtn);
  } else {
    const editBtn = el("button", "mini-btn", "✎ Dùng lại");
    editBtn.type = "button";
    editBtn.addEventListener("click", () => {
      const input = $("messageInput");
      if (!input) return;
      input.value = bubble.textContent || "";
      autoResizeInput();
      input.focus();
    });
    actions.appendChild(editBtn);
  }

  meta.append(timeLabel);
  if (model && role === "assistant") meta.appendChild(el("span", "", `· ${model}`));
  meta.appendChild(actions);
  wrap.appendChild(meta);

  article.append(avatar, wrap);
  return { article, bubble, wrap };
}

function appendUserMessage(text) {
  const { article } = createMessageElement("user", text);
  $("messages").appendChild(article);
  hideWelcome();
  scrollToBottom("auto");
}

function appendAssistantMessage(html, options = {}) {
  const created = createMessageElement("assistant", html, options);
  created.article.dataset.streaming = options.isStreaming ? "1" : "0";
  $("messages").appendChild(created.article);
  return created;
}

function formatTime(date) {
  try {
    return date.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" });
  } catch (err) {
    return "";
  }
}

function hideWelcome() {
  const welcome = $("welcomeScreen");
  if (welcome) welcome.classList.add("hidden");
}

function showWelcomeIfEmpty() {
  const messages = $("messages");
  const welcome = $("welcomeScreen");
  if (!messages || !welcome) return;
  if (messages.children.length === 0) welcome.classList.remove("hidden");
  else welcome.classList.add("hidden");
}

function clearMessages() {
  const messages = $("messages");
  if (messages) messages.innerHTML = "";
  showWelcomeIfEmpty();
}

/* ---------- 9. Gọi REST API ---------- */
async function apiRequest(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json; charset=utf-8" },
    ...options,
  });

  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = null;
    }
  }

  if (!response.ok) {
    const message = (payload && (payload.error || payload.message))
      || `Lỗi máy chủ (HTTP ${response.status}).`;
    throw new Error(message);
  }
  return payload || {};
}

/* ---------- 10. Kiểm tra tình trạng hệ thống ---------- */
async function checkHealth() {
  const pill = $("statusPill");
  const text = $("statusText");
  try {
    const data = await apiRequest(API.health, { method: "GET" });

    if (data.model) {
      state.modelName = data.model;
      const brandModel = $("brandModel");
      if (brandModel) brandModel.textContent = data.model;
    }

    const kbCount = $("kbCount");
    if (kbCount) kbCount.textContent = String(data.knowledge_documents ?? 0);

    if (data.api_key_configured) {
      pill.classList.add("online");
      text.textContent = "Đã kết nối AI";
    } else {
      pill.classList.add("warn");
      text.textContent = "Chưa có API Key";
      showToast(
        "Chưa cấu hình API Key. Hãy mở file .env, điền API_KEY rồi khởi động lại ứng dụng.",
        "warn",
        9000
      );
    }
  } catch (err) {
    console.error("Health check thất bại:", err);
    pill.classList.add("error");
    text.textContent = "Mất kết nối backend";
    showToast("Không kết nối được backend Flask. Hãy kiểm tra server đang chạy.", "error", 8000);
  }
}

/* ---------- 11. Quản lý danh sách phiên chat ---------- */
async function loadSessions() {
  try {
    const data = await apiRequest(API.sessions, { method: "GET" });
    state.sessions = Array.isArray(data.sessions) ? data.sessions : [];
    renderSessionList();
  } catch (err) {
    console.error("Không tải được danh sách phiên:", err);
    const list = $("sessionList");
    if (list) list.innerHTML = "";
    if (list) list.appendChild(el("div", "session-empty", "Không tải được lịch sử."));
  }
}

function renderSessionList() {
  const list = $("sessionList");
  if (!list) return;
  list.innerHTML = "";

  if (!state.sessions.length) {
    list.appendChild(el("div", "session-empty", "Chưa có cuộc trò chuyện nào."));
    return;
  }

  state.sessions.forEach((session) => {
    const item = el("div", "session-item");
    if (session.id === state.sessionId) item.classList.add("active");

    const icon = el("div", "session-icon", "💬");
    const body = el("div", "session-body");
    const title = el("div", "session-title", session.title || "Cuộc trò chuyện mới");

    const count = session.message_count || 0;
    const meta = el("div", "session-meta", `${count} tin nhắn · ${formatRelativeTime(session.updated_at)}`);
    body.append(title, meta);

    const delBtn = el("button", "session-delete", "✕");
    delBtn.type = "button";
    delBtn.title = "Xóa cuộc trò chuyện";
    delBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      confirmDeleteSession(session.id, session.title);
    });

    item.append(icon, body, delBtn);
    item.addEventListener("click", () => selectSession(session.id));
    list.appendChild(item);
  });
}

function formatRelativeTime(isoString) {
  if (!isoString) return "";
  const then = new Date(isoString);
  if (Number.isNaN(then.getTime())) return "";
  const diffMs = Date.now() - then.getTime();
  const minutes = Math.floor(diffMs / 60000);

  if (minutes < 1) return "vừa xong";
  if (minutes < 60) return `${minutes} phút trước`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} giờ trước`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} ngày trước`;
  return then.toLocaleDateString("vi-VN");
}

async function createNewSession(switchTo = true) {
  try {
    const data = await apiRequest(API.sessions, {
      method: "POST",
      body: JSON.stringify({}),
    });
    const session = data.session;
    state.sessions.unshift({ ...session, message_count: 0 });
    if (switchTo) {
      state.sessionId = session.id;
      clearMessages();
      setHeader(session.title, "Sẵn sàng hỗ trợ bạn");
    }
    renderSessionList();
    closeSidebarOnMobile();
    return session;
  } catch (err) {
    console.error("Không tạo được phiên mới:", err);
    showToast("Không tạo được cuộc trò chuyện mới. Vui lòng thử lại.", "error");
    return null;
  }
}

async function selectSession(sessionId) {
  if (state.isStreaming) stopStreaming();
  try {
    const data = await apiRequest(`${API.sessions}/${encodeURIComponent(sessionId)}`, { method: "GET" });
    state.sessionId = sessionId;
    clearMessages();

    const session = data.session || {};
    setHeader(session.title || "Cuộc trò chuyện", `${(data.messages || []).length} tin nhắn`);
    (data.messages || []).forEach((msg) => renderStoredMessage(msg));
    renderSessionList();
    closeSidebarOnMobile();
    state.autoScroll = true;
    scrollToBottom("auto");
  } catch (err) {
    console.error("Không mở được phiên:", err);
    showToast(err.message || "Không mở được cuộc trò chuyện.", "error");
  }
}

function renderStoredMessage(msg) {
  if (msg.role === "user") {
    appendUserMessage(msg.content);
  } else {
    const html = renderMarkdown(msg.content);
    const created = appendAssistantMessage(html, {
      sources: msg.sources || [],
      timestamp: formatTime(new Date(msg.created_at)),
      model: msg.model || "",
    });
    enhanceCodeBlocks(created.bubble);
    renderMath(created.bubble);
  }
}

async function confirmDeleteSession(sessionId, title) {
  const label = title || "cuộc trò chuyện này";
  if (!window.confirm(`Xóa "${label}"? Hành động này không thể hoàn tác.`)) return;

  try {
    await apiRequest(`${API.sessions}/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    state.sessions = state.sessions.filter((s) => s.id !== sessionId);

    if (state.sessionId === sessionId) {
      state.sessionId = null;
      clearMessages();
      setHeader("Cuộc trò chuyện mới", "Sẵn sàng hỗ trợ bạn");
    }
    renderSessionList();
    showToast("Đã xóa cuộc trò chuyện.", "success", 2600);
  } catch (err) {
    console.error("Không xóa được phiên:", err);
    showToast("Không xóa được cuộc trò chuyện.", "error");
  }
}

async function confirmDeleteAllSessions() {
  if (!state.sessions.length) {
    showToast("Chưa có lịch sử để xóa.", "info", 2400);
    return;
  }
  if (!window.confirm("Xóa TOÀN BỘ lịch sử trò chuyện? Hành động này không thể hoàn tác.")) return;

  try {
    await apiRequest(API.sessions, { method: "DELETE" });
    state.sessions = [];
    state.sessionId = null;
    clearMessages();
    setHeader("Cuộc trò chuyện mới", "Sẵn sàng hỗ trợ bạn");
    renderSessionList();
    showToast("Đã xóa toàn bộ lịch sử.", "success", 2600);
  } catch (err) {
    console.error("Không xóa được toàn bộ lịch sử:", err);
    showToast("Không xóa được lịch sử.", "error");
  }
}

function setHeader(title, subtitle) {
  const titleEl = $("chatTitle");
  const subEl = $("chatSubtitle");
  if (titleEl) titleEl.textContent = title || "Cuộc trò chuyện";
  if (subEl && subtitle !== undefined) subEl.textContent = subtitle;
}

/* ---------- 12. Gửi tin nhắn & nhận STREAMING ---------- */
function setStreamingUI(active) {
  state.isStreaming = active;
  const sendBtn = $("sendBtn");
  const stopBtn = $("stopBtn");
  if (sendBtn) sendBtn.classList.toggle("hidden", active);
  if (stopBtn) stopBtn.classList.toggle("hidden", !active);
}

async function sendMessage(presetText) {
  if (state.isStreaming) {
    showToast("AI đang trả lời, hãy đợi hoặc bấm nút dừng.", "info", 2600);
    return;
  }

  const input = $("messageInput");
  const raw = presetText !== undefined ? presetText : (input ? input.value : "");
  const message = (raw || "").trim();

  if (!message) {
    showToast("Vui lòng nhập nội dung câu hỏi.", "warn", 2600);
    if (input) input.focus();
    return;
  }
  if (message.length > 8000) {
    showToast("Câu hỏi quá dài (tối đa 8000 ký tự).", "warn");
    return;
  }

  // Đảm bảo có phiên chat trước khi gửi
  if (!state.sessionId) {
    const session = await createNewSession(true);
    if (!session) return;
  }

  if (input && presetText === undefined) {
    input.value = "";
    autoResizeInput();
  }

  appendUserMessage(message);
  state.autoScroll = true;
  setStreamingUI(true);

  // Khung tin nhắn AI rỗng kèm hiệu ứng "đang gõ"
  const created = appendAssistantMessage(
    '<div class="typing"><span></span><span></span><span></span></div>',
    { isStreaming: false }
  );
  const bubble = created.bubble;
  const mdBox = bubble.querySelector(".md");
  let streamedText = "";
  let typingVisible = true;
  let sourcesFromMeta = [];

  state.controller = new AbortController();

  try {
    const response = await fetch(API.chat, {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        message: message,
        session_id: state.sessionId,
        use_knowledge: state.useKnowledge,
      }),
      signal: state.controller.signal,
    });

    // Lỗi trả về dạng JSON (khi chưa bắt đầu stream)
    if (!response.ok) {
      let detail = "";
      try {
        const errData = await response.json();
        detail = errData.error || errData.message || "";
      } catch (e) {
        detail = "";
      }
      throw new Error(detail || `Lỗi máy chủ (HTTP ${response.status}).`);
    }

    if (!response.body) {
      throw new Error("Trình duyệt không hỗ trợ đọc luồng phản hồi (streaming).");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() || "";

      for (const rawEvent of events) {
        const parsed = parseSSE(rawEvent);
        if (!parsed) continue;

        if (parsed.event === "meta") {
          sourcesFromMeta = parsed.data.sources || [];
          if (parsed.data.session_id) state.sessionId = parsed.data.session_id;
          if (parsed.data.model) state.modelName = parsed.data.model;
        } else if (parsed.event === "delta") {
          if (typingVisible) {
            mdBox.innerHTML = "";
            typingVisible = false;
            mdBox.classList.add("cursor-blink");
          }
          streamedText += parsed.data.text || "";
          mdBox.textContent = streamedText;   // text thuần khi đang stream (nhanh, an toàn)
          scrollToBottom("auto");
        } else if (parsed.event === "done") {
          streamedText = parsed.data.content || streamedText;
          if (parsed.data.title) setHeader(parsed.data.title, "Đã trả lời");
        } else if (parsed.event === "error") {
          throw new Error(parsed.data.error || "AI gặp lỗi khi trả lời.");
        }
      }
    }

    // Stream xong: render Markdown đầy đủ
    mdBox.classList.remove("cursor-blink");
    finalizeAssistantBubble(mdBox, streamedText, sourcesFromMeta, created.wrap);

    if (!streamedText.trim()) {
      throw new Error("AI không trả về nội dung nào. Hãy thử lại.");
    }
    loadSessions();
  } catch (err) {
    handleSendError(err, created, mdBox, streamedText, sourcesFromMeta);
  } finally {
    state.controller = null;
    setStreamingUI(false);
    const inputEl = $("messageInput");
    if (inputEl) inputEl.focus();
    scrollToBottom();
  }
}

function parseSSE(rawEvent) {
  const lines = String(rawEvent).split("\n");
  let eventName = "message";
  const dataLines = [];

  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }

  if (!dataLines.length) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLines.join("\n")) };
  } catch (err) {
    console.warn("Bỏ qua sự kiện SSE không hợp lệ:", dataLines.join("\n").slice(0, 120));
    return null;
  }
}

function handleSendError(err, created, mdBox, streamedText, sources) {
  if (err && err.name === "AbortError") {
    mdBox.classList.remove("cursor-blink");
    if (streamedText.trim()) {
      finalizeAssistantBubble(mdBox, streamedText, sources, created.wrap);
      created.wrap.appendChild(el("div", "message-meta", "⏹ Đã dừng tạo câu trả lời"));
    } else {
      created.article.remove();
      showToast("Đã dừng tạo câu trả lời.", "info", 2400);
    }
    return;
  }

  console.error("Lỗi khi gửi tin nhắn:", err);
  mdBox.classList.remove("cursor-blink");

  if (streamedText.trim()) {
    // Đã nhận được một phần nội dung -> giữ lại phần đã nhận và cảnh báo
    finalizeAssistantBubble(mdBox, streamedText, sources, created.wrap);
    created.wrap.appendChild(
      el("div", "message-meta", `⚠️ Luồng bị ngắt: ${err.message || err}`)
    );
    showToast(err.message || "Kết nối bị ngắt giữa chừng.", "error");
  } else {
    created.article.remove();
    renderErrorMessage(err.message || "Không thể lấy phản hồi từ AI.");
  }
}

function finalizeAssistantBubble(mdBox, text, sources, wrap) {
  mdBox.innerHTML = renderMarkdown(text);
  enhanceCodeBlocks(wrap);
  renderMath(mdBox);

  if (sources && sources.length) {
    const existing = wrap.querySelector(".sources");
    if (existing) existing.remove();

    const sourcesBox = el("div", "sources");
    sourcesBox.appendChild(el("span", "sources-label", "Nguồn tri thức:"));
    sources.forEach((src) => {
      const chip = el("span", "source-chip", `📄 ${src.title || src.id}`);
      chip.title = (src.source || "Tài liệu nội bộ") + (src.score ? ` · điểm ${src.score}` : "");
      sourcesBox.appendChild(chip);
    });

    const meta = wrap.querySelector(".message-meta");
    if (meta) wrap.insertBefore(sourcesBox, meta);
    else wrap.appendChild(sourcesBox);
  }
}

function renderErrorMessage(text) {
  const article = el("article", "message assistant error");
  const avatar = el("div", "avatar", "!");
  const wrap = el("div", "bubble-wrap");
  const bubble = el("div", "bubble");
  bubble.textContent = text;

  const meta = el("div", "message-meta");
  meta.appendChild(el("span", "", formatTime(new Date())));

  const retry = el("button", "mini-btn", "↻ Đóng");
  retry.type = "button";
  retry.addEventListener("click", () => {
    article.remove();
    showWelcomeIfEmpty();
    const input = $("messageInput");
    if (input) input.focus();
  });
  meta.appendChild(retry);

  wrap.append(bubble, meta);
  article.append(avatar, wrap);
  $("messages").appendChild(article);
  hideWelcome();
  scrollToBottom();
}

function stopStreaming() {
  if (state.controller) {
    try {
      state.controller.abort();
    } catch (err) {
      console.warn("Không hủy được request:", err);
    }
  }
}

/* ---------- 13. Ô nhập liệu ---------- */
function autoResizeInput() {
  const input = $("messageInput");
  if (!input) return;
  input.style.height = "auto";
  const next = Math.min(input.scrollHeight, 216);
  input.style.height = `${next}px`;
}

function updateCharCount() {
  const input = $("messageInput");
  const counter = $("charCount");
  if (!input || !counter) return;
  counter.textContent = String(input.value.length);
  counter.style.color = input.value.length > 7600 ? "var(--danger)" : "";
}

function setupComposer() {
  const input = $("messageInput");
  const sendBtn = $("sendBtn");
  const stopBtn = $("stopBtn");

  if (input) {
    input.addEventListener("input", () => {
      autoResizeInput();
      updateCharCount();
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendMessage();
      }
    });

    input.addEventListener("paste", () => setTimeout(autoResizeInput, 0));
  }

  if (sendBtn) sendBtn.addEventListener("click", () => sendMessage());
  if (stopBtn) stopBtn.addEventListener("click", () => stopStreaming());

  autoResizeInput();
  updateCharCount();
}

/* ---------- 14. Sidebar & sự kiện UI ---------- */
function openSidebar() {
  const sidebar = $("sidebar");
  const overlay = $("sidebarOverlay");
  if (sidebar) sidebar.classList.add("open");
  if (overlay) overlay.classList.add("show");
}

function closeSidebar() {
  const sidebar = $("sidebar");
  const overlay = $("sidebarOverlay");
  if (sidebar) sidebar.classList.remove("open");
  if (overlay) overlay.classList.remove("show");
}

function closeSidebarOnMobile() {
  if (window.innerWidth <= 860) closeSidebar();
}

function setupSidebar() {
  const newChatBtn = $("newChatBtn");
  const deleteAllBtn = $("deleteAllBtn");
  const openBtn = $("openSidebarBtn");
  const closeBtn = $("closeSidebarBtn");
  const overlay = $("sidebarOverlay");
  const clearBtn = $("clearChatBtn");

  if (newChatBtn) newChatBtn.addEventListener("click", () => createNewSession(true));
  if (deleteAllBtn) deleteAllBtn.addEventListener("click", () => confirmDeleteAllSessions());
  if (openBtn) openBtn.addEventListener("click", openSidebar);
  if (closeBtn) closeBtn.addEventListener("click", closeSidebar);
  if (overlay) overlay.addEventListener("click", closeSidebar);

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      if (!state.sessionId) {
        showToast("Chưa có cuộc trò chuyện nào để xóa.", "info", 2400);
        return;
      }
      confirmDeleteSession(state.sessionId, $("chatTitle")?.textContent);
    });
  }
}

function setupKnowledgeToggle() {
  const toggle = $("kbToggle");
  if (!toggle) return;
  state.useKnowledge = toggle.checked;

  toggle.addEventListener("change", () => {
    state.useKnowledge = toggle.checked;
    showToast(
      state.useKnowledge
        ? "Đã BẬT tra cứu Knowledge Base nội bộ."
        : "Đã TẮT tra cứu Knowledge Base nội bộ.",
      "info",
      2400
    );
  });
}

function setupSuggestions() {
  document.querySelectorAll(".suggestion-card").forEach((card) => {
    card.addEventListener("click", () => {
      const prompt = card.dataset.prompt || card.textContent.trim();
      sendMessage(prompt);
    });
  });
}

function setupKeyboardShortcuts() {
  document.addEventListener("keydown", (event) => {
    // Ctrl/Cmd + K: cuộc trò chuyện mới
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      createNewSession(true);
    }
    // Esc: đóng sidebar hoặc dừng stream
    if (event.key === "Escape") {
      if (state.isStreaming) stopStreaming();
      else closeSidebar();
    }
  });
}

/* ---------- 15. Khởi động ứng dụng ---------- */
async function initApp() {
  configureMarked();
  setupComposer();
  setupSidebar();
  setupKnowledgeToggle();
  setupSuggestions();
  setupKeyboardShortcuts();
  setupAutoScrollDetection();
  showWelcomeIfEmpty();

  await checkHealth();
  await loadSessions();

  // Nếu đã có lịch sử, mở phiên mới nhất; ngược lại tạo phiên mới sẵn sàng dùng
  if (state.sessions.length > 0) {
    await selectSession(state.sessions[0].id);
  } else {
    await createNewSession(true);
  }

  setHeader("Cuộc trò chuyện mới", "Sẵn sàng hỗ trợ bạn");
  const input = $("messageInput");
  if (input) input.focus();
}

window.addEventListener("error", (event) => {
  console.error("Lỗi JavaScript:", event.error || event.message);
});

window.addEventListener("unhandledrejection", (event) => {
  console.error("Promise bị từ chối:", event.reason);
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initApp);
} else {
  initApp();
}