"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const msg = (t, ok) => { $("msg").textContent = t || ""; $("msg").className = ok ? "ok" : "error"; };

  function showChange() {
    $("login-form").hidden = true;
    $("change-form").hidden = false;
    $("dev-login").hidden = true;
  }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    msg("");
    try {
      const r = await (await Lab.api("/auth/login", { method: "POST",
        json: { email: $("email").value, password: $("password").value } })).json();
      if (r.must_change_password) { $("old-password").value = $("password").value; showChange(); }
      else location.href = "/";
    } catch (err) { msg(err.message); }
  });

  $("change-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("new-password").value !== $("new-password2").value) { msg("Hai mật khẩu mới không khớp."); return; }
    try {
      await Lab.api("/auth/change-password", { method: "POST",
        json: { old_password: $("old-password").value, new_password: $("new-password").value } });
      msg("Đã đổi mật khẩu.", true);
      setTimeout(() => { location.href = "/"; }, 700);
    } catch (err) { msg(err.message); }
  });

  $("dev-btn").addEventListener("click", async () => {
    try {
      await Lab.api("/auth/dev-login", { method: "POST", json: { name: $("dev-name").value } });
      location.href = "/";
    } catch (err) { msg(err.message); }
  });

  (async function init() {
    const me = await Lab.me();
    $("lab-name").textContent = me.lab_name;
    if (me.logged_in && me.must_change_password) showChange();
    else if (location.hash === "#change" && me.logged_in) showChange();
    if (me.dev_login) {
      try {
        const data = await (await Lab.api("/auth/dev-login")).json();
        const sel = $("dev-name");
        for (const m of data.members) {
          const o = document.createElement("option");
          o.value = m.ho_ten;
          o.textContent = m.ho_ten + " (" + m.role + ")";
          sel.appendChild(o);
        }
        $("dev-login").hidden = false;
      } catch (err) { msg("Không tải được danh sách thành viên: " + err.message); }
    }
  })();
})();
