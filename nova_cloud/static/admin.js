/* NOVA admin control plane.
 *
 * Every view here is backed by a real endpoint. Sections the signed-in role
 * cannot access are not rendered at all, rather than shown and then failing --
 * and nothing on this page displays a number the backend did not return.
 *
 * The token is kept in memory only. A refresh signs the operator out, which is
 * the right trade for a privileged console.
 */
(function () {
  "use strict";

  var token = null;
  var me = null;
  var perms = [];
  var current = "dashboard";

  var $ = function (id) { return document.getElementById(id); };

  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    return fetch("/admin/api" + path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.status === 401 && token) { signOut(); }
        if (!r.ok) throw Object.assign(new Error(j.message || r.statusText),
                                       { code: j.error, status: r.status });
        return j;
      });
    });
  }

  function can(p) { return perms.indexOf(p) !== -1; }

  /* -- formatting -------------------------------------------------------- */

  function ago(ts) {
    if (!ts) return "never";
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }

  function stamp(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    return d.toISOString().replace("T", " ").slice(0, 19);
  }

  function num(v) {
    if (v === null || v === undefined) return "—";
    return typeof v === "number" ? v.toLocaleString() : String(v);
  }

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      if (k === "class") n.className = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) {
      if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }

  function table(columns, rows, rowFn) {
    if (!rows.length) return el("div", { class: "empty", text: "Nothing recorded yet." });
    var thead = el("tr", {}, columns.map(function (c) { return el("th", { text: c }); }));
    var tbody = el("tbody", {}, rows.map(rowFn));
    return el("div", { class: "table-wrap" }, [
      el("table", {}, [el("thead", {}, [thead]), tbody])
    ]);
  }

  function card(label, value, tone) {
    return el("div", { class: "card" }, [
      el("div", { class: "label", text: label }),
      el("div", { class: "value" + (tone ? " " + tone : ""), text: num(value) })
    ]);
  }

  function pill(text, tone) { return el("span", { class: "pill " + tone, text: text }); }

  /* -- navigation -------------------------------------------------------- */

  var SECTIONS = [
    { id: "dashboard", label: "Dashboard", perm: "dashboard.view" },
    { id: "fleet", label: "Instances & updates", perm: "dashboard.view" },
    { id: "users", label: "Users", perm: "users.view" },
    { id: "devices", label: "Devices", perm: "devices.view" },
    { id: "activity", label: "Activity", perm: "activity.view" },
    { id: "agents", label: "Agents", perm: "agents.view" },
    { id: "models", label: "Models", perm: "models.view" },
    { id: "health", label: "System health", perm: "health.view" },
    { id: "errors", label: "Errors", perm: "errors.view" },
    { id: "flags", label: "Feature flags", perm: "flags.view" },
    { id: "audit", label: "Admin audit log", perm: "audit.view" },
    { id: "admins", label: "Administrators", perm: "admins.view" }
  ];

  function buildNav() {
    var nav = $("nav");
    nav.textContent = "";
    SECTIONS.filter(function (s) { return can(s.perm); }).forEach(function (s) {
      nav.appendChild(el("a", {
        text: s.label,
        class: s.id === current ? "active" : "",
        onclick: function () { go(s.id); }
      }));
    });
  }

  function go(id) {
    current = id;
    buildNav();
    render();
  }

  function render(extra) {
    var content = $("content");
    $("loading").hidden = false;
    var fn = VIEWS[current];
    if (!fn) { $("loading").hidden = true; return; }
    fn(extra).then(function (node) {
      content.textContent = "";
      content.appendChild(node);
      $("loading").hidden = true;
    }).catch(function (e) {
      content.textContent = "";
      content.appendChild(el("div", { class: "forbidden",
        text: e.status === 403 ? "Your role cannot view this section." : (e.message || "Failed to load.") }));
      $("loading").hidden = true;
    });
  }

  function header(title, sub) {
    return [el("h1", { text: title }), el("p", { class: "sub", text: sub })];
  }

  /* -- views ------------------------------------------------------------- */

  var VIEWS = {};

  VIEWS.dashboard = function () {
    return api("/dashboard").then(function (j) {
      var m = j.metrics;
      var errTone = m.model_error_rate_24h > 0.1 ? "bad"
                  : (m.model_error_rate_24h > 0.02 ? "warn" : "");
      return el("div", {}, header("Dashboard", "Live counts from the platform database. Last 24 hours unless stated.").concat([
        el("div", { class: "section-title", text: "Accounts" }),
        el("div", { class: "cards" }, [
          card("Total users", m.users_total),
          card("New (24h)", m.users_new_24h),
          card("New (7d)", m.users_new_7d),
          card("Active (24h)", m.users_active_24h),
          card("Active (7d)", m.users_active_7d),
          card("Suspended", m.users_suspended),
          card("Disabled", m.users_disabled)
        ]),
        el("div", { class: "section-title", text: "Devices and sessions" }),
        el("div", { class: "cards" }, [
          card("Active devices", m.devices_total),
          card("Seen (24h)", m.devices_seen_24h),
          card("Revoked", m.devices_revoked),
          card("Open sessions", m.sessions_active),
          card("Voice sessions (24h)", m.voice_sessions_24h)
        ]),
        el("div", { class: "section-title", text: "Runtime" }),
        el("div", { class: "cards" }, [
          card("Model requests (24h)", m.model_calls_24h),
          card("Failed requests", m.model_calls_failed_24h, m.model_calls_failed_24h ? "warn" : ""),
          card("Error rate", (m.model_error_rate_24h * 100).toFixed(1) + "%", errTone),
          card("Avg latency", m.avg_latency_ms_24h ? m.avg_latency_ms_24h + " ms" : "—"),
          card("Tasks started", m.tasks_24h),
          card("Tasks failed", m.tasks_failed_24h, m.tasks_failed_24h ? "warn" : ""),
          card("Agent runs", m.agent_runs_24h),
          card("Errors", m.errors_24h, m.errors_24h ? "warn" : ""),
          card("Offline fallbacks", m.offline_fallbacks_24h),
          card("Network degraded", m.network_degraded_24h)
        ])
      ]));
    });
  };

  VIEWS.fleet = function () {
    return api("/fleet").then(function (j) {
      var f = j.fleet;
      var kids = header("Instances & updates",
        "Each account's NOVA, the app versions in the field, model usage today and update rollout. Counts only.");
      kids.push(el("div", { class: "section-title", text: "Instances" }));
      kids.push(el("div", { class: "cards" }, [
        card("Instances", f.instances_total),
        card("Finished setup", f.instances_onboarded),
        card("Online now", f.instances_online),
        card("Offline", f.instances_offline)
      ]));
      kids.push(el("div", { class: "section-title", text: "App versions in use" }));
      kids.push(table(["Version", "Devices"], f.app_versions, function (v) {
        return el("tr", {}, [el("td", { class: "mono", text: v.version }), el("td", { text: num(v.devices) })]);
      }));
      kids.push(el("div", { class: "section-title", text: "Model usage today (UTC)" }));
      kids.push(table(["Kind", "Requests", "Input tokens", "Output tokens"], f.model_usage_today, function (u) {
        return el("tr", {}, [el("td", { text: u.kind }), el("td", { text: num(u.requests) }),
          el("td", { text: num(u.input_tokens) }), el("td", { text: num(u.output_tokens) })]);
      }));
      kids.push(el("div", { class: "section-title", text: "Published releases" }));
      kids.push(table(["Channel", "Version", "Min supported", "Rollout", "Published"], f.releases, function (r) {
        return el("tr", {}, [el("td", { text: r.channel }), el("td", { class: "mono", text: r.version }),
          el("td", { class: "mono", text: r.min_supported }), el("td", { text: r.rollout_percent + "%" }),
          el("td", { class: "mono", text: stamp(r.published_at) })]);
      }));
      kids.push(el("div", { class: "section-title", text: "Update results" }));
      kids.push(table(["Result", "Devices"], f.update_results, function (r) {
        return el("tr", {}, [el("td", {}, [pill(r.result, r.result === "installed" ? "ok"
          : (r.result === "failed" || r.result === "rolled_back" ? "bad" : ""))]), el("td", { text: num(r.devices) })]);
      }));
      if (f.recent_update_failures.length) {
        kids.push(el("div", { class: "section-title", text: "Recent update failures" }));
        kids.push(table(["Device", "Target", "Result", "Error", "When"], f.recent_update_failures, function (x) {
          return el("tr", {}, [el("td", { class: "mono", text: x.device_id }), el("td", { class: "mono", text: x.target || "—" }),
            el("td", { text: x.result }), el("td", { text: x.error || "—" }), el("td", { text: ago(x.at) })]);
        }));
      }
      return el("div", {}, kids);
    });
  };

  VIEWS.users = function () {
    var q = "";
    var wrap = el("div", {});
    var results = el("div", {});

    function load() {
      return api("/users?limit=100&q=" + encodeURIComponent(q)).then(function (j) {
        results.textContent = "";
        results.appendChild(table(
          ["Email", "Name", "Status", "Devices", "Created", "Last active"],
          j.users,
          function (u) {
            return el("tr", {}, [
              el("td", {}, [el("a", { class: "row-link", text: u.email,
                onclick: function () { render({ userId: u.id }); } })]),
              el("td", { text: u.display_name || "—" }),
              el("td", {}, [pill(u.status, u.status === "active" ? "ok" : "bad")]),
              el("td", { text: num(u.devices) }),
              el("td", { class: "mono", text: stamp(u.created_at) }),
              el("td", { text: ago(u.last_active_at) })
            ]);
          }
        ));
        results.appendChild(el("p", { class: "sub",
          text: j.total + " account" + (j.total === 1 ? "" : "s") + " total." }));
      });
    }

    var search = el("input", { type: "search", placeholder: "Search email, name, account ID or device ID…",
      oninput: function (e) { q = e.target.value; clearTimeout(load._t); load._t = setTimeout(load, 250); } });

    header("Users", "Identity and operational metadata. Conversation content is not accessible here.")
      .forEach(function (n) { wrap.appendChild(n); });
    wrap.appendChild(el("div", { class: "toolbar" }, [search]));
    wrap.appendChild(results);
    return load().then(function () { return wrap; });
  };

  VIEWS.users.detail = true;

  function userDetail(userId) {
    return api("/users/" + userId).then(function (j) {
      var u = j.user;
      var wrap = el("div", {}, [
        el("a", { class: "back", text: "← All users", onclick: function () { go("users"); } })
      ]);
      header(u.email, u.display_name || "No display name")
        .forEach(function (n) { wrap.appendChild(n); });

      wrap.appendChild(el("div", { class: "notice", text: j.note }));

      wrap.appendChild(el("dl", { class: "kv" }, [
        el("dt", { text: "Account ID" }), el("dd", { class: "mono", text: u.id }),
        el("dt", { text: "Status" }), el("dd", {}, [pill(u.status, u.status === "active" ? "ok" : "bad")]),
        el("dt", { text: "Email verified" }), el("dd", { text: u.email_verified ? "Yes" : "No" }),
        el("dt", { text: "Created" }), el("dd", { text: stamp(u.created_at) }),
        el("dt", { text: "Last active" }), el("dd", { text: ago(u.last_active_at) }),
        el("dt", { text: "Active sessions" }), el("dd", { text: num(j.active_sessions) }),
        el("dt", { text: "Model requests" }), el("dd", { text: num(j.model_calls_total) })
      ]));

      var actions = el("div", { class: "actions" });
      if (can("users.disable") && u.status === "active") {
        actions.appendChild(el("button", { class: "ghost", text: "Suspend account",
          onclick: function () { confirmThen("Suspend " + u.email + "? They will be signed out until reactivated.",
            function () { return api("/users/" + u.id + "/status", { method: "POST", body: { status: "suspended" } }); },
            function () { render({ userId: u.id }); }); } }));
        actions.appendChild(el("button", { class: "danger", text: "Disable account",
          onclick: function () { confirmThen("Disable " + u.email + "? They will be signed out on every device.",
            function () { return api("/users/" + u.id + "/status", { method: "POST", body: { status: "disabled" } }); },
            function () { render({ userId: u.id }); }); } }));
      }
      if (can("users.enable") && u.status !== "active") {
        actions.appendChild(el("button", { text: "Enable account",
          onclick: function () { api("/users/" + u.id + "/status", { method: "POST", body: { status: "active" } })
            .then(function () { render({ userId: u.id }); }); } }));
      }
      if (can("users.revoke_sessions")) {
        actions.appendChild(el("button", { class: "ghost", text: "Sign out everywhere",
          onclick: function () { confirmThen("Sign " + u.email + " out of every device?",
            function () { return api("/users/" + u.id + "/revoke_sessions", { method: "POST" }); },
            function () { render({ userId: u.id }); }); } }));
      }
      if (actions.children.length) wrap.appendChild(actions);

      wrap.appendChild(el("div", { class: "section-title", text: "Devices" }));
      wrap.appendChild(table(["Name", "Platform", "Version", "Last seen", "State", ""],
        j.devices, function (d) {
          return el("tr", {}, [
            el("td", { text: d.name }),
            el("td", { text: d.platform }),
            el("td", { class: "mono", text: d.app_version || "—" }),
            el("td", { text: ago(d.last_seen_at) }),
            el("td", {}, [pill(d.revoked ? "revoked" : "active", d.revoked ? "bad" : "ok")]),
            el("td", {}, can("devices.revoke") && !d.revoked ? [
              el("button", { class: "ghost", text: "Revoke",
                onclick: function () { confirmThen("Revoke " + d.name + "? That device will be signed out.",
                  function () { return api("/devices/" + d.id + "/revoke", { method: "POST" }); },
                  function () { render({ userId: u.id }); }); } })
            ] : [])
          ]);
        }));

      wrap.appendChild(el("div", { class: "section-title", text: "Recent activity" }));
      wrap.appendChild(table(["When", "Event", "Platform", "Detail"], j.activity, function (a) {
        return el("tr", {}, [
          el("td", { class: "mono", text: stamp(a.ts) }),
          el("td", { text: a.type }),
          el("td", { text: a.platform || "—" }),
          el("td", { class: "mono", text: a.attrs ? JSON.stringify(a.attrs) : "—" })
        ]);
      }));
      return wrap;
    });
  }

  VIEWS.devices = function () {
    return api("/devices").then(function (j) {
      return el("div", {}, header("Devices", "Every registered installation. Online means seen in the last 5 minutes.").concat([
        table(["Name", "Platform", "Version", "Last seen", "State", ""], j.devices, function (d) {
          return el("tr", {}, [
            el("td", { text: d.name }),
            el("td", { text: d.platform }),
            el("td", { class: "mono", text: d.app_version || "—" }),
            el("td", { text: ago(d.last_seen_at) }),
            el("td", {}, [d.revoked ? pill("revoked", "bad")
                        : pill(d.online ? "online" : "offline", d.online ? "ok" : "muted")]),
            el("td", {}, can("devices.revoke") && !d.revoked ? [
              el("button", { class: "ghost", text: "Revoke",
                onclick: function () { confirmThen("Revoke " + d.name + "?",
                  function () { return api("/devices/" + d.id + "/revoke", { method: "POST" }); },
                  function () { render(); }); } })
            ] : [])
          ]);
        })
      ]));
    });
  };

  VIEWS.activity = function () {
    return api("/activity?limit=200").then(function (j) {
      return el("div", {}, header("Activity", "Operational events reported by NOVA clients. Metadata only.").concat([
        table(["When", "Event", "User", "Device", "Platform", "Detail"], j.events, function (e) {
          return el("tr", {}, [
            el("td", { class: "mono", text: stamp(e.ts) }),
            el("td", { text: e.type }),
            el("td", { class: "mono", text: e.user_id ? e.user_id.slice(0, 8) : "—" }),
            el("td", { class: "mono", text: e.device_id ? e.device_id.slice(0, 8) : "—" }),
            el("td", { text: e.platform || "—" }),
            el("td", { class: "mono", text: e.attrs ? JSON.stringify(e.attrs) : "—" })
          ]);
        })
      ]));
    });
  };

  VIEWS.models = function () {
    return api("/models").then(function (j) {
      return el("div", {}, header("Models", "Per-provider request volume, reliability and latency over the last 7 days.").concat([
        table(["Provider", "Model", "Requests", "Failures", "Success", "Avg latency", "First token", "Offline"],
          j.providers, function (p) {
            var tone = p.success_rate >= 0.98 ? "ok" : (p.success_rate >= 0.9 ? "warn" : "bad");
            return el("tr", {}, [
              el("td", { text: p.provider }),
              el("td", { class: "mono", text: p.model }),
              el("td", { text: num(p.requests) }),
              el("td", { text: num(p.failures) }),
              el("td", {}, [pill((p.success_rate * 100).toFixed(1) + "%", tone)]),
              el("td", { text: p.avg_latency_ms ? p.avg_latency_ms + " ms" : "—" }),
              el("td", { text: p.avg_first_token_ms ? p.avg_first_token_ms + " ms" : "—" }),
              el("td", { text: num(p.offline_requests) })
            ]);
          })
      ]));
    });
  };

  VIEWS.agents = function () {
    return api("/agents").then(function (j) {
      return el("div", {}, header("Agents", "Which specialist agents actually run, and which are slow or failing.").concat([
        table(["Agent", "Runs", "Failed", "Success", "Avg duration"], j.agents, function (a) {
          var tone = a.success_rate >= 0.95 ? "ok" : (a.success_rate >= 0.8 ? "warn" : "bad");
          return el("tr", {}, [
            el("td", { text: a.agent }),
            el("td", { text: num(a.runs) }),
            el("td", { text: num(a.failed) }),
            el("td", {}, [pill((a.success_rate * 100).toFixed(1) + "%", tone)]),
            el("td", { text: a.avg_duration_ms ? a.avg_duration_ms + " ms" : "—" })
          ]);
        })
      ]));
    });
  };

  VIEWS.health = function () {
    return api("/health").then(function (j) {
      var tone = j.status === "healthy" ? "ok" : (j.status === "degraded" ? "warn" : "bad");
      return el("div", {}, header("System health", "Measured where the backend can observe directly; reported from client telemetry otherwise.").concat([
        el("div", { class: "cards" }, [
          el("div", { class: "card" }, [
            el("div", { class: "label", text: "Overall" }),
            el("div", { class: "value " + (tone === "ok" ? "" : tone), text: j.status })
          ])
        ]),
        el("div", { class: "section-title", text: "Services" }),
        table(["Service", "Status", "Source", "Detail"], j.checks, function (c) {
          var t = c.status === "healthy" ? "ok" : (c.status === "degraded" ? "warn" : "bad");
          var detail = [];
          if (c.latency_ms !== undefined) detail.push(c.latency_ms + " ms");
          if (c.error_rate !== undefined) detail.push((c.error_rate * 100).toFixed(1) + "% errors");
          if (c.requests_24h !== undefined) detail.push(c.requests_24h + " requests/24h");
          if (c.events_last_hour !== undefined) detail.push(c.events_last_hour + " events/hour");
          if (c.detail) detail.push(c.detail);
          return el("tr", {}, [
            el("td", { text: c.service }),
            el("td", {}, [pill(c.status, t)]),
            el("td", { class: "mono", text: c.source }),
            el("td", { text: detail.join(" · ") || "—" })
          ]);
        })
      ]));
    });
  };

  VIEWS.errors = function () {
    return api("/errors").then(function (j) {
      return el("div", {}, header("Errors", "Structured error codes reported by clients. No user content is stored.").concat([
        el("div", { class: "section-title", text: "By code" }),
        table(["Code", "Count"], j.by_code, function (r) {
          return el("tr", {}, [el("td", { text: r.code }), el("td", { text: num(r.count) })]);
        }),
        el("div", { class: "section-title", text: "Recent" }),
        table(["When", "Code", "Platform", "Version", "Context"], j.recent, function (e) {
          return el("tr", {}, [
            el("td", { class: "mono", text: stamp(e.ts) }),
            el("td", { text: e.code }),
            el("td", { text: e.platform || "—" }),
            el("td", { class: "mono", text: e.app_version || "—" }),
            el("td", { class: "mono", text: e.context ? JSON.stringify(e.context) : "—" })
          ]);
        })
      ]));
    });
  };

  VIEWS.flags = function () {
    return api("/flags").then(function (j) {
      var wrap = el("div", {});
      header("Feature flags", "Server-controlled rollout. Clients cache these so a flag lookup never needs the network.")
        .forEach(function (n) { wrap.appendChild(n); });

      if (can("flags.edit")) {
        var key = el("input", { placeholder: "flag_key" });
        var desc = el("input", { placeholder: "description" });
        wrap.appendChild(el("div", { class: "toolbar" }, [key, desc,
          el("button", { text: "Create or update", onclick: function () {
            if (!key.value.trim()) return;
            api("/flags/" + encodeURIComponent(key.value.trim()), {
              method: "PUT", body: { description: desc.value, enabled: false, rollout_percent: 0 }
            }).then(function () { render(); });
          } })]));
      }

      wrap.appendChild(table(["Key", "Description", "State", "Rollout", ""], j.flags, function (f) {
        var pct = el("input", { type: "number", min: "0", max: "100", value: String(f.rollout_percent) });
        pct.style.width = "80px";
        return el("tr", {}, [
          el("td", { class: "mono", text: f.key }),
          el("td", { text: f.description || "—" }),
          el("td", {}, [pill(f.enabled ? "on" : "off", f.enabled ? "ok" : "muted")]),
          el("td", {}, can("flags.edit") ? [pct] : [el("span", { text: f.rollout_percent + "%" })]),
          el("td", {}, can("flags.edit") ? [
            el("button", { class: "ghost", text: f.enabled ? "Turn off" : "Turn on",
              onclick: function () {
                api("/flags/" + encodeURIComponent(f.key), { method: "PUT",
                  body: { enabled: !f.enabled, rollout_percent: parseInt(pct.value, 10) || 0 }
                }).then(function () { render(); });
              } })
          ] : [])
        ]);
      }));
      return Promise.resolve(wrap);
    });
  };

  VIEWS.audit = function () {
    return api("/audit?limit=200").then(function (j) {
      return el("div", {}, header("Admin audit log", "Every privileged action, including reads of individual user records.").concat([
        table(["When", "Administrator", "Action", "Target", "Result", "Detail"], j.entries, function (e) {
          return el("tr", {}, [
            el("td", { class: "mono", text: stamp(e.ts) }),
            el("td", { text: e.admin_email || "—" }),
            el("td", { text: e.action }),
            el("td", { class: "mono", text: e.target_id ? (e.target_type + ":" + e.target_id.slice(0, 8)) : "—" }),
            el("td", {}, [pill(e.result, e.result === "success" ? "ok" : "bad")]),
            el("td", { class: "mono", text: e.detail ? JSON.stringify(e.detail) : "—" })
          ]);
        })
      ]));
    });
  };

  VIEWS.admins = function () {
    return api("/admins").then(function (j) {
      return el("div", {}, header("Administrators", "Accounts with access to this console.").concat([
        table(["Email", "Role", "MFA", "Status", "Last sign-in"], j.admins, function (a) {
          return el("tr", {}, [
            el("td", { text: a.email }),
            el("td", {}, [pill(a.role, "muted")]),
            el("td", {}, [pill(a.mfa_enabled ? "enabled" : "not enrolled",
                               a.mfa_enabled ? "ok" : "warn")]),
            el("td", {}, [pill(a.status, a.status === "active" ? "ok" : "bad")]),
            el("td", { text: ago(a.last_login_at) })
          ]);
        })
      ]));
    });
  };

  function confirmThen(message, action, done) {
    // Deliberately a real confirmation before anything destructive.
    if (!window.confirm(message)) return;
    action().then(done).catch(function (e) { window.alert(e.message || "Failed."); });
  }

  /* -- render dispatch --------------------------------------------------- */

  var baseRender = render;
  render = function (extra) {
    if (current === "users" && extra && extra.userId) {
      $("loading").hidden = false;
      return userDetail(extra.userId).then(function (node) {
        $("content").textContent = "";
        $("content").appendChild(node);
        $("loading").hidden = true;
      }).catch(function (e) {
        $("content").textContent = "";
        $("content").appendChild(el("div", { class: "forbidden", text: e.message }));
        $("loading").hidden = true;
      });
    }
    return baseRender(extra);
  };

  /* -- session ----------------------------------------------------------- */

  function signIn(ev) {
    ev.preventDefault();
    var btn = $("login-btn");
    var err = $("login-error");
    err.hidden = true;
    btn.disabled = true;
    fetch("/admin/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("email").value, password: $("password").value, totp: $("totp").value
      })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      btn.disabled = false;
      if (!res.ok) {
        err.textContent = res.j.message || "Sign-in failed.";
        err.hidden = false;
        return;
      }
      token = res.j.access_token;
      me = res.j.admin;
      perms = res.j.permissions || [];
      $("login-view").hidden = true;
      $("app-view").hidden = false;
      $("who").textContent = me.email + " · " + me.role;
      var first = SECTIONS.filter(function (s) { return can(s.perm); })[0];
      go(first ? first.id : "dashboard");
    }).catch(function () {
      btn.disabled = false;
      err.textContent = "Could not reach the server.";
      err.hidden = false;
    });
  }

  function signOut() {
    if (token) { api("/auth/logout", { method: "POST" }).catch(function () {}); }
    token = null; me = null; perms = [];
    $("app-view").hidden = true;
    $("login-view").hidden = false;
    // Clear the whole form: this console is often shared, and leaving the
    // previous operator's address in the field invites signing in as them.
    $("email").value = "";
    $("password").value = "";
    $("totp").value = "";
    $("login-error").hidden = true;
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("login-form").addEventListener("submit", signIn);
    $("logout-btn").addEventListener("click", signOut);
    $("refresh-btn").addEventListener("click", function () { render(); });
  });
})();
