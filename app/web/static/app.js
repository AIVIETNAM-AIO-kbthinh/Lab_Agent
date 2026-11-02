"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const msgs = $("messages"), input = $("input"), form = $("composer"), sendBtn = $("send");
  const fileInput = $("file-input"), attachBtn = $("attach"), attachList = $("attachments");
  let conversationId = sessionStorage.getItem("la_conv") || null;
  let busy = false;
  let pendingUploads = []; // {upload_id, filename}
  const draftCards = {}; // draft_id → thẻ mới nhất (thẻ cũ bị vô hiệu)

  const TOOL_LABEL = {
    search_kb: "Đang tìm trong tài liệu…", query_equipment: "Đang tra thiết bị…", my_borrows: "Đang tra phiếu mượn…",
    query_duty: "Đang xem lịch trực…", query_members: "Đang tra thành viên…", list_forms: "Đang xem các loại đơn…",
    get_application_status: "Đang kiểm tra…", list_pending_applications: "Đang xem đơn chờ duyệt…",
    get_form_template: "Đang lấy mẫu đơn…", start_form: "Đang tạo bản nháp…", update_form_fields: "Đang cập nhật đơn…",
    attach_file: "Đang gắn file…", submit_filled_form: "Đang đọc đơn bạn gửi…", render_preview: "Đang tạo bản xem trước…",
    cancel_form: "Đang huỷ…", return_equipment: "Đang chuẩn bị yêu cầu trả…", extend_borrow: "Đang chuẩn bị gia hạn…",
    request_member_change: "Đang chuẩn bị yêu cầu…",
    propose_template: "Đang phân tích mẫu đơn (tẩy trắng, nhận diện trường, điền thử)… có thể mất 1–2 phút",
    update_template_draft: "Đang cập nhật mẫu đơn…", cancel_template_draft: "Đang huỷ…",
  };
  const SUGGEST = {
    guest: ["Lab nghiên cứu những hướng nào?", "Làm sao để tham gia lab?", "Tin tức mới nhất của lab?"],
    member: ["Máy chiếu còn mấy cái?", "Mình muốn mượn thiết bị", "Mình đang mượn gì, hạn khi nào?"],
  };

  function addMsg(role, html) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role;
    const b = document.createElement("div");
    b.className = "bubble";
    b.innerHTML = html;
    wrap.appendChild(b);
    msgs.appendChild(wrap);
    msgs.scrollTop = msgs.scrollHeight;
    return b;
  }

  function setSuggestions(role) {
    const box = $("suggestions");
    box.textContent = "";
    for (const s of SUGGEST[role === "guest" ? "guest" : "member"]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip";
      btn.textContent = s;
      btn.addEventListener("click", () => { input.value = s; form.requestSubmit(); });
      box.appendChild(btn);
    }
  }

  // ---------------------------------------------------------------- đính kèm file
  function renderAttachments() {
    attachList.textContent = "";
    attachList.hidden = pendingUploads.length === 0;
    pendingUploads.forEach((u, i) => {
      const chip = document.createElement("span");
      chip.className = "chip file";
      chip.textContent = "📎 " + u.filename + " ";
      const x = document.createElement("button");
      x.type = "button"; x.className = "link"; x.textContent = "✕"; x.title = "Bỏ file";
      x.addEventListener("click", () => { pendingUploads.splice(i, 1); renderAttachments(); });
      chip.appendChild(x);
      attachList.appendChild(chip);
    });
  }

  attachBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const f = fileInput.files[0];
    fileInput.value = "";
    if (!f) return;
    attachBtn.disabled = true;
    try {
      const fd = new FormData();
      fd.append("file", f);
      const res = await (await Lab.api("/uploads", { method: "POST", body: fd })).json();
      pendingUploads.push({ upload_id: res.upload_id, filename: res.filename });
      renderAttachments();
      input.focus();
    } catch (e) {
      addMsg("assistant", '<span class="error">Không tải được file: ' + Lab.escapeHtml(e.message) + "</span>");
    } finally {
      attachBtn.disabled = false;
    }
  });

  // ---------------------------------------------------------------- thẻ hành động (backend sinh, LLM không tạo được)
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function summaryTable(rows) {
    const t = el("table", "summary");
    for (const [k, v] of rows || []) {
      const tr = el("tr");
      tr.appendChild(el("th", "", k));
      tr.appendChild(el("td", "", v));
      t.appendChild(tr);
    }
    return t;
  }

  function resultLine(card, ok, text) {
    let r = card.querySelector(".result");
    if (!r) { r = el("div", "result"); card.appendChild(r); }
    r.className = "result " + (ok ? "ok" : "error");
    r.textContent = text;
    return r;
  }

  function actionButtons(card, confirmLabel, onConfirm, onCancel) {
    const row = el("div", "row actions");
    const ok = el("button", "", confirmLabel);
    const no = el("button", "secondary", "Huỷ");
    const lock = (v) => { ok.disabled = v; no.disabled = v; };
    ok.addEventListener("click", async () => { lock(true); ok.textContent = "Đang xử lý…"; await onConfirm(lock, ok, confirmLabel); });
    no.addEventListener("click", async () => { lock(true); await onCancel(lock); });
    row.append(ok, no);
    card.appendChild(row);
    card._lock = lock;
  }

  function renderUiAction(a, container) {
    const card = el("div", "card-action");
    if (a.type === "download") {
      const link = el("a", "button-link", "⬇ " + (a.title || a.filename));
      link.href = a.url;
      link.setAttribute("download", a.filename || "");
      card.appendChild(link);
      container.appendChild(card);
      return;
    }
    if (a.type === "form_preview" || a.type === "form_confirm") {
      card.appendChild(el("div", "card-title", a.title + (a.version ? " — bản xem trước v" + a.version : " — đơn bạn đã gửi")));
      const url = a.url || ("/drafts/" + a.draft_id + "/preview");
      const isDocx = a.mime && a.mime.indexOf("pdf") < 0;
      const link = el("a", "", isDocx ? "Tải bản xem trước (DOCX)" : "Mở bản xem trước");
      link.href = url; link.target = "_blank"; link.rel = "noopener";
      card.appendChild(link);
      card.appendChild(summaryTable(a.summary));
      const prev = draftCards[a.draft_id];
      if (prev && prev._lock) {
        prev._lock(true);
        resultLine(prev, false, "Đã có bản mới hơn bên dưới.");
      }
      draftCards[a.draft_id] = card;
      actionButtons(card, a.type === "form_preview" ? "Xác nhận" : "Xác nhận nộp",
        async (lock, btn, label) => {
          try {
            const res = await Lab.api("/drafts/" + a.draft_id + "/confirm", { method: "POST", json: { shown_hash: a.shown_hash } });
            const r = await res.json();
            const line = resultLine(card, r.ok, r.message);
            if (r.ok && r.result && r.result.link) {
              const l = el("a", "", "Xem file đã lưu");
              l.href = r.result.link; l.target = "_blank"; l.rel = "noopener noreferrer";
              line.appendChild(l);
            }
            if (r.errors && r.errors.length) {
              const ul = el("ul");
              for (const e of r.errors) ul.appendChild(el("li", "", (e.label || e.field) + ": " + e.error));
              card.appendChild(ul);
              // trợ lý không thấy kết quả nút bấm: điền sẵn lỗi vào ô chat để người dùng gửi nhờ sửa
              input.value = "Đơn chưa xác nhận được vì: " + r.errors.map((e) => e.error).join(" ") + " Bạn sửa giúp mình rồi gửi bản xem trước mới nhé.";
              input.focus();
            } else if (!r.ok && r.status === "NEEDS_EDIT") {
              input.value = "Đơn chưa xác nhận được: " + r.message + " Bạn sửa giúp mình nhé.";
              input.focus();
            }
            btn.textContent = r.ok ? "Đã xác nhận" : label;
          } catch (e) {
            resultLine(card, false, e.message);
            btn.textContent = label;
            if (!e.status || e.status >= 500) lock(false);
          }
        },
        async () => {
          try {
            await Lab.api("/drafts/" + a.draft_id + "/cancel", { method: "POST" });
            resultLine(card, false, "Đã huỷ bản nháp.");
          } catch (e) { resultLine(card, false, e.message); }
        });
      container.appendChild(card);
      return;
    }
    if (a.type === "template_preview") {
      card.classList.add("template");
      card.appendChild(el("div", "card-title", "Mẫu đơn mới: " + a.title + " — bản xem trước v" + a.version));
      const links = el("div", "row links");
      const isDocx = a.mime && a.mime.indexOf("pdf") < 0;
      const lp = el("a", "", isDocx ? "Tải bản điền thử (DOCX)" : "Mở bản điền thử");
      lp.href = a.url; lp.target = "_blank"; lp.rel = "noopener";
      const lb = el("a", "", "Tải mẫu trắng" + (a.wiped ? " (đã tẩy " + a.wiped + " thông tin mẫu)" : ""));
      lb.href = a.blank_url; lb.rel = "noopener";
      links.append(lp, document.createTextNode(" · "), lb);
      card.appendChild(links);
      card.appendChild(summaryTable(a.summary));
      if (a.warnings && a.warnings.length) {
        const ul = el("ul", "warnings");
        for (const w of a.warnings) ul.appendChild(el("li", "", "⚠ " + w));
        card.appendChild(ul);
      }
      const key = "tpl:" + a.template_draft_id;
      const prev = draftCards[key];
      if (prev && prev._lock) {
        prev._lock(true);
        resultLine(prev, false, "Đã có bản mới hơn bên dưới.");
      }
      draftCards[key] = card;
      if (a.blocked) {
        resultLine(card, false, a.blocked);
      }
      actionButtons(card, "Kích hoạt",
        async (lock, btn, label) => {
          try {
            const res = await Lab.api("/admin/api/templates/" + a.template_draft_id + "/confirm", { method: "POST", json: { shown_hash: a.shown_hash } });
            const r = await res.json();
            const line = resultLine(card, r.ok, r.message);
            if (r.ok && r.result && r.result.link) {
              const l = el("a", "", " Xem mẫu trắng trên Drive");
              l.href = r.result.link; l.target = "_blank"; l.rel = "noopener noreferrer";
              line.appendChild(l);
            }
            if (!r.ok && r.status === "NEEDS_EDIT") {
              input.value = "Chưa kích hoạt được mẫu đơn: " + r.message + " Bạn sửa giúp mình nhé.";
              input.focus();
            }
            btn.textContent = r.ok ? "Đã kích hoạt" : label;
            if (!r.ok) lock(false);
          } catch (e) {
            resultLine(card, false, e.message);
            btn.textContent = label;
            if (!e.status || e.status >= 500) lock(false);
          }
        },
        async () => {
          try {
            await Lab.api("/admin/api/templates/" + a.template_draft_id + "/cancel", { method: "POST" });
            resultLine(card, false, "Đã huỷ bản duyệt mẫu đơn.");
          } catch (e) { resultLine(card, false, e.message); }
        });
      if (a.blocked) card._lock(true);
      container.appendChild(card);
      return;
    }
    if (a.type === "action_confirm") {
      card.appendChild(el("div", "card-title", a.title));
      const ul = el("ul");
      for (const line of a.summary || []) ul.appendChild(el("li", "", line));
      card.appendChild(ul);
      actionButtons(card, "Xác nhận",
        async (lock, btn, label) => {
          try {
            const r = await (await Lab.api("/actions/" + a.action_id + "/confirm", { method: "POST" })).json();
            resultLine(card, r.ok, r.message);
            btn.textContent = r.ok ? "Đã xác nhận" : label;
          } catch (e) {
            resultLine(card, false, e.message);
            btn.textContent = label;
            if (!e.status || e.status >= 500) lock(false);
          }
        },
        async () => {
          try {
            await Lab.api("/actions/" + a.action_id + "/cancel", { method: "POST" });
            resultLine(card, false, "Đã huỷ yêu cầu.");
          } catch (e) { resultLine(card, false, e.message); }
        });
      container.appendChild(card);
    }
  }

  async function send(text) {
    busy = true;
    sendBtn.disabled = true;
    $("suggestions").textContent = "";
    if (pendingUploads.length) {
      const lines = pendingUploads.map((u) => "[Tệp đã tải lên: " + u.filename + " | upload_id: " + u.upload_id + "]");
      text = (text ? text + "\n" : "") + lines.join("\n");
      pendingUploads = [];
      renderAttachments();
    }
    addMsg("user", Lab.escapeHtml(text).replace(/\n/g, "<br>"));
    const bubble = addMsg("assistant", '<span class="typing">…</span>');
    const status = document.createElement("div");
    status.className = "status";
    let answer = "";
    try {
      const res = await Lab.api("/chat", { method: "POST", json: { message: text, conversation_id: conversationId } });
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const dataLine = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!dataLine) continue;
          const ev = JSON.parse(dataLine.slice(6));
          if (ev.type === "conversation") {
            conversationId = ev.id;
            sessionStorage.setItem("la_conv", ev.id);
          } else if (ev.type === "text") {
            answer += ev.text;
            bubble.innerHTML = Lab.md(answer);
          } else if (ev.type === "reset") {
            answer = "";
            bubble.innerHTML = '<span class="typing">…</span>';
          } else if (ev.type === "tool") {
            if (ev.status === "start") {
              status.textContent = TOOL_LABEL[ev.name] || "Đang xử lý…";
              bubble.parentNode.appendChild(status);
            } else {
              status.remove();
            }
          } else if (ev.type === "ui_action") {
            renderUiAction(ev.action, bubble.parentNode);
          } else if (ev.type === "error") {
            bubble.innerHTML = '<span class="error">' + Lab.escapeHtml(ev.message) + "</span>";
          } else if (ev.type === "done" && ev.degraded) {
            status.textContent = "Đang dùng mô hình dự phòng.";
            bubble.parentNode.appendChild(status);
          }
          msgs.scrollTop = msgs.scrollHeight;
        }
      }
      if (!answer && !bubble.querySelector(".error")) bubble.innerHTML = "<em>(không có câu trả lời)</em>";
    } catch (e) {
      bubble.innerHTML = '<span class="error">' + Lab.escapeHtml(e.message) + "</span>";
      if (e.status === 403 && /đổi mật khẩu/i.test(e.message)) location.href = "/login#change";
    } finally {
      busy = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if ((!text && !pendingUploads.length) || busy) return;
    input.value = "";
    input.style.height = "";
    send(text);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  });
  $("new-chat").addEventListener("click", () => {
    conversationId = null;
    sessionStorage.removeItem("la_conv");
    msgs.querySelectorAll(".msg:not(.intro)").forEach((n) => n.remove());
  });
  $("logout-btn").addEventListener("click", async () => {
    await Lab.api("/auth/logout", { method: "POST" });
    sessionStorage.removeItem("la_conv");
    location.reload();
  });

  async function loadHistory() {
    if (!conversationId) return;
    try {
      const data = await (await Lab.api("/chat/" + conversationId + "/history")).json();
      for (const m of data.messages) {
        addMsg(m.role, m.role === "user" ? Lab.escapeHtml(m.text).replace(/\n/g, "<br>") : Lab.md(m.text));
      }
    } catch (_) {
      conversationId = null;
      sessionStorage.removeItem("la_conv");
    }
  }

  (async function init() {
    try {
      const me = await Lab.me();
      $("lab-name").textContent = me.lab_name;
      document.title = "Trợ lý " + me.lab_name.split(" - ")[0];
      const labels = { guest: "Khách", member: "Thành viên", thu_ki: "Thư ký", leader: "Leader", admin: "Admin" };
      $("who-label").textContent = (me.ho_ten ? me.ho_ten + " · " : "") + (labels[me.role] || me.role);
      $("login-link").hidden = me.logged_in || me.auth_bypass;
      $("logout-btn").hidden = !me.logged_in || me.auth_bypass;
      $("admin-link").hidden = me.role !== "admin" && me.role !== "leader";
      if (me.notice) {
        // tài khoản không còn trong Members: session đã bị huỷ, đang dùng với tư cách khách
        sessionStorage.removeItem("la_conv");
        addMsg("assistant", '<span class="error">' + Lab.escapeHtml(me.notice) + "</span>");
      }
      $("test-banner").hidden = !me.test_mode;
      if (me.auth_bypass) {
        // chế độ dev cô lập auth: đổi vai trò ngay, không cần đăng nhập
        const sel = $("dev-role");
        sel.hidden = false;
        sel.value = me.role;
        $("test-banner").textContent = "Chế độ TEST · bỏ qua đăng nhập: chọn vai trò ở góc trên để thử.";
        sel.addEventListener("change", async () => {
          await Lab.api("/auth/dev-role", { method: "POST", json: { role: sel.value } });
          sessionStorage.removeItem("la_conv");
          location.reload();
        });
      }
      if (me.must_change_password) { location.href = "/login#change"; return; }
      setSuggestions(me.role);
      await loadHistory();
    } catch (e) {
      addMsg("assistant", '<span class="error">Không kết nối được máy chủ.</span>');
    }
  })();
})();
