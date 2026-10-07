/**
 * <dorm-room-card> — R66 (Web Components refactor)
 *
 * Wraps a single meter-grid card (vol / cur / yggl / status /
 * update_dt).  Same structure as <dorm-stat-card> but with an extra
 * `data-tone` attribute for "online" / "offline" / "warning" colour
 * cues used by the meter-status-pill.
 *
 * Attributes:
 *   data-label     — card-label text
 *   data-tone      — "" | "online" | "offline" | "warning"
 *   data-empty     — "true" | "false"
 *   data-fontsize  — "" | "small"   (18px for status-pill + update_dt)
 *
 * Children:
 *   Whatever existed inside the <dorm-room-card> at parse time stays
 *   verbatim; only the wrapping .card, the data-label header are added.
 *   The existing meter-* spans remain reachable via getElementById so
 *   dashboard.js updateMeter() can still setCell('meter-vol', ...).
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<div class="card">',
    '  <div class="card-label"></div>',
    '</div>',
  ].join('\n');

  class DormRoomCard extends HTMLElement {
    static get observedAttributes() {
      return ['data-label', 'data-tone', 'data-empty', 'data-fontsize'];
    }

    constructor() {
      super();
      var frag = tpl.content.cloneNode(true);
      this._card = frag.firstElementChild;
      this._label = this._card.querySelector('.card-label');
      this.appendChild(this._card);
      // Move original light children to the right of the label.
      this._card.appendChild(this._label);
      while (this.firstChild) {
        this._card.appendChild(this.firstChild);
      }
    }

    connectedCallback() { this._sync(); }
    attributeChangedCallback() { this._sync(); }

    _sync() {
      if (!this._label) return;
      this._label.textContent = this.getAttribute('data-label') || '';
      var empty = this.getAttribute('data-empty') === 'true';
      var value = this._card.querySelector('.card-value');
      if (value) value.classList.toggle('is-empty', empty);
      var tone = this.getAttribute('data-tone') || '';
      var color = '';
      if (tone === 'online') color = 'var(--online)';
      else if (tone === 'offline') color = 'var(--offline)';
      else if (tone === 'warning') color = 'var(--warning)';
      if (value) value.style.color = color;
      var fs = this.getAttribute('data-fontsize');
      if (value) value.style.fontSize = fs === 'small' ? '18px' : '';
    }
  }

  customElements.define('dorm-room-card', DormRoomCard);
})();