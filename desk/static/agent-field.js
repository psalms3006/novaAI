/**
 * NOVA Agent Field — Real-time agent/task hierarchy visualization.
 *
 * Renders agent nodes only when tasks are running. Nodes are driven by
 * backend WebSocket events — no mocked/demo states.
 *
 * States: hidden (no tasks), active (tasks running), collapsing (task done)
 */
"use strict";

class AgentField {
  constructor(container) {
    this.container = container;
    this.tasks = new Map(); // task_id -> { label, agents: Map }
    this.nodes = new Map(); // agent_id -> DOM element
    this._render();
  }

  handleEvent(event) {
    switch (event.type) {
      case "task_start":
        this._taskStart(event.task_id, event.label);
        break;
      case "task_done":
        this._taskDone(event.task_id, event.ok, event.summary);
        break;
      case "agent_start":
        this._agentStart(event.agent_id, event.task_id, event.name, event.action, event.tool);
        break;
      case "agent_progress":
        this._agentProgress(event.agent_id, event.action, event.tool);
        break;
      case "agent_done":
        this._agentDone(event.agent_id, event.ok, event.summary);
        break;
    }
  }

  _taskStart(taskId, label) {
    this.tasks.set(taskId, { label, agents: new Map(), done: false });
    this._render();
  }

  _taskDone(taskId, ok, summary) {
    const task = this.tasks.get(taskId);
    if (task) {
      task.done = true;
      task.ok = ok;
      task.summary = summary;
    }
    // Remove after delay
    setTimeout(() => {
      this.tasks.delete(taskId);
      // Remove all agent nodes for this task
      for (const [agentId, el] of this.nodes) {
        if (agentId.includes(taskId)) {
          el.classList.add("removing");
          setTimeout(() => el.remove(), 200);
          this.nodes.delete(agentId);
        }
      }
      this._render();
    }, 2000);
    this._render();
  }

  _agentStart(agentId, taskId, name, action, tool) {
    const task = this.tasks.get(taskId);
    if (!task) return;

    task.agents.set(agentId, { name, action, tool, ok: true, done: false });

    const el = document.createElement("div");
    el.className = "agent-node entering";
    el.dataset.agentId = agentId;
    el.innerHTML = `
      <div class="agent-icon">${this._toolIcon(tool)}</div>
      <div class="agent-info">
        <div class="agent-name">${this._esc(name)}</div>
        <div class="agent-action">${this._esc(action || "")}</div>
      </div>
      <div class="agent-status active"></div>
    `;
    this.container.appendChild(el);
    this.nodes.set(agentId, el);

    // Trigger enter animation
    requestAnimationFrame(() => el.classList.remove("entering"));
  }

  _agentProgress(agentId, action, tool) {
    const agent = this.nodes.get(agentId);
    if (!agent) return;

    const actionEl = agent.querySelector(".agent-action");
    if (actionEl && action) actionEl.textContent = action;

    if (tool) {
      const iconEl = agent.querySelector(".agent-icon");
      if (iconEl) iconEl.innerHTML = this._toolIcon(tool);
    }
  }

  _agentDone(agentId, ok, summary) {
    const el = this.nodes.get(agentId);
    if (!el) return;

    const statusEl = el.querySelector(".agent-status");
    if (statusEl) {
      statusEl.className = `agent-status ${ok ? "done" : "error"}`;
    }

    if (summary) {
      const actionEl = el.querySelector(".agent-action");
      if (actionEl) actionEl.textContent = summary.slice(0, 60);
    }

    el.classList.add("completed");
  }

  _render() {
    const hasActive = [...this.tasks.values()].some(t => !t.done);
    this.container.classList.toggle("hidden", !hasActive && this.tasks.size === 0);
  }

  _toolIcon(tool) {
    const icons = {
      web_search: "&#128269;",
      browser_control: "&#127760;",
      file_controller: "&#128193;",
      file_processor: "&#128196;",
      vision: "&#128065;",
      computer_control: "&#128187;",
      computer_settings: "&#9881;",
      open_app: "&#128229;",
      close_app: "&#10060;",
      planner: "&#128203;",
      nova_task: "&#9889;",
      remember_fact: "&#129504;",
      nova_memory: "&#128218;",
      self_editor: "&#9998;",
    };
    return icons[tool] || "&#128295;";
  }

  _esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
  }
}

// Export
if (typeof module !== "undefined" && module.exports) {
  module.exports = { AgentField };
} else {
  window.AgentField = AgentField;
}
