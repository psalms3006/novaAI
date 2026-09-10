/**
 * NOVA Event Client — WebSocket connection to /ws/events.
 *
 * Receives: voice_state, transcript, task_start/done, agent_start/progress/done, orb_state
 * Dispatches to registered handlers.
 */
"use strict";

class NovaEvents {
  constructor(token) {
    this.token = token;
    this.ws = null;
    this.handlers = {};
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 30000;
    this.connected = false;
  }

  on(eventType, handler) {
    if (!this.handlers[eventType]) this.handlers[eventType] = [];
    this.handlers[eventType].push(handler);
  }

  off(eventType, handler) {
    if (!this.handlers[eventType]) return;
    this.handlers[eventType] = this.handlers[eventType].filter(h => h !== handler);
  }

  connect() {
    if (this.ws && this.ws.readyState <= 1) return;

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws/events?token=${this.token}`;

    try {
      this.ws = new WebSocket(url);
    } catch (e) {
      console.warn("[nova-events] WebSocket creation failed:", e);
      this._scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.connected = true;
      this.reconnectDelay = 1000;
      console.log("[nova-events] connected");
      this._dispatch({ type: "_connected" });
    };

    this.ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        this._dispatch(data);
      } catch (e) {
        console.warn("[nova-events] parse error:", e);
      }
    };

    this.ws.onclose = () => {
      this.connected = false;
      console.log("[nova-events] disconnected");
      this._dispatch({ type: "_disconnected" });
      this._scheduleReconnect();
    };

    this.ws.onerror = (e) => {
      console.warn("[nova-events] WebSocket error:", e);
    };
  }

  disconnect() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.connected = false;
  }

  _dispatch(event) {
    const type = event.type;
    if (this.handlers[type]) {
      for (const h of this.handlers[type]) {
        try { h(event); } catch (e) { console.error("[nova-events] handler error:", e); }
      }
    }
    // Also dispatch to wildcard handlers
    if (this.handlers["*"]) {
      for (const h of this.handlers["*"]) {
        try { h(event); } catch (e) { console.error("[nova-events] handler error:", e); }
      }
    }
  }

  _scheduleReconnect() {
    setTimeout(() => {
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxReconnectDelay);
      this.connect();
    }, this.reconnectDelay);
  }
}

// Export
if (typeof module !== "undefined" && module.exports) {
  module.exports = { NovaEvents };
} else {
  window.NovaEvents = NovaEvents;
}
