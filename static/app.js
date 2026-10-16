(() => {
  const $ = (selector) => document.querySelector(selector);
  const sessionForm = $("#session-form");
  const activeSession = $("#active-session");
  const notice = $("#notice");
  let sessionId = "";
  let sessionTitle = "";
  let currentReceipt = null;
  let codeExpiresAt = 0;
  let refreshingCode = false;

  function localDateTime(date) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return local.toISOString().slice(0, 16);
  }

  function setDefaults() {
    const start = new Date();
    const end = new Date(start.getTime() + 60 * 60_000);
    $("#starts-at").value = localDateTime(start);
    $("#ends-at").value = localDateTime(end);
  }

  function showNotice(message, success = false) {
    notice.textContent = message;
    notice.classList.toggle("notice-success", success);
    notice.setAttribute("role", success ? "status" : "alert");
    notice.setAttribute("aria-live", success ? "polite" : "assertive");
    notice.hidden = false;
  }

  function clearNotice() {
    notice.hidden = true;
    notice.textContent = "";
  }

  async function api(path, { method = "GET", user = "instructor-1", body } = {}) {
    const headers = { "X-User-ID": user };
    const options = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`;
      throw new Error(detail);
    }
    return data;
  }

  function updateSessionView(id, title) {
    sessionId = id;
    sessionTitle = title || "Attendance session";
    sessionForm.hidden = true;
    activeSession.hidden = false;
    $("#active-title").textContent = sessionTitle;
    $("#session-id").value = id;
    $("#checkin-session-id").value = id;
    localStorage.setItem("digii.attendance.sessionId", id);
    localStorage.setItem("digii.attendance.sessionTitle", sessionTitle);
  }

  async function refreshCode() {
    if (!sessionId || refreshingCode) return;
    refreshingCode = true;
    try {
      const data = await api(`/sessions/${encodeURIComponent(sessionId)}/code`);
      $("#live-code").textContent = data.code;
      $("#copy-code").disabled = false;
      codeExpiresAt = Date.now() + data.valid_for_seconds * 1000;
      updateTimer();
    } catch (error) {
      $("#live-code").textContent = "— — — — — —";
      $("#code-timer").textContent = "Code unavailable";
      $("#copy-code").disabled = true;
      // Retry at the normal rotation cadence instead of every timer tick, so a
      // standing failure (deleted session, server restart) can't turn into a
      // once-a-second request storm against the backend.
      codeExpiresAt = Date.now() + 30_000;
      showNotice(error.message);
    } finally {
      refreshingCode = false;
    }
  }

  function updateTimer() {
    if (!codeExpiresAt) return;
    const seconds = Math.max(0, Math.ceil((codeExpiresAt - Date.now()) / 1000));
    $("#code-timer").textContent = seconds ? `Changes in ${seconds}s` : "Refreshing code…";
    if (seconds === 0) refreshCode();
  }

  async function copyText(text, label) {
    try {
      await navigator.clipboard.writeText(text);
      showNotice(`${label} copied.`, true);
    } catch {
      showNotice("Clipboard access is unavailable in this browser. Select and copy the text instead.");
    }
  }

  function showReceipt(receipt) {
    currentReceipt = receipt;
    $("#receipt-panel").hidden = false;
    $("#receipt-verified").hidden = true;
    $("#receipt-time").textContent = new Date(receipt.checked_in_at).toLocaleString();
    $("#receipt-id").textContent = receipt.receipt_id;
  }

  function studentName(id) {
    return ({ "student-1": "Demo Student", "student-2": "Demo Student Two" })[id] || id;
  }

  function renderAttendance(items) {
    const list = $("#attendance-list");
    list.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No attendance events yet.";
      list.append(empty);
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "attendance-item";
      const person = document.createElement("span");
      person.className = "attendance-person";
      person.textContent = studentName(item.student_id);
      const state = document.createElement("span");
      state.className = `status-pill${item.status === "present" ? " status-present" : ""}`;
      state.textContent = item.status;
      if (item.disputed) {
        const disputed = document.createElement("span");
        disputed.className = "status-pill status-disputed";
        disputed.textContent = "under review";
        state.append(" ", disputed);
      }
      row.append(person, state);
      list.append(row);
    }
  }

  sessionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearNotice();
    const button = sessionForm.querySelector("button[type='submit']");
    button.disabled = true;
    try {
      const result = await api("/sessions", {
        method: "POST",
        body: {
          title: $("#session-title").value.trim(),
          starts_at: new Date($("#starts-at").value).toISOString(),
          ends_at: new Date($("#ends-at").value).toISOString(),
        },
      });
      updateSessionView(result.id, result.title);
      showNotice("Session created. The projected code is ready below.", true);
      await refreshCode();
    } catch (error) {
      showNotice(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#clear-session").addEventListener("click", () => {
    localStorage.removeItem("digii.attendance.sessionId");
    localStorage.removeItem("digii.attendance.sessionTitle");
    sessionId = "";
    sessionTitle = "";
    codeExpiresAt = 0;
    currentReceipt = null;
    sessionForm.reset();
    setDefaults();
    sessionForm.hidden = false;
    activeSession.hidden = true;
    $("#receipt-panel").hidden = true;
    $("#attendance-result").hidden = true;
    clearNotice();
  });

  $("#copy-session").addEventListener("click", () => copyText(sessionId, "Session ID"));
  $("#copy-code").addEventListener("click", () => copyText($("#live-code").textContent, "Check-in code"));

  $("#checkin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    clearNotice();
    const button = event.currentTarget.querySelector("button[type='submit']");
    button.disabled = true;
    const studentId = $("#student-id").value;
    try {
      const receipt = await api(`/sessions/${encodeURIComponent($("#checkin-session-id").value.trim())}/checkin`, {
        method: "POST",
        user: studentId,
        body: { code: $("#checkin-code").value.trim() },
      });
      showReceipt(receipt);
      showNotice("Your check-in is recorded. Keep the receipt ID for your records.", true);
    } catch (error) {
      showNotice(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#verify-receipt").addEventListener("click", async () => {
    if (!currentReceipt) return;
    try {
      const result = await api("/receipts/verify", {
        method: "POST",
        user: currentReceipt.student_id,
        body: { receipt_id: currentReceipt.receipt_id, signature: currentReceipt.signature },
      });
      $("#receipt-verified").hidden = !result.valid;
      showNotice(result.valid ? "Receipt signature and event chain verified." : "Receipt could not be verified.", result.valid);
    } catch (error) {
      showNotice(error.message);
    }
  });

  $("#copy-receipt").addEventListener("click", () => {
    if (!currentReceipt) return;
    copyText(JSON.stringify(currentReceipt, null, 2), "Receipt");
  });

  $("#load-attendance").addEventListener("click", async (event) => {
    const id = $("#checkin-session-id").value.trim();
    if (!id) return showNotice("Create a session or enter a session ID first.");
    clearNotice();
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/sessions/${encodeURIComponent(id)}/attendance`, { user: $("#attendance-user").value });
      $("#attendance-result").hidden = false;
      renderAttendance(result.attendance);
      $("#chain-state").textContent = `${result.events.length} event${result.events.length === 1 ? "" : "s"}`;
      $("#chain-detail").textContent = "Current attendance from the event history";
    } catch (error) {
      showNotice(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#verify-chain").addEventListener("click", async (event) => {
    const id = $("#checkin-session-id").value.trim();
    if (!id) return showNotice("Create a session or enter a session ID first.");
    clearNotice();
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/sessions/${encodeURIComponent(id)}/verify`, { user: $("#attendance-user").value });
      $("#chain-state").textContent = result.valid ? "History verified" : "History needs review";
      $("#chain-detail").textContent = result.valid ? `${result.events_checked} event${result.events_checked === 1 ? "" : "s"} checked` : `First invalid event: ${result.first_invalid_event_id}`;
      showNotice(result.valid ? "The event chain is intact." : "The event chain has an integrity issue.", result.valid);
    } catch (error) {
      showNotice(error.message);
    } finally {
      button.disabled = false;
    }
  });

  setDefaults();
  const savedId = localStorage.getItem("digii.attendance.sessionId");
  if (savedId) {
    updateSessionView(savedId, localStorage.getItem("digii.attendance.sessionTitle"));
    refreshCode();
  }
  window.setInterval(updateTimer, 1000);
})();
