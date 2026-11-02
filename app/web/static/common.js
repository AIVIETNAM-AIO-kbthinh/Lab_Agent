"use strict";

window.Lab = (function () {
  function cookie(name) {
    const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return m ? decodeURIComponent(m[1]) : "";
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({ "X-CSRF-Token": cookie("la_csrf") }, opts.headers || {});
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    const res = await fetch(path, Object.assign({}, opts, { headers, credentials: "same-origin" }));
    if (!res.ok) {
      let detail = "Lỗi " + res.status;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* không phải JSON */ }
      const err = new Error(detail);
      err.status = res.status;
      throw err;
    }
    return res;
  }

  async function me() {
    // gọi một GET trước để chắc chắn đã có session + cookie CSRF
    return (await api("/auth/me")).json();
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // markdown tối giản, AN TOÀN: escape trước, chỉ cho phép vài định dạng
  function md(text) {
    const lines = escapeHtml(text).split("\n");
    let html = "", inList = false;
    for (const raw of lines) {
      let line = raw
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
        .replace(/_\(([^)]+)\)_/g, "<em>($1)</em>")
        .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
        .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
      const li = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
      if (li) {
        if (!inList) { html += "<ul>"; inList = true; }
        html += "<li>" + li[1] + "</li>";
        continue;
      }
      if (inList) { html += "</ul>"; inList = false; }
      const h = line.match(/^#{1,4}\s+(.*)$/);
      if (h) { html += "<p><strong>" + h[1] + "</strong></p>"; continue; }
      html += line.trim() ? "<p>" + line + "</p>" : "";
    }
    if (inList) html += "</ul>";
    return html;
  }

  return { cookie, api, me, md, escapeHtml };
})();
