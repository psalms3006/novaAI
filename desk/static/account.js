/* NOVA account surface for the desktop shell.
 *
 * Deliberately quiet. If no NOVA Cloud backend is configured this file does
 * nothing at all and NOVA behaves exactly as it did before -- local-first is
 * the default, not a degraded mode.
 *
 * When a backend *is* configured and nobody is signed in, the gate appears
 * once, before the main UI. After that NOVA restores the session from the OS
 * keystore and goes straight to voice-ready: the user is never asked to sign
 * in again just because they reopened the app, and never asked at all while
 * they are talking to NOVA.
 */
(function () {
  "use strict";

  var state = null;
  var gate = null;

  function token() { return window.DESK_TOKEN || ""; }

  function api(path, opts) {
    opts = opts || {};
    return fetch(path, {
      method: opts.method || "GET",
      headers: {
        "Content-Type": "application/json",
        "X-Desk-Token": token()
      },
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw Object.assign(new Error(j.message || "Request failed"),
                                       { code: j.error });
        return j;
      });
    });
  }

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "class") n.className = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) {
      if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }

  /* -- the sign-in gate -------------------------------------------------- */

  function buildGate() {
    var mode = "signin";
    var err = el("p", { class: "acct-error", hidden: "hidden" });
    var email = el("input", { type: "email", placeholder: "you@example.com",
                              autocomplete: "username" });
    var pass = el("input", { type: "password", placeholder: "Password",
                             autocomplete: "current-password" });
    var name = el("input", { type: "text", placeholder: "What should NOVA call you?",
                             autocomplete: "name", hidden: "hidden" });
    var submit = el("button", { class: "acct-primary", text: "Sign in" });
    var toggle = el("button", { class: "acct-link",
                                text: "Create an account instead" });
    var forgot = el("button", { class: "acct-link", text: "Forgot password" });
    var note = el("p", { class: "acct-note" });

    function setMode(next) {
      mode = next;
      var up = mode === "signup";
      name.hidden = !up;
      submit.textContent = up ? "Create account" : "Sign in";
      toggle.textContent = up ? "I already have an account"
                              : "Create an account instead";
      pass.setAttribute("autocomplete", up ? "new-password" : "current-password");
      forgot.hidden = up;
      note.textContent = up
        ? "Your account carries your preferences between devices. Conversations "
        + "and local models stay on this machine."
        : "";
      err.hidden = true;
    }

    function fail(message) {
      err.textContent = message;
      err.hidden = false;
      submit.disabled = false;
    }

    submit.addEventListener("click", function () {
      err.hidden = true;
      submit.disabled = true;
      var path = mode === "signup" ? "/api/account/signup" : "/api/account/signin";
      api(path, { method: "POST", body: {
        email: email.value.trim(), password: pass.value,
        display_name: name.value.trim()
      } }).then(function (j) {
        state = j;
        dismiss();
      }).catch(function (e) { fail(e.message || "Could not sign in."); });
    });

    pass.addEventListener("keydown", function (e) {
      if (e.key === "Enter") submit.click();
    });

    toggle.addEventListener("click", function () {
      setMode(mode === "signup" ? "signin" : "signup");
    });

    forgot.addEventListener("click", function () {
      var addr = email.value.trim();
      if (!addr) { fail("Enter your email address first."); return; }
      api("/api/account/password/forgot", { method: "POST", body: { email: addr } })
        .then(function (j) {
          err.textContent = j.message || "Check your email for a reset link.";
          err.className = "acct-error acct-info";
          err.hidden = false;
        })
        .catch(function () { fail("Could not send a reset link."); });
    });

    setMode("signin");

    return el("div", { class: "acct-gate", id: "acct-gate" }, [
      el("div", { class: "acct-card" }, [
        el("div", { class: "acct-brand", text: "NOVA" }),
        el("p", { class: "acct-sub", text: "Sign in to carry your NOVA between devices." }),
        email, pass, name, submit, err, note,
        el("div", { class: "acct-links" }, [toggle, forgot])
      ])
    ]);
  }

  function present() {
    if (gate) return;
    gate = buildGate();
    document.body.appendChild(gate);
  }

  function dismiss() {
    if (!gate) return;
    gate.remove();
    gate = null;
    window.dispatchEvent(new CustomEvent("nova-account-ready", { detail: state }));
  }

  /* -- account panel (settings) ------------------------------------------ */

  function renderPanel(host) {
    if (!host) return;
    host.textContent = "";
    if (!state || !state.configured) {
      host.appendChild(el("p", { class: "acct-note",
        text: "This NOVA runs entirely on this computer. No account is in use." }));
      return;
    }
    if (!state.signed_in) {
      host.appendChild(el("button", { class: "acct-primary", text: "Sign in",
                                      onclick: present }));
      return;
    }

    var u = state.user || {};
    host.appendChild(el("dl", { class: "acct-kv" }, [
      el("dt", { text: "Signed in as" }), el("dd", { text: u.email || "—" }),
      el("dt", { text: "Name" }), el("dd", { text: state.display_name || "—" }),
      el("dt", { text: "Connection" }),
      el("dd", { text: state.online ? "Online" : "Offline (using cached session)" }),
      el("dt", { text: "Credentials stored in" }),
      el("dd", { text: state.hardware_backed
                       ? state.credential_backend
                       : state.credential_backend + " (no OS keystore available)" })
    ]));

    var devices = el("div", { class: "acct-devices" });
    host.appendChild(el("h4", { text: "Your devices" }));
    host.appendChild(devices);
    api("/api/account/devices").then(function (j) {
      devices.textContent = "";
      (j.devices || []).forEach(function (d) {
        devices.appendChild(el("div", { class: "acct-device" }, [
          el("div", {}, [
            el("strong", { text: d.name + (d.current ? " (this device)" : "") }),
            el("span", { class: "acct-dim",
                         text: " " + d.platform + " · " + (d.app_version || "?") })
          ]),
          d.current ? el("span", { class: "acct-dim", text: "active now" })
                    : el("button", { class: "acct-link", text: "Sign out this device",
                        onclick: function () {
                          if (!window.confirm("Sign " + d.name + " out?")) return;
                          api("/api/account/devices/" + d.id + "/revoke",
                              { method: "POST" }).then(function () { renderPanel(host); });
                        } })
        ]));
      });
      if (!(j.devices || []).length) {
        devices.appendChild(el("p", { class: "acct-note", text: "No devices listed." }));
      }
    }).catch(function () {
      devices.textContent = "";
      devices.appendChild(el("p", { class: "acct-note",
        text: "Device list needs a connection." }));
    });

    host.appendChild(el("div", { class: "acct-links" }, [
      el("button", { class: "acct-link", text: "Sync preferences now",
        onclick: function () { api("/api/account/sync", { method: "POST" }); } }),
      el("button", { class: "acct-link", text: "Sign out",
        onclick: function () {
          api("/api/account/signout", { method: "POST" }).then(function (j) {
            state = j;
            renderPanel(host);
          });
        } })
    ]));
  }

  /* -- boot -------------------------------------------------------------- */

  function refresh() {
    return api("/api/account").then(function (j) {
      state = j;
      window.NOVA_ACCOUNT = j;
      if (j.configured && !j.signed_in) present();
      return j;
    }).catch(function () {
      // The account surface being unavailable must never stop NOVA loading.
      state = { configured: false, signed_in: false };
      window.NOVA_ACCOUNT = state;
      return state;
    });
  }

  window.NovaAccount = {
    refresh: refresh,
    present: present,
    renderPanel: renderPanel,
    get state() { return state; }
  };

  document.addEventListener("DOMContentLoaded", function () { refresh(); });
})();
