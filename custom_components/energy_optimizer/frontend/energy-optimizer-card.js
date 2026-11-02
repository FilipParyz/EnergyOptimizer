/**
 * energy-optimizer-card.js
 *
 * Custom Lovelace card for the Energy Optimizer integration.
 * No build step, no framework -- a plain Web Component in a shadow root,
 * styled with Home Assistant's CSS variables so it follows the active theme.
 *
 * Reads a sensor.<appliance>_recommended_start entity (state = ISO datetime)
 * and its attributes:
 *   expected_solar_coverage_pct, confidence, basis      -- the recommendation
 *   suggestion_status   pending | accepted | matched | missed | overridden | expired | replaced
 *   appliance_state     idle | running
 *   running_since       ISO datetime while running
 *   current_run_fit     {fit, suggested_hour} for the run in progress
 *   last_run            {start, end, duration_min, energy_wh, fit, suggested_hour}
 *   power_entity        entity id of the appliance's power sensor (live watts)
 *
 * Config:
 *   type: custom:energy-optimizer-card
 *   entity: sensor.pralka_recommended_start
 *   name: Pralka            (optional, defaults to the device name)
 *   icon: mdi:washing-machine (optional, defaults to the entity icon)
 */

const STRINGS = {
  en: {
    idle: "Idle",
    running: "Running",
    since: "since",
    recommended: "Recommended start",
    noRecommendation: "No recommendation yet",
    solar: "solar coverage",
    baseline: "Baseline",
    personalized: "Personalized",
    lastRun: "Last run",
    noRuns: "No runs recorded yet",
    accept: "Accept",
    other: "Pick different time",
    accepted: "Suggestion accepted",
    overridden: "Marked as a different time",
    failed: "Action failed",
    notFound: "Entity not found",
    today: "today",
    tomorrow: "tomorrow",
    fit: {
      in_window: "within the suggested window",
      outside_window: "outside the suggested window",
      no_suggestion: "no suggestion for that day",
    },
    suggested: "suggested",
    status: {
      pending: "Waiting for your decision",
      accepted: "Accepted",
      matched: "Started as suggested",
      missed: "Started outside the suggestion",
      overridden: "Different time chosen",
      expired: "Not started",
      replaced: "Replaced by a newer suggestion",
    },
  },
  pl: {
    idle: "Bezczynne",
    running: "Pracuje",
    since: "od",
    recommended: "Sugerowany start",
    noRecommendation: "Brak sugestii",
    solar: "pokrycia z PV",
    baseline: "Bazowa",
    personalized: "Spersonalizowana",
    lastRun: "Ostatnie uruchomienie",
    noRuns: "Brak zapisanych uruchomień",
    accept: "Akceptuj",
    other: "Wybiorę inną porę",
    accepted: "Sugestia zaakceptowana",
    overridden: "Zapisano wybór innej pory",
    failed: "Nie udało się wykonać akcji",
    notFound: "Nie znaleziono encji",
    today: "dziś",
    tomorrow: "jutro",
    fit: {
      in_window: "w sugerowanym oknie",
      outside_window: "poza sugerowanym oknem",
      no_suggestion: "brak sugestii na ten dzień",
    },
    suggested: "sugestia",
    status: {
      pending: "Czeka na decyzję",
      accepted: "Zaakceptowana",
      matched: "Uruchomiono zgodnie z sugestią",
      missed: "Uruchomiono poza sugestią",
      overridden: "Wybrano inną porę",
      expired: "Nie uruchomiono",
      replaced: "Zastąpiona nowszą",
    },
  },
};

// Colour per outcome: good / bad / neutral.
const FIT_TONE = { in_window: "ok", outside_window: "warn", no_suggestion: "neutral" };
const STATUS_TONE = {
  pending: "info", accepted: "info", matched: "ok", missed: "warn",
  overridden: "neutral", expired: "neutral", replaced: "neutral",
};

class EnergyOptimizerCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("You must set 'entity' to a recommended-start sensor");
    }
    this._config = config;
    this._busy = false;
    if (this._root) this._root.innerHTML = "";
    this._built = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._buildDom();
    this._render();
  }

  // ---------- helpers ----------

  get _t() {
    const lang = (this._hass?.locale?.language || this._hass?.language || "en").slice(0, 2);
    return STRINGS[lang] || STRINGS.en;
  }

  get _locale() {
    return this._hass?.locale?.language || this._hass?.language || undefined;
  }

  _hhmm(date) {
    return date.toLocaleTimeString(this._locale, { hour: "2-digit", minute: "2-digit" });
  }

  _dayLabel(date) {
    const t = this._t;
    const startOf = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const diff = Math.round((startOf(date) - startOf(new Date())) / 86400000);
    if (diff === 0) return t.today;
    if (diff === 1) return t.tomorrow;
    return date.toLocaleDateString(this._locale, { weekday: "short", day: "numeric", month: "short" });
  }

  _when(date) {
    return `${this._dayLabel(date)}, ${this._hhmm(date)}`;
  }

  _fitText(fit, suggestedHour) {
    const t = this._t;
    if (!fit) return "";
    const text = t.fit[fit] || fit;
    if (fit === "no_suggestion" || suggestedHour == null) return text;
    const h = String(suggestedHour).padStart(2, "0");
    return `${text} (${t.suggested} ${h}:00 ±1 h)`;
  }

  _toast(message) {
    this.dispatchEvent(new CustomEvent("hass-notification", {
      detail: { message }, bubbles: true, composed: true,
    }));
  }

  _moreInfo() {
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      detail: { entityId: this._config.entity }, bubbles: true, composed: true,
    }));
  }

  // ---------- DOM ----------

  _buildDom() {
    this._root = this._root || this.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>
        ha-card { overflow: hidden; }
        .top { display: flex; align-items: center; gap: 12px; padding: 16px 16px 0;
               cursor: pointer; }
        .top ha-icon { --mdc-icon-size: 32px; color: var(--state-icon-color); }
        .top.running ha-icon { color: var(--state-active-color, var(--amber-color, #ffc107)); }
        .title { flex: 1; min-width: 0; }
        .name { font-size: 16px; font-weight: 500; color: var(--primary-text-color);
                white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .state { font-size: 13px; color: var(--secondary-text-color); margin-top: 2px; }
        .chip { display: inline-flex; align-items: center; gap: 4px; font-size: 12px;
                padding: 2px 8px; border-radius: 12px; white-space: nowrap;
                background: var(--secondary-background-color); color: var(--secondary-text-color); }
        .chip.ok { background: rgba(var(--rgb-success-color, 67, 160, 71), .15); color: var(--success-color, #43a047); }
        .chip.warn { background: rgba(var(--rgb-warning-color, 255, 166, 0), .18); color: var(--warning-color, #ffa600); }
        .chip.info { background: rgba(var(--rgb-primary-color, 3, 169, 244), .15); color: var(--primary-color); }
        .chip ha-icon { --mdc-icon-size: 14px; }
        [hidden] { display: none !important; }
        .section { padding: 12px 16px 0; cursor: pointer; }
        .label { font-size: 12px; color: var(--secondary-text-color); text-transform: uppercase;
                 letter-spacing: .04em; }
        .time { font-size: 26px; font-weight: 500; color: var(--primary-text-color); margin-top: 2px; }
        .basis { font-size: 13px; color: var(--secondary-text-color); margin-top: 2px; }
        .bar-track { height: 6px; border-radius: 3px; background: var(--divider-color);
                     margin: 10px 0 4px; overflow: hidden; }
        .bar-fill { height: 100%; width: 0; background: var(--warning-color, #ffa600); transition: width .4s ease; }
        .row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 6px;
               font-size: 13px; color: var(--secondary-text-color); }
        .last { font-size: 14px; color: var(--primary-text-color); margin-top: 2px; }
        .actions { display: flex; gap: 8px; padding: 14px 16px 16px; }
        .actions[hidden] { display: none; }
        button { flex: 1; padding: 9px 0; border-radius: var(--ha-card-border-radius, 8px);
                 border: none; font: inherit; font-size: 14px; cursor: pointer; }
        button:disabled { opacity: .5; cursor: default; }
        .accept { background: var(--primary-color); color: var(--text-primary-color, #fff); }
        .other { background: var(--secondary-background-color); color: var(--primary-text-color); }
        .spacer { height: 16px; }
        .error { padding: 16px; color: var(--error-color); }
      </style>
      <ha-card>
        <div class="error" hidden></div>
        <div class="body">
          <div class="top">
            <ha-icon></ha-icon>
            <div class="title">
              <div class="name"></div>
              <div class="state"></div>
            </div>
            <span class="chip fit-now" hidden></span>
          </div>

          <div class="section rec">
            <div class="label"></div>
            <div class="time"></div>
            <div class="basis"></div>
            <div class="bar-track"><div class="bar-fill"></div></div>
            <div class="row">
              <span class="coverage"></span>
              <span class="chip confidence"></span>
              <span class="chip status"></span>
            </div>
          </div>

          <div class="section last-run">
            <div class="label last-label"></div>
            <div class="last"></div>
            <div class="row"><span class="chip fit-last" hidden></span></div>
          </div>

          <div class="actions">
            <button class="accept"></button>
            <button class="other"></button>
          </div>
          <div class="spacer" hidden></div>
        </div>
      </ha-card>
    `;

    const q = (s) => this._root.querySelector(s);
    this._el = {
      error: q(".error"), body: q(".body"), top: q(".top"), icon: q(".top ha-icon"),
      name: q(".name"), state: q(".state"), fitNow: q(".fit-now"),
      recLabel: q(".rec .label"), time: q(".time"), basis: q(".basis"), bar: q(".bar-fill"),
      coverage: q(".coverage"), confidence: q(".confidence"), status: q(".status"),
      lastLabel: q(".last-label"), last: q(".last"), fitLast: q(".fit-last"),
      actions: q(".actions"), accept: q(".accept"), other: q(".other"), spacer: q(".spacer"),
    };

    // Tapping anywhere outside the buttons opens the entity's more-info dialog.
    for (const el of [this._el.top, q(".rec"), q(".last-run")]) {
      el.addEventListener("click", () => this._moreInfo());
    }
    this._el.accept.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._callService("accept_suggestion", this._t.accepted);
    });
    this._el.other.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this._callService("pick_different_time", this._t.overridden);
    });

    this._built = true;
  }

  _setChip(el, text, tone, icon) {
    if (!text) {
      el.hidden = true;
      return;
    }
    el.hidden = false;
    el.className = `${el.className.split(" ").filter((c) => !["ok", "warn", "info", "neutral"].includes(c)).join(" ")} ${tone || ""}`.trim();
    el.innerHTML = icon ? `<ha-icon icon="${icon}"></ha-icon>` : "";
    el.appendChild(document.createTextNode(text));
  }

  _render() {
    const t = this._t;
    const el = this._el;
    const stateObj = this._hass.states[this._config.entity];

    if (!stateObj) {
      el.body.hidden = true;
      el.error.hidden = false;
      el.error.textContent = `${t.notFound}: ${this._config.entity}`;
      return;
    }
    el.body.hidden = false;
    el.error.hidden = true;

    const a = stateObj.attributes || {};

    // --- header: name + live state ---
    const deviceName = (a.friendly_name || "").replace(/\s*(Recommended start|Sugerowany start)$/i, "");
    el.name.textContent = this._config.name || deviceName || "Energy Optimizer";
    el.icon.setAttribute("icon", this._config.icon || a.icon || "mdi:power-plug");

    const running = a.appliance_state === "running";
    el.top.classList.toggle("running", running);
    let stateText = running ? t.running : t.idle;
    if (running && a.running_since) {
      stateText += ` ${t.since} ${this._hhmm(new Date(a.running_since))}`;
    }
    const power = a.power_entity ? this._hass.states[a.power_entity] : undefined;
    if (power && !Number.isNaN(Number(power.state))) {
      stateText += ` · ${Math.round(Number(power.state))} W`;
    }
    el.state.textContent = stateText;

    const nowFit = running && a.current_run_fit ? a.current_run_fit : null;
    this._setChip(
      el.fitNow,
      nowFit ? t.fit[nowFit.fit] || nowFit.fit : "",
      nowFit ? FIT_TONE[nowFit.fit] : "",
      nowFit && nowFit.fit === "in_window" ? "mdi:check-circle" : nowFit ? "mdi:alert-circle-outline" : null,
    );

    // --- recommendation ---
    el.recLabel.textContent = t.recommended;
    const rec = new Date(stateObj.state);
    const hasRec = !Number.isNaN(rec.getTime());
    el.time.textContent = hasRec ? this._when(rec) : t.noRecommendation;
    el.basis.textContent = hasRec ? a.basis || "" : "";

    const coverage = Math.max(0, Math.min(100, Number(a.expected_solar_coverage_pct) || 0));
    el.bar.style.width = hasRec ? `${coverage}%` : "0";
    el.coverage.textContent = hasRec ? `${Math.round(coverage)}% ${t.solar}` : "";
    this._setChip(el.confidence, hasRec && a.confidence
      ? (a.confidence === "personalized" ? t.personalized : t.baseline) : "", "neutral");
    const status = a.suggestion_status;
    this._setChip(el.status, hasRec && status ? t.status[status] || status : "", STATUS_TONE[status]);

    // --- last run ---
    el.lastLabel.textContent = t.lastRun;
    const last = a.last_run;
    if (last && last.start) {
      const parts = [this._when(new Date(last.start))];
      if (last.duration_min != null) {
        const h = Math.floor(last.duration_min / 60);
        const m = last.duration_min % 60;
        parts.push(h ? `${h} h ${m} min` : `${m} min`);
      }
      if (last.energy_wh) parts.push(`${(last.energy_wh / 1000).toFixed(2)} kWh`);
      el.last.textContent = parts.join(" · ");
      this._setChip(
        el.fitLast,
        this._fitText(last.fit, last.suggested_hour),
        FIT_TONE[last.fit],
        last.fit === "in_window" ? "mdi:check-circle" : last.fit === "outside_window" ? "mdi:alert-circle-outline" : null,
      );
    } else {
      el.last.textContent = t.noRuns;
      el.fitLast.hidden = true;
    }

    // --- actions: only meaningful while a suggestion waits for a decision ---
    const actionable = hasRec && status === "pending";
    el.actions.hidden = !actionable;
    el.spacer.hidden = actionable;
    el.accept.textContent = t.accept;
    el.other.textContent = t.other;
    el.accept.disabled = el.other.disabled = this._busy;
  }

  async _callService(service, okMessage) {
    if (!this._hass || this._busy) return;
    this._busy = true;
    this._render();
    try {
      await this._hass.callService("energy_optimizer", service, {}, { entity_id: this._config.entity });
      this._toast(okMessage);
    } catch (err) {
      this._toast(`${this._t.failed}: ${err && err.message ? err.message : err}`);
    } finally {
      this._busy = false;
      if (this._hass) this._render();
    }
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find(
      (id) => id.startsWith("sensor.") && "suggestion_status" in (hass.states[id].attributes || {}),
    );
    return { entity: entity || "" };
  }
}

if (!customElements.get("energy-optimizer-card")) {
  customElements.define("energy-optimizer-card", EnergyOptimizerCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "energy-optimizer-card",
    name: "Energy Optimizer Card",
    description: "Recommended start time, live appliance state and whether runs followed the suggestion.",
    preview: true,
  });
}
