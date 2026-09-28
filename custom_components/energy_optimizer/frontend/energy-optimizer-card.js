/**
 * energy-optimizer-card.js
 *
 * Minimal custom Lovelace card for the Energy Optimizer integration.
 * No build step, no framework -- a plain Web Component, styled with Home
 * Assistant's own CSS variables so it follows the active theme automatically.
 *
 * Expects a sensor.<appliance>_recommended_start entity (state = ISO
 * datetime) with these attributes:
 *   - expected_solar_coverage_pct : number 0-100
 *   - confidence                  : "baseline" | "personalized"
 *   - basis                       : short human-readable reason string
 */

class EnergyOptimizerCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("You must set 'entity' to a recommended-start sensor");
    }
    this._config = config;
    this._built = false;
  }

  set hass(hass) {
    this._hass = hass;
    const stateObj = hass.states[this._config.entity];

    if (!this._built) {
      this._buildDom();
      this._built = true;
    }

    if (!stateObj) {
      this._elements.time.textContent = "Entity not found";
      this._elements.meta.textContent = this._config.entity;
      return;
    }

    this._render(stateObj);
  }

  _buildDom() {
    const card = document.createElement("ha-card");
    card.header = this._config.name || "Energy Optimizer";

    const style = document.createElement("style");
    style.textContent = `
      .wrap { padding: 0 16px 16px; }
      .row { display: flex; align-items: center; gap: 12px; }
      .icon { color: var(--state-icon-color, var(--paper-item-icon-color)); }
      .time { font-size: 28px; font-weight: 500; color: var(--primary-text-color); }
      .meta { font-size: 13px; color: var(--secondary-text-color); margin-top: 2px; }
      .bar-track { height: 6px; border-radius: 3px; background: var(--divider-color); margin: 12px 0 4px; overflow: hidden; }
      .bar-fill { height: 100%; background: var(--warning-color, #f5a623); transition: width .4s ease; }
      .badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 10px;
               background: var(--secondary-background-color); color: var(--secondary-text-color); margin-top: 8px; }
      .actions { display: flex; gap: 8px; margin-top: 14px; }
      button { flex: 1; padding: 8px 0; border-radius: var(--ha-card-border-radius, 8px);
               border: none; font-size: 13px; cursor: pointer; }
      .accept { background: var(--primary-color); color: var(--text-primary-color, #fff); }
      .change { background: var(--secondary-background-color); color: var(--primary-text-color); }
    `;

    const wrap = document.createElement("div");
    wrap.className = "wrap";
    wrap.innerHTML = `
      <div class="row">
        <ha-icon class="icon" icon="${this._config.icon || "mdi:power-plug"}"></ha-icon>
        <div>
          <div class="time"></div>
          <div class="meta"></div>
        </div>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:0%"></div></div>
      <div class="badge"></div>
      <div class="actions">
        <button class="accept">Accept</button>
        <button class="change">Pick different time</button>
      </div>
    `;

    card.appendChild(style);
    card.appendChild(wrap);
    this.appendChild(card);

    this._elements = {
      time: wrap.querySelector(".time"),
      meta: wrap.querySelector(".meta"),
      barFill: wrap.querySelector(".bar-fill"),
      badge: wrap.querySelector(".badge"),
      acceptBtn: wrap.querySelector(".accept"),
      changeBtn: wrap.querySelector(".change"),
    };

    this._elements.acceptBtn.addEventListener("click", () => this._callService("accept_suggestion"));
    this._elements.changeBtn.addEventListener("click", () => this._callService("pick_different_time"));
  }

  _render(stateObj) {
    const attrs = stateObj.attributes || {};
    const dt = new Date(stateObj.state);
    const validDate = !Number.isNaN(dt.getTime());

    this._elements.time.textContent = validDate
      ? dt.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })
      : stateObj.state;

    this._elements.meta.textContent = attrs.basis || "Recommended start time";

    const coverage = Math.max(0, Math.min(100, Number(attrs.expected_solar_coverage_pct) || 0));
    this._elements.barFill.style.width = `${coverage}%`;

    this._elements.badge.textContent =
      attrs.confidence === "personalized"
        ? `Personalized \u00b7 ${coverage}% solar coverage`
        : `Baseline \u00b7 ${coverage}% solar coverage`;
  }

  _callService(service) {
    if (!this._hass) return;
    this._hass.callService("energy_optimizer", service, { entity_id: this._config.entity });
  }

  getCardSize() {
    return 3;
  }
}

customElements.define("energy-optimizer-card", EnergyOptimizerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "energy-optimizer-card",
  name: "Energy Optimizer Card",
  description: "Shows the recommended next start time for an appliance, with solar coverage and accept/change actions.",
});
