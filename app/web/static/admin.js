"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => Lab.escapeHtml(v === null || v === undefined ? "" : String(v));
  const dt = (v) => (v ? new Date(v).toLocaleString("vi-VN") : "");
  const msg = (t, ok) => { $("msg").textContent = t || ""; $("msg").className = ok ? "ok" : "error"; };

  function cell(tr, html) { const td = document.createElement("td"); td.innerHTML = html; tr.appendChild(td); return td; }

  async function loadCosts() {
    const c = await (await Lab.api("/admin/api/llm/costs")).json();
    $("costs").innerHTML =
      `<div><b>${c.month_total_usd.toFixed(2)} USD</b><span>tháng này</span></div>` +
      `<div><b>${c.today_usd.toFixed(2)} USD</b><span>hôm nay</span></div>` +
      `<div><b>${c.p95_first_token_ms_7d ?? "—"} ms</b><span>p95 token đầu (7 ngày)</span></div>` +
      `<div><b>${c.alert_usd} / ${c.hard_stop_usd} USD</b><span>ngưỡng cảnh báo / dừng</span></div>`;
    const tb = $("cost-rows");
    tb.textContent = "";
    for (const r of c.month) {
      const tr = document.createElement("tr");
      for (const v of [r.profile, r.model, r.calls, r.input, r.output, r.cache_read, r.cost_usd, r.avg_first_token_ms]) cell(tr, esc(v));
      tb.appendChild(tr);
    }
  }

  async function loadWeb() {
    const f = $("filter").value;
    const d = await (await Lab.api("/admin/api/kb/web" + (f ? "?status=" + encodeURIComponent(f) : ""))).json();
    const runs = $("runs");
    runs.textContent = "";
    for (const r of d.runs) {
      const tr = document.createElement("tr");
      const s = r.stats || {};
      cell(tr, esc(r.id)); cell(tr, esc(r.mode)); cell(tr, `<span class="st st-${esc(r.status)}">${esc(r.status)}</span>`);
      cell(tr, esc(dt(r.started_at)));
      cell(tr, esc(`tải ${s.fetched ?? 0} · mới ${s.new ?? 0} · đổi ${s.changed ?? 0} · mất ${s.missing ?? 0} · xoá ${s.deleted ?? 0}` +
        (s.reason ? " — " + s.reason : "") + (r.error ? " — " + r.error : "")));
      const act = cell(tr, "");
      if (r.status === "BLOCKED") {
        const b = document.createElement("button");
        b.textContent = "Áp dụng";
        b.addEventListener("click", async () => {
          if (!confirm("Áp dụng kế hoạch của lần chạy #" + r.id + "? Các trang trong kế hoạch sẽ bị thay/xoá khỏi KB.")) return;
          try { await Lab.api(`/admin/api/kb/web/runs/${r.id}/apply`, { method: "POST" }); msg("Đã xếp hàng áp dụng.", true); }
          catch (e) { msg(e.message); }
        });
        act.appendChild(b);
      }
      runs.appendChild(tr);
    }
    const pages = $("pages");
    pages.textContent = "";
    $("page-count").textContent = "(" + d.pages.length + ")";
    for (const p of d.pages) {
      const tr = document.createElement("tr");
      cell(tr, `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.url.replace(/^https?:\/\//, ""))}</a>`);
      for (const v of [p.lang, p.title, p.status, dt(p.last_fetched), dt(p.last_changed), p.missing_count, p.chunks]) cell(tr, esc(v));
      pages.appendChild(tr);
    }
  }

  async function loadForms() {
    const d = await (await Lab.api("/admin/api/templates")).json();
    const tb = $("forms");
    tb.textContent = "";
    for (const f of d.forms) {
      const tr = document.createElement("tr");
      for (const v of [f.form_id, f.title, f.version, f.status + (f.hook ? " · xử lý riêng: " + f.hook : ""), (f.roles || []).join(", "), f.fields]) cell(tr, esc(v));
      const act = cell(tr, "");
      if (f.status === "active") {
        const b = document.createElement("button");
        b.className = "secondary small";
        b.textContent = "Ngừng dùng";
        b.addEventListener("click", async () => {
          if (!confirm("Ngừng dùng mẫu '" + f.title + "'? Trợ lý sẽ không giới thiệu đơn này nữa; đơn đã nộp không ảnh hưởng.")) return;
          try { await Lab.api("/admin/api/templates/forms/" + encodeURIComponent(f.form_id) + "/deactivate", { method: "POST" }); msg("Đã ngừng dùng.", true); loadForms(); }
          catch (e) { msg(e.message); }
        });
        act.appendChild(b);
      }
      tb.appendChild(tr);
    }
    if (!d.forms.length) { const tr = document.createElement("tr"); cell(tr, "Chưa có mẫu đơn").colSpan = 7; tb.appendChild(tr); }
    $("pending-templates").textContent = d.pending.length
      ? "Đang chờ duyệt trong chat: " + d.pending.map((p) => p.title + " (" + p.form_id + ")").join(", ")
      : "";
  }

  async function loadOps() {
    const d = await (await Lab.api("/admin/api/ops?status=FAILED")).json();
    const tb = $("ops");
    tb.textContent = "";
    for (const o of d.ops) {
      const tr = document.createElement("tr");
      for (const v of [o.id, o.kind, o.attempts, o.actor, o.error]) cell(tr, esc(v));
      tb.appendChild(tr);
    }
    if (!d.ops.length) { const tr = document.createElement("tr"); cell(tr, "Không có").colSpan = 5; tb.appendChild(tr); }
  }

  async function loadAccounts() {
    const d = await (await Lab.api("/admin/api/accounts")).json();
    const tb = $("accounts");
    tb.textContent = "";
    for (const a of d.accounts) {
      const tr = document.createElement("tr");
      cell(tr, esc(a.email));
      cell(tr, a.valid ? esc(a.who) : `<span class="error">${esc(a.who)}</span>`);
      cell(tr, esc([a.disabled && "đã khoá", a.locked && "khoá tạm", a.must_change_password && "chưa đổi mật khẩu"].filter(Boolean).join(", ") || "hoạt động"));
      cell(tr, esc(dt(a.last_login_at)));
      const act = cell(tr, "");
      for (const [action, label] of [["reset", "Đặt lại mật khẩu"], [a.disabled ? "enable" : "disable", a.disabled ? "Mở" : "Khoá"], ["unlock", "Mở khoá tạm"], ["delete", "Xoá"]]) {
        const b = document.createElement("button");
        b.className = "secondary small";
        b.textContent = label;
        b.addEventListener("click", async () => {
          if (action === "delete" && !confirm("Xoá hẳn tài khoản " + a.email + "? Mọi phiên đăng nhập bị huỷ; AuditLog vẫn giữ email.")) return;
          try {
            const r = await (await Lab.api("/admin/api/accounts/action", { method: "POST", json: { email: a.email, action } })).json();
            msg(r.temp_password ? `Mật khẩu tạm của ${a.email}: ${r.temp_password} (gửi mail thất bại)` : "Đã thực hiện.", true);
            loadAccounts();
          } catch (e) { msg(e.message); }
        });
        act.appendChild(b);
      }
      tb.appendChild(tr);
    }
  }

  async function loadAudit() {
    const q = $("audit-q").value.trim();
    const d = await (await Lab.api("/admin/api/audit?limit=200" + (q ? "&q=" + encodeURIComponent(q) : ""))).json();
    const tb = $("audit");
    tb.textContent = "";
    for (const r of d.rows) {
      const tr = document.createElement("tr");
      for (const v of [r.thoi_gian, r.actor, r.role, r.action, r.target, r.ket_qua, r.chi_tiet]) cell(tr, esc(v));
      tb.appendChild(tr);
    }
    if (!d.rows.length) { const tr = document.createElement("tr"); cell(tr, "Không có").colSpan = 7; tb.appendChild(tr); }
  }

  async function crawl(mode) {
    try { await Lab.api("/admin/api/kb/web/crawl", { method: "POST", json: { mode } }); msg("Đã xếp hàng crawl, worker sẽ chạy trong giây lát.", true); }
    catch (e) { msg(e.message); }
  }

  $("crawl-quick").addEventListener("click", () => crawl("quick"));
  $("crawl-full").addEventListener("click", () => crawl("full"));
  $("filter").addEventListener("change", loadWeb);
  $("audit-form").addEventListener("submit", (e) => { e.preventDefault(); loadAudit().catch((err) => msg(err.message)); });
  $("acc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await (await Lab.api("/admin/api/accounts", { method: "POST", json: { email: $("acc-email").value } })).json();
      msg(r.mailed ? `Đã tạo và gửi mật khẩu tạm tới ${r.email}.` : `Đã tạo. Mật khẩu tạm: ${r.temp_password} (gửi mail thất bại)`, true);
      $("acc-email").value = "";
      loadAccounts();
    } catch (err) { msg(err.message); }
  });

  (async function init() {
    try {
      const me = await Lab.me();
      if (me.role !== "admin" && me.role !== "leader") { msg("Chỉ leader hoặc admin được truy cập trang này."); return; }
      const show = (sel) => document.querySelectorAll(sel).forEach((el) => { el.hidden = false; });
      show(".leader-admin");
      if (me.role === "leader") { await loadForms(); return; }  // leader chỉ quản lý mẫu đơn
      show(".admin-only");
      await Promise.all([loadCosts(), loadWeb(), loadForms(), loadOps(), loadAccounts(), loadAudit()]);
    } catch (e) { msg(e.message); }
  })();
})();
