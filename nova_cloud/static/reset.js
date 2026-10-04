/* Password reset. The token stays in the URL and the password never does. */
(function () {
  "use strict";
  var token = new URLSearchParams(location.search).get("token") || "";
  var form = document.getElementById("reset-form");
  var msg = document.getElementById("msg");
  var go = document.getElementById("go");

  function say(text, ok) {
    msg.textContent = text;
    msg.className = ok ? "hint" : "error";
    msg.hidden = false;
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var a = document.getElementById("pw").value;
    var b = document.getElementById("pw2").value;
    if (a !== b) { say("Those two passwords are not the same.", false); return; }
    go.disabled = true;
    fetch("/v1/auth/password/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token, password: a })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      go.disabled = false;
      if (res.ok) {
        form.innerHTML = '<div class="brand">NOVA</div>'
          + '<p class="hint" style="margin-top:18px">Your password is updated. '
          + 'Sign in again from NOVA.</p>';
      } else {
        say(res.j.message || "That did not work.", false);
      }
    }).catch(function () {
      go.disabled = false;
      say("Could not reach NOVA Cloud.", false);
    });
  });
})();
