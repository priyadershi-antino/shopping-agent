// Shopping Agent web UI: streamed chat, JSON forms for the REST API, chart hover.
// No framework, no build step. All text from the server is inserted with textContent;
// only the server-rendered (auto-escaped) run fragment is inserted as HTML.
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

  async function errorText(res) {
    try {
      const body = await res.json();
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return body.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
      return JSON.stringify(body);
    } catch {
      return `${res.status} ${res.statusText}`;
    }
  }

  async function api(method, url, body) {
    const init = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    const res = await fetch(url, init);
    if (!res.ok) throw new Error(await errorText(res));
    const type = res.headers.get("content-type") || "";
    return type.includes("application/json") ? res.json() : null;
  }

  // ---------- Server-Sent Events over fetch (POST bodies are not possible with EventSource) ----------

  async function readSse(stream, onEvent) {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        let event = "message";
        const data = [];
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
        }
        if (data.length) {
          try {
            onEvent(event, JSON.parse(data.join("\n")));
          } catch (err) {
            console.warn("bad event", err);
          }
        }
      }
    }
  }

  // ---------- Chat ----------

  function addStep(list, text, kind, tool) {
    const li = document.createElement("li");
    li.className = kind;
    li.textContent = text;
    if (tool) li.dataset.tool = tool;
    list.append(li);
    return li;
  }

  function initChat() {
    const form = $("#ask-form");
    if (!form) return;
    const labels = JSON.parse(form.dataset.toolLabels || "{}");
    const progress = $("#progress");
    const result = $("#result");
    const button = form.querySelector("button[type=submit]");
    const followUp = $("#follow-up", form);

    const syncFollowUp = () => {
      if (!followUp) return;
      followUp.closest(".check").hidden = !form.dataset.lastRun;
      if (!form.dataset.lastRun) followUp.checked = false;
    };
    syncFollowUp();

    // Platform choice is remembered in this browser (storage can be blocked: ignore failures).
    const boxes = $$("input[name=platform]", form);
    try {
      const saved = JSON.parse(localStorage.getItem("platforms") || "null");
      if (Array.isArray(saved) && saved.length) boxes.forEach((b) => (b.checked = saved.includes(b.value)));
    } catch (e) { /* default selection */ }
    const chosenPlatforms = () => boxes.filter((b) => b.checked).map((b) => b.value);

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const message = form.elements.message.value.trim();
      if (message.length < 2) return;
      const platforms = chosenPlatforms();
      if (!platforms.length) {
        progress.hidden = false;
        progress.replaceChildren();
        addStep(progress, "Pick at least one platform to show prices from.", "error");
        return;
      }
      try { localStorage.setItem("platforms", JSON.stringify(platforms)); } catch (e) { /* ignore */ }
      const previous = followUp && followUp.checked ? form.dataset.lastRun : null;
      button.disabled = true;
      progress.hidden = false;
      progress.replaceChildren();
      result.replaceChildren();
      addStep(progress, "Starting…", "stage");
      let finalRun = null;
      try {
        const res = await fetch("/v1/chat", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message, previous_run_id: previous, stream: true, platforms }),
        });
        if (!res.ok || !res.body) throw new Error(await errorText(res));
        await readSse(res.body, (event, data) => {
          if (event === "stage") addStep(progress, data.message, "stage");
          else if (event === "tool_call") addStep(progress, labels[data.message] || data.message, "tool", data.message);
          else if (event === "tool_result") {
            const pending = $$("li.tool", progress).find((li) => li.dataset.tool === data.message && !li.dataset.done);
            if (pending) {
              pending.dataset.done = "1";
              pending.classList.add(data.data.status === "ok" || data.data.status === "partial" ? "ok" : "failed");
            }
          } else if (event === "warning") addStep(progress, data.message, "warning");
          else if (event === "final") finalRun = data;
        });
        if (!finalRun) throw new Error("The run ended without a result. Check the server log.");
        const fragment = await fetch(`/ui/runs/${encodeURIComponent(finalRun.run_id)}`, { credentials: "same-origin" });
        if (!fragment.ok) throw new Error(await errorText(fragment));
        result.innerHTML = await fragment.text();
        form.dataset.lastRun = finalRun.run_id;
        syncFollowUp();
        if (finalRun.status === "needs_clarification" && followUp) followUp.checked = true;
        form.elements.message.value = "";
        window.history.replaceState({}, "", `/runs/${encodeURIComponent(finalRun.run_id)}`);
        enhance(result);
      } catch (err) {
        addStep(progress, err.message || String(err), "error");
      } finally {
        button.disabled = false;
      }
    });
  }

  // ---------- JSON forms and buttons that call the REST API ----------

  function formBody(form) {
    const body = {};
    for (const el of form.elements) {
      if (!el.name || el.disabled || el.type === "submit") continue;
      const kind = el.dataset.type || (el.type === "number" ? "number" : el.type === "checkbox" ? "bool" : "text");
      let value;
      if (kind === "bool") value = el.type === "checkbox" ? el.checked : el.value === "true";
      else if (el.value.trim() === "") continue;
      else if (kind === "number") value = Number(el.value);
      else if (kind === "json") {
        try {
          value = JSON.parse(el.value);
        } catch {
          value = el.value.trim();
        }
      } else if (kind === "list") value = el.value.split(",").map((s) => s.trim()).filter(Boolean);
      else value = el.value.trim();
      body[el.name] = value;
    }
    return body;
  }

  function fillUrl(template, body) {
    return template.replace(/\{(\w+)\}/g, (_, key) => {
      const v = body[key];
      delete body[key];
      return encodeURIComponent(v ?? "");
    });
  }

  function showMessage(form, text, isError) {
    let box = $(".form-message", form);
    if (!box) {
      box = document.createElement("p");
      box.className = "form-message";
      form.append(box);
    }
    box.classList.toggle("error", Boolean(isError));
    box.textContent = text;
  }

  function initApiForms(root) {
    for (const form of $$("form[data-api]", root)) {
      if (form.dataset.bound) continue;
      form.dataset.bound = "1";
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        const body = formBody(form);
        const url = fillUrl(form.dataset.api, body);
        const method = (form.dataset.method || "POST").toUpperCase();
        const button = form.querySelector("button[type=submit]");
        if (button) button.disabled = true;
        try {
          const data = await api(method, url, method === "GET" || method === "DELETE" ? undefined : body);
          if (form.dataset.render === "json") {
            const out = $(form.dataset.target);
            if (out) out.textContent = JSON.stringify(data, null, 2);
            if (out) out.hidden = false;
          } else if (form.dataset.redirect) {
            window.location.assign(form.dataset.redirect);
          } else {
            window.location.reload();
          }
        } catch (err) {
          showMessage(form, err.message || String(err), true);
        } finally {
          if (button) button.disabled = false;
        }
      });
    }

    for (const btn of $$("button[data-api]", root)) {
      if (btn.dataset.bound) continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", async () => {
        if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
        btn.disabled = true;
        try {
          const body = btn.dataset.body ? JSON.parse(btn.dataset.body) : undefined;
          await api((btn.dataset.method || "POST").toUpperCase(), btn.dataset.api, body);
          if (btn.dataset.done) {
            btn.textContent = btn.dataset.done;
          } else {
            window.location.reload();
          }
        } catch (err) {
          btn.disabled = false;
          btn.title = err.message || String(err);
          btn.textContent = `Failed: ${btn.title}`.slice(0, 80);
        }
      });
    }
  }

  // ---------- Price chart hover: crosshair + tooltip ----------

  function initCharts(root) {
    for (const svg of $$("svg.price-chart", root)) {
      if (svg.dataset.bound) continue;
      svg.dataset.bound = "1";
      let series;
      try {
        series = JSON.parse(svg.dataset.series || "[]");
      } catch {
        continue;
      }
      if (!series.length) continue;
      const figure = svg.closest("figure");
      const tooltip = $(".tooltip", figure);
      const cross = $(".crosshair", svg);
      const ns = "http://www.w3.org/2000/svg";
      const dots = series.map((s) => {
        const c = document.createElementNS(ns, "circle");
        c.setAttribute("r", "4.5");
        c.setAttribute("class", `hover-dot marker s${s.slot}`);
        c.setAttribute("visibility", "hidden");
        svg.append(c);
        return c;
      });
      const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p.d)))].sort();
      const xOf = new Map();
      for (const s of series) for (const p of s.points) xOf.set(p.d, p.x);

      const hide = () => {
        cross.setAttribute("visibility", "hidden");
        dots.forEach((d) => d.setAttribute("visibility", "hidden"));
        tooltip.hidden = true;
      };

      svg.addEventListener("pointerleave", hide);
      svg.addEventListener("pointermove", (ev) => {
        const ctm = svg.getScreenCTM();
        if (!ctm) return;
        const pt = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(ctm.inverse());
        let best = dates[0];
        for (const d of dates) if (Math.abs(xOf.get(d) - pt.x) < Math.abs(xOf.get(best) - pt.x)) best = d;
        const x = xOf.get(best);
        cross.setAttribute("x1", x);
        cross.setAttribute("x2", x);
        cross.setAttribute("visibility", "visible");

        tooltip.replaceChildren();
        const head = document.createElement("div");
        head.className = "t-date";
        head.textContent = new Date(`${best}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
        tooltip.append(head);
        series.forEach((s, i) => {
          // step-after: the price on a date is the last observation on or before it
          const known = s.points.filter((p) => p.d <= best);
          const p = known[known.length - 1];
          if (!p) {
            dots[i].setAttribute("visibility", "hidden");
            return;
          }
          dots[i].setAttribute("cx", x);
          dots[i].setAttribute("cy", p.y);
          dots[i].setAttribute("visibility", "visible");
          const row = document.createElement("div");
          row.className = "t-row";
          if (series.length > 1) {
            const key = document.createElement("span");
            key.className = `key s${s.slot}`;
            row.append(key);
          }
          const text = document.createElement("span");
          text.textContent = series.length > 1 ? `${s.label}: ${inr.format(p.v)}` : inr.format(p.v);
          row.append(text);
          tooltip.append(row);
        });
        tooltip.hidden = false;
        const box = figure.getBoundingClientRect();
        const left = ev.clientX - box.left + 14;
        const maxLeft = box.width - tooltip.offsetWidth - 4;
        tooltip.style.left = `${Math.max(4, Math.min(left, maxLeft))}px`;
        tooltip.style.top = `${Math.max(0, ev.clientY - box.top - tooltip.offsetHeight - 12)}px`;
      });
    }
  }

  function enhance(root) {
    initApiForms(root);
    initCharts(root);
  }

  async function showUnread() {
    const badge = $("#unread-count");
    if (!badge) return;
    try {
      const inbox = await api("GET", "/v1/notifications?unread_only=true&limit=1");
      if (inbox && inbox.unread > 0) {
        badge.textContent = String(inbox.unread);
        badge.hidden = false;
      }
    } catch {
      /* the badge is optional */
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    initChat();
    enhance(document);
    showUnread();
  });
})();
