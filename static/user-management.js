/* Admin-only User management panel.
 * Gated on session.role === "admin". Non-admins never fetch users and
 * never bind mutation controls. Reuses _csrfHeaders and _handle401 from app.js.
 */
(function () {
  "use strict";

  var VALID_ROLES = ["viewer", "operator", "admin"];

  // Fixed, safe inline messages keyed by HTTP status. Never reflect raw payload.
  var STATUS_MESSAGES = {
    400: "Request was invalid. Please check the fields and try again.",
    403: "You do not have permission to perform this action.",
    404: "That user could not be found.",
    409: "That change conflicts with current state. Please refresh and try again.",
    422: "The submitted data was not valid. Please check the fields.",
    500: "Something went wrong on the server. Please try again.",
  };

  function safeMessage(status) {
    return STATUS_MESSAGES[status] || "An unexpected error occurred. Please try again.";
  }

  // Escape a username for use inside a URL path segment.
  function encUser(username) {
    return encodeURIComponent(username);
  }

  // Build a JSON request with CSRF headers, reusing app.js helpers.
  function apiFetch(url, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers || {});
    if (typeof window._csrfHeaders === "function") {
      Object.assign(headers, window._csrfHeaders());
    }
    if (options.body !== undefined && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(url, {
      method: options.method || "GET",
      headers: headers,
      body: options.body !== undefined ? options.body : undefined,
      credentials: "same-origin",
    });
  }

  function handle401(response) {
    if (response.status === 401 && typeof window._handle401 === "function") {
      return window._handle401(response);
    }
    return false;
  }

  // Map a non-OK response to a safe inline message (never reflects raw payload).
  function safeError(response) {
    if (handle401(response)) return "Your session expired. Please sign in again.";
    return safeMessage(response.status);
  }

  function initUserManagement(session) {
    if (!session || session.role !== "admin") {
      return; // Non-admin: leave section hidden, bind nothing.
    }

    var section = document.getElementById("user-management");
    if (!section) return;

    var tableBody = section.querySelector("#user-table-body");
    var statusRegion = section.querySelector("#user-status");
    var errorRegion = section.querySelector("#user-error");
    var createForm = section.querySelector("#user-create-form");
    var createUsername = section.querySelector("#new-username");
    var createPassword = section.querySelector("#new-password");
    var createRole = section.querySelector("#new-role");
    var createBusy = section.querySelector("#create-user-btn");

    // Confirm modal
    var modal = document.getElementById("user-confirm-modal");
    var modalTitle = modal ? modal.querySelector("#confirm-title") : null;
    var modalText = modal ? modal.querySelector("#confirm-text") : null;
    var modalInput = modal ? modal.querySelector("#confirm-username") : null;
    var modalError = modal ? modal.querySelector("#confirm-error") : null;
    var modalCancel = modal ? modal.querySelector("#confirm-cancel") : null;
    var modalConfirm = modal ? modal.querySelector("#confirm-ok") : null;

    // Password modal (for reset)
    var pwModal = document.getElementById("user-password-modal");
    var pwTitle = pwModal ? pwModal.querySelector("#pw-title") : null;
    var pwInput = pwModal ? pwModal.querySelector("#pw-new") : null;
    var pwError = pwModal ? pwModal.querySelector("#pw-error") : null;
    var pwCancel = pwModal ? pwModal.querySelector("#pw-cancel") : null;
    var pwOk = pwModal ? pwModal.querySelector("#pw-ok") : null;

    var selfUsername = session.username;
    var actionBusy = false;

    function setStatus(msg) {
      if (statusRegion) statusRegion.textContent = msg || "";
    }
    function setError(msg) {
      if (errorRegion) errorRegion.textContent = msg || "";
    }

    function clearPasswordFields() {
      if (createPassword) createPassword.value = "";
      if (pwInput) pwInput.value = "";
    }

    function openModal(el) { if (el) el.hidden = false; }
    function closeModal(el) {
      if (actionBusy) return;
      if (!el) return;
      el.hidden = true;
      if (el === modal && modalInput) modalInput.value = "";
      if (el === modal && modalError) modalError.textContent = "";
      if (el === pwModal && pwInput) pwInput.value = "";
      if (el === pwModal && pwError) pwError.textContent = "";
      pendingAction = null;
    }

    // Warn + redirect on self mutation (session revocation).
    function isSelf(username) {
      return username === selfUsername;
    }
    function afterSelfMutation() {
      window.location.href = "/login";
    }

    function renderUsers(users) {
      if (!tableBody) return;
      tableBody.textContent = "";
      users.forEach(function (u) {
        var tr = document.createElement("tr");
        tr.dataset.username = u.username;
        tr.dataset.role = u.role;

        var tdName = document.createElement("td");
        tdName.textContent = u.username;
        tr.appendChild(tdName);

        var tdRole = document.createElement("td");
        var sel = document.createElement("select");
        sel.className = "role-select";
        sel.setAttribute("aria-label", "Role for " + u.username);
        VALID_ROLES.forEach(function (r) {
          var opt = document.createElement("option");
          opt.value = r;
          opt.textContent = r;
          if (r === u.role) opt.selected = true;
          sel.appendChild(opt);
        });
        tdRole.appendChild(sel);
        tr.appendChild(tdRole);

        var tdStatus = document.createElement("td");
        var statusText = document.createElement("span");
        statusText.textContent = u.disabled ? "Disabled" : "Enabled";
        tdStatus.appendChild(statusText);
        tr.appendChild(tdStatus);

        var tdActions = document.createElement("td");
        tdActions.className = "row-actions";

        var toggleBtn = document.createElement("button");
        toggleBtn.type = "button";
        toggleBtn.className = "btn btn-sm";
        toggleBtn.textContent = u.disabled ? "Enable" : "Disable";
        toggleBtn.dataset.action = "toggle";
        tdActions.appendChild(toggleBtn);

        var resetBtn = document.createElement("button");
        resetBtn.type = "button";
        resetBtn.className = "btn btn-sm";
        resetBtn.textContent = "Reset password";
        resetBtn.dataset.action = "reset";
        tdActions.appendChild(resetBtn);

        tr.appendChild(tdActions);
        tableBody.appendChild(tr);
      });
    }

    function loadUsers() {
      setError("");
      setStatus("Loading users…");
      apiFetch("/api/admin/users")
        .then(function (res) {
          if (!res.ok) throw new Error(safeError(res));
          return res.json();
        })
        .then(function (data) {
          var users = Array.isArray(data.users) ? data.users : [];
          renderUsers(users);
          setStatus("");
        })
        .catch(function (err) {
          setError(err.message || safeMessage(500));
          setStatus("");
        });
    }

    // Confirm modal flow for destructive actions (disable/demote/reset).
    var pendingAction = null;
    function openConfirm(action, username, label, role) {
      if (actionBusy) return;
      pendingAction = { action: action, username: username, role: role };
      if (modalTitle) modalTitle.textContent = label;
      if (modalText) {
        var self = isSelf(username);
        modalText.textContent = self
          ? "You are modifying your own account. This will sign you out. Type your username to confirm."
          : "Type the username to confirm.";
      }
      if (modalInput) {
        modalInput.value = "";
      }
      if (modalConfirm) modalConfirm.disabled = false;
      if (modalCancel) modalCancel.disabled = false;
      openModal(modal);
      if (modalInput) modalInput.focus();
    }

    function runPending() {
      if (actionBusy || !pendingAction || modal.hidden) return;
      var username = pendingAction.username;
      var action = pendingAction.action;
      var requestedRole = pendingAction.role;
      if (modalInput && modalInput.value !== username) {
        if (modalError) modalError.textContent = "Username does not match.";
        return;
      }
      if (modalError) modalError.textContent = "";
      actionBusy = true;
      if (modalConfirm) modalConfirm.disabled = true;
      if (modalCancel) modalCancel.disabled = true;

      var done = function (ok) {
        actionBusy = false;
        if (modalConfirm) modalConfirm.disabled = false;
        if (modalCancel) modalCancel.disabled = false;
        closeModal(modal);
        if (ok) {
          if (isSelf(username)) {
            afterSelfMutation();
            return;
          }
          loadUsers();
        }
      };

      if (action === "toggle") {
        var target = username;
        var currentDisabled = null;
        if (tableBody) {
          var row = tableBody.querySelector('tr[data-username="' + encUser(target) + '"]');
          if (row) {
            var span = row.querySelector("td:nth-child(3) span");
            currentDisabled = span ? span.textContent === "Disabled" : null;
          }
        }
        var nextDisabled = currentDisabled === null ? true : !currentDisabled;
        apiFetch("/api/admin/users/" + encUser(target) + "/disabled", {
          method: "PATCH",
          body: JSON.stringify({ disabled: nextDisabled }),
        })
          .then(function (res) {
            if (!res.ok) throw new Error(safeError(res));
            done(true);
          })
          .catch(function (err) {
            setError(err.message || safeMessage(500));
            done(false);
          });
      } else if (action === "demote") {
        apiFetch("/api/admin/users/" + encUser(username), {
          method: "PATCH",
          body: JSON.stringify({ role: requestedRole }),
        })
          .then(function (res) {
            if (!res.ok) throw new Error(safeError(res));
            done(true);
          })
          .catch(function (err) {
            setError(err.message || safeMessage(500));
            done(false);
          });
      } else if (action === "reset") {
        // Hand off to password modal.
        actionBusy = false;
        closeModal(modal);
        openPasswordModal(username);
      }
    }

    function openPasswordModal(username) {
      if (actionBusy) return;
      if (pwTitle) pwTitle.textContent = "Reset password for " + username;
      if (pwInput) {
        pwInput.value = "";
      }
      if (pwError) pwError.textContent = "";
      if (pwOk) pwOk.disabled = false;
      if (pwCancel) pwCancel.disabled = false;
      openModal(pwModal);
      if (pwInput) pwInput.focus();
      pendingAction = { action: "reset", username: username };
    }

    function runPassword() {
      if (actionBusy || pwModal.hidden) return;
      var username = pendingAction ? pendingAction.username : null;
      if (!username) return;
      var pw = pwInput ? pwInput.value : "";
      if (pw.length < 12 || pw.length > 128) {
        if (pwError) pwError.textContent = "Password must be 12 to 128 characters.";
        return;
      }
      if (pwOk) pwOk.disabled = true;
      if (pwCancel) pwCancel.disabled = true;
      actionBusy = true;
      apiFetch("/api/admin/users/" + encUser(username) + "/reset-password", {
        method: "POST",
        body: JSON.stringify({ password: pw }),
      })
        .then(function (res) {
          if (!res.ok) throw new Error(safeError(res));
          actionBusy = false;
          clearPasswordFields();
          if (pwOk) pwOk.disabled = false;
          if (pwCancel) pwCancel.disabled = false;
          closeModal(pwModal);
          if (isSelf(username)) {
            afterSelfMutation();
            return;
          }
          loadUsers();
        })
        .catch(function (err) {
          actionBusy = false;
          clearPasswordFields();
          if (pwOk) pwOk.disabled = false;
          if (pwCancel) pwCancel.disabled = false;
          if (pwError) pwError.textContent = err.message || safeMessage(500);
        });
    }

    // Wire controls (admin only).
    if (section) {
      section.hidden = false;
    }

    if (createForm) {
      createForm.addEventListener("submit", function (e) {
        e.preventDefault();
        if (createBusy && createBusy.disabled) return; // busy guard
        var username = createUsername ? createUsername.value.trim() : "";
        var password = createPassword ? createPassword.value : "";
        var role = createRole ? createRole.value : "viewer";
        if (!/^[a-z0-9][a-z0-9._-]{0,31}$/.test(username) || password.length < 12 || password.length > 128) {
          setError("Provide a username and a password of 12 to 128 characters.");
          return;
        }
        if (createBusy) createBusy.disabled = true;
        setError("");
        setStatus("Creating user…");
        apiFetch("/api/admin/users", {
          method: "POST",
          body: JSON.stringify({ username: username, password: password, role: role }),
        })
          .then(function (res) {
            if (!res.ok) throw new Error(safeError(res));
            clearPasswordFields();
            if (createUsername) createUsername.value = "";
            setStatus("");
            loadUsers();
          })
          .catch(function (err) {
            clearPasswordFields();
            setError(err.message || safeMessage(500));
            setStatus("");
          })
          .finally(function () {
            if (createBusy) createBusy.disabled = false;
          });
      });
    }

    // Delegate row actions.
    if (tableBody) {
      tableBody.addEventListener("click", function (e) {
        if (actionBusy) return;
        var btn = e.target.closest("button[data-action]");
        if (!btn) return;
        var row = btn.closest("tr");
        if (!row) return;
        var username = row.dataset.username;
        if (!username) return;
        var action = btn.dataset.action;
        if (action === "toggle") {
          openConfirm("toggle", username, "Change enabled status");
        } else if (action === "reset") {
          openConfirm("reset", username, "Reset password");
        }
      });

      // Role select changes.
      tableBody.addEventListener("change", function (e) {
        var sel = e.target.closest("select.role-select");
        if (!sel) return;
        var row = sel.closest("tr");
        if (!row) return;
        if (actionBusy) { sel.value = row.dataset.role; return; }
        var username = row.dataset.username;
        if (!username) return;
        var newRole = sel.value;
        var currentRole = null;
        // Determine current role from the select's previous value is not tracked;
        // treat any change as a role mutation. Demoting an admin is destructive.
        var wasAdmin = false;
        // We don't store prior role on the element; infer: if newRole !== admin and
        // the user was admin, it's a demotion. Track via dataset.
        if (row.dataset.role === "admin" && newRole !== "admin") {
          wasAdmin = true;
        }
        if (wasAdmin) {
          openConfirm("demote", username, "Demote admin to " + newRole, newRole);
          // Revert select until confirmed.
          sel.value = row.dataset.role || "viewer";
          return;
        }
        // Non-destructive role change: apply directly.
        if (sel.disabled) return;
        sel.disabled = true;
        actionBusy = true;
        apiFetch("/api/admin/users/" + encUser(username), {
          method: "PATCH",
          body: JSON.stringify({ role: newRole }),
        })
          .then(function (res) {
            if (!res.ok) throw new Error(safeError(res));
            if (isSelf(username)) {
              afterSelfMutation();
              return;
            }
            loadUsers();
          })
          .catch(function (err) {
            setError(err.message || safeMessage(500));
            sel.value = row.dataset.role || "viewer";
          })
          .finally(function () {
            actionBusy = false;
            sel.disabled = false;
          });
      });
    }

    // Modal wiring.
    if (modalCancel) modalCancel.addEventListener("click", function () { closeModal(modal); });
    if (modalConfirm) modalConfirm.addEventListener("click", runPending);
    if (modalInput) {
      modalInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); runPending(); }
      });
    }
    if (pwCancel) pwCancel.addEventListener("click", function () { if (actionBusy) return; clearPasswordFields(); closeModal(pwModal); });
    if (pwOk) pwOk.addEventListener("click", runPassword);
    if (pwInput) {
      pwInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); runPassword(); }
      });
    }

    // Close modals on backdrop click / Escape.
    [modal, pwModal].forEach(function (m) {
      if (!m) return;
      m.addEventListener("click", function (e) {
        if (e.target === m) {
          if (actionBusy) return;
          clearPasswordFields();
          closeModal(m);
        }
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (actionBusy) return;
        clearPasswordFields();
        closeModal(modal);
        closeModal(pwModal);
      }
    });

    loadUsers();
  }

  // Expose for app.js to call after /api/session confirms admin.
  window.initUserManagement = initUserManagement;
})();
