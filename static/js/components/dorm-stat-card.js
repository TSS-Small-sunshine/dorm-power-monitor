/**
 * <dorm-stat-card> — R66 (Web Components refactor)
 *
 * Wraps the recurring ".card / .card-label / .card-value / .card-foot"
 * stat block used in the overview section (monthly projection, hourly,
 * daily avg, read time).  Inherits HTMLElement, uses native <template> +
 * cloneNode, and reads its data from data-* attributes.  Light DOM so
 * the legacy getElementById('stat-hourly') / 'stat-daily' / etc.
 * queries inside dashboard.js keep finding the live nodes.
 *
 * Attributes:
 *   data-label  — top label text
 *   data-foot   — bottom foot text
 *   data-empty  — "true" | "false"  (toggles .is-empty class on value)
 *
 * Children:
 *   Whatever existed inside the <dorm-stat-card> at parse time stays
 *   verbatim; only the wrapping .card, the data-label header and the
 *   data-foot footer are added.
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<div class="card">',
    '  <div class="card-label"></div>',
    '  <div class="card-foot"></div>',
    '</div>',
  ].join('\n');

  class DormStatCard extends HTMLElement {
    static get observedAttributes() {
      return ['data-label', 'data-foot', 'data-empty'];
    }

    constructor() {
      super();
      var frag = tpl.content.cloneNode(true);
      this._card = frag.firstElementChild;
      this._label = this._card.querySelector('.card-label');
      this._foot = this._card.querySelector('.card-foot');
      // Move original light children into the .card, between label and foot.
      // appendChild moves the node, so the loop is safe even if label/foot
      // are not yet attached.
      this.appendChild(this._card);
      this._card.appendChild(this._label);
      while (this.firstChild) {
        this._card.appendChild(this.firstChild);
      }
      this._card.appendChild(this._foot);
    }

    connectedCallback() { this._sync(); }

    attributeChangedCallback() { this._sync(); }

    _sync() {
      if (!this._label) return;
      this._label.textContent = this.getAttribute('data-label') || '';
      this._foot.textContent = this.getAttribute('data-foot') || '';
      var empty = this.getAttribute('data-empty') === 'true';
      // Find the first .card-value child (or the host's direct child if
      // it has the .card-value class) and toggle .is-empty on it.
      var value = this._card.querySelector('.card-value');
      if (value) value.classList.toggle('is-empty', empty);
    }
  }

  customElements.define('dorm-stat-card', DormStatCard);
})();