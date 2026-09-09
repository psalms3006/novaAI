/* NOVA document library: the setup choice, and the Documents panel.
 *
 * The choice this file exists for is a real trade-off, so it is put to the
 * user once, in plain terms, rather than decided for them:
 *
 *   on this computer   semantic search, offline, ~90 MB download
 *   through the cloud  best quality, needs a connection, text leaves the machine
 *   keyword only       nothing downloaded, nothing sent, finds exact words
 *
 * Whatever they pick, NOVA reports what is *actually* in use. If the local
 * model is chosen but not yet downloaded, the panel says so instead of
 * quietly running keyword search behind a label that promises more.
 */
(function () {
  "use strict";

  var state = null;

  function token() { return window.DESK_TOKEN || ""; }

  function api(path, opts) {
    opts = opts || {};
    var init = {
      method: opts.method || "GET",
      headers: { "X-NOVA-Desk": token() }
    };
    if (opts.body instanceof FormData) {
      init.body = opts.body;                 // let the browser set the boundary
    } else if (opts.body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
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
      else if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) {
      if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }

  function bytes(n) {
    if (!n) return "—";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
  }

  /* -- the setup choice -------------------------------------------------- */

  function chooserCard(onDone) {
    var wrap = el("div", { class: "doc-chooser" });
    wrap.appendChild(el("h3", { text: "How should NOVA search your documents?" }));
    wrap.appendChild(el("p", { class: "doc-note",
      text: "You can change this later. Switching afterwards means anything "
          + "already indexed has to be read again." }));

    var list = el("div", { class: "doc-choices" });
    wrap.appendChild(list);
    var status = el("p", { class: "doc-note" });
    wrap.appendChild(status);

    api("/api/documents/embeddings").then(function (j) {
      state = j;
      list.textContent = "";
      (j.choices || []).forEach(function (c) {
        var card = el("button", {
          class: "doc-choice" + (j.chosen === c.id ? " selected" : ""),
          onclick: function () { pick(c); }
        }, [
          el("div", { class: "doc-choice-title", text: c.label }),
          el("div", { class: "doc-choice-desc", text: c.description }),
          el("div", {
            class: "doc-choice-state" + (c.available ? " ok" : " warn"),
            text: c.available ? "Ready" : c.status
          })
        ]);
        list.appendChild(card);
      });

      // The local model is the only choice that needs fetching first.
      var local = (j.choices || []).filter(function (c) {
        return c.id === "onnx" && !c.available;
      })[0];
      if (local) {
        list.appendChild(el("button", {
          class: "doc-download", text: "Download the local model (~90 MB)",
          onclick: function (e) {
            e.target.disabled = true;
            e.target.textContent = "Downloading…";
            api("/api/documents/embeddings/download", { method: "POST" })
              .then(function () {
                status.textContent = "Downloading in the background. "
                  + "Choose “On this computer” once it finishes.";
              })
              .catch(function () {
                e.target.disabled = false;
                e.target.textContent = "Download failed — try again";
              });
          }
        }));
      }
    }).catch(function () {
      list.textContent = "";
      list.appendChild(el("p", { class: "doc-note",
        text: "The document library is not available in this build." }));
    });

    function pick(choice) {
      api("/api/documents/embeddings", { method: "POST",
                                         body: { choice: choice.id } })
        .then(function (j) {
          status.textContent = j.note || "";
          status.className = "doc-note" + (j.honoured ? "" : " warn");
          Array.prototype.forEach.call(list.querySelectorAll(".doc-choice"),
            function (b) { b.classList.remove("selected"); });
          if (onDone) onDone(j);
          refreshPanel();
        })
        .catch(function (e) { status.textContent = e.message || "Could not save."; });
    }

    return wrap;
  }

  /* -- the Documents panel ----------------------------------------------- */

  var panelHost = null;

  function refreshPanel() {
    if (panelHost) renderPanel(panelHost);
  }

  function renderPanel(host) {
    if (!host) return;
    panelHost = host;
    host.textContent = "";

    host.appendChild(el("div", { class: "settings-sec" }, [
      el("h3", { text: "Your documents" }),
      el("p", { class: "doc-note",
        text: "Drop a file in and NOVA can answer from it, with a reference "
            + "back to the page or section it came from." })
    ]));

    var drop = el("div", { class: "doc-drop", text: "Drop a file here, or click to choose" });
    var picker = el("input", { type: "file", hidden: "hidden" });
    var result = el("p", { class: "doc-note" });

    drop.addEventListener("click", function () { picker.click(); });
    drop.addEventListener("dragover", function (e) {
      e.preventDefault(); drop.classList.add("over");
    });
    drop.addEventListener("dragleave", function () { drop.classList.remove("over"); });
    drop.addEventListener("drop", function (e) {
      e.preventDefault(); drop.classList.remove("over");
      if (e.dataTransfer.files && e.dataTransfer.files.length) {
        upload(e.dataTransfer.files[0]);
      }
    });
    picker.addEventListener("change", function () {
      if (picker.files && picker.files.length) upload(picker.files[0]);
    });

    function upload(file) {
      result.className = "doc-note";
      result.textContent = "Reading " + file.name + "…";
      var form = new FormData();
      form.append("file", file);
      api("/api/documents", { method: "POST", body: form })
        .then(function (j) {
          result.textContent = j.message || "Added.";
          (j.warnings || []).forEach(function (w) {
            result.textContent += "  " + w;
          });
          listDocuments();
        })
        .catch(function (e) {
          result.className = "doc-note warn";
          result.textContent = e.message || "That file could not be added.";
        });
    }

    host.appendChild(drop);
    host.appendChild(picker);
    host.appendChild(result);

    var table = el("div", { class: "doc-list" });
    host.appendChild(table);

    function listDocuments() {
      api("/api/documents").then(function (j) {
        table.textContent = "";
        var s = j.stats || {};
        if (!s.semantic) {
          table.appendChild(el("p", { class: "doc-note warn",
            text: s.backend_note || "Keyword search only." }));
        }
        if (!(j.documents || []).length) {
          table.appendChild(el("p", { class: "doc-note",
            text: "No documents yet." }));
          return;
        }
        (j.documents || []).forEach(function (d) {
          table.appendChild(el("div", { class: "doc-row" }, [
            el("div", {}, [
              el("strong", { text: d.title || d.filename }),
              el("span", { class: "doc-dim",
                text: "  " + d.kind + " · " + bytes(d.bytes)
                    + " · " + d.chunks + " passages"
                    + (d.pages ? " · " + d.pages + " pages" : "") })
            ]),
            el("button", { class: "doc-link", text: "Remove",
              onclick: function () {
                if (!window.confirm("Remove " + (d.title || d.filename)
                                    + " from NOVA's library?")) return;
                api("/api/documents/" + d.id, { method: "DELETE" })
                  .then(listDocuments);
              } })
          ]));
          (d.warnings || []).forEach(function (w) {
            table.appendChild(el("p", { class: "doc-note warn", text: "  " + w }));
          });
        });
      }).catch(function () {
        table.textContent = "";
        table.appendChild(el("p", { class: "doc-note",
          text: "The document library is not available in this build." }));
      });
    }

    listDocuments();
    host.appendChild(el("div", { class: "settings-sec" },
                       [chooserCard(null)]));
  }

  window.NovaDocuments = {
    renderPanel: renderPanel,
    chooser: chooserCard,
    refresh: refreshPanel
  };
})();
