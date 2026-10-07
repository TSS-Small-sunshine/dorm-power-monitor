/**
 * <dorm-records-table> — R66 (Web Components refactor)
 *
 * Decorates the existing records-tbody / records-empty / records-sub
 * group with a view-transition-name and exposes a `.rows` JS property
 * + `data-rows` JSON attribute that the records-filter inside
 * dashboard.js can use to re-render rows on data refresh.
 *
 * Attributes:
 *   data-rows     — JSON array of { ts, read_time, remain }
 *   data-vt-name  — view-transition-name for re-render animation
 *
 * Properties:
 *   .rows         — Array (preferred)
 *
 * Children:
 *   The original <table class="data-table">...</table> markup stays
 *   intact so the dashboard.js records-filter keeps working.
 */
(function () {
  class DormRecordsTable extends HTMLElement {
    static get observedAttributes() {
      return ['data-rows', 'data-vt-name'];
    }

    constructor() {
      super();
      this._rows = [];
    }

    get rows() { return this._rows; }
    set rows(v) {
      this._rows = Array.isArray(v) ? v : [];
      this._render();
    }

    connectedCallback() {
      this._sync();
    }

    attributeChangedCallback(name) {
      if (name === 'data-rows') {
        try {
          this._rows = JSON.parse(this.getAttribute('data-rows') || '[]');
          this._render();
        } catch (e) { /* ignore malformed JSON */ }
      } else if (name === 'data-vt-name') {
        var vt = this.getAttribute('data-vt-name') || 'records-table';
        this.style.viewTransitionName = vt;
      }
    }

    _sync() {
      var vt = this.getAttribute('data-vt-name') || 'records-table';
      this.style.viewTransitionName = vt;
    }

    _render() {
      // Render into the <tbody> if one is found in light DOM; otherwise
      // show empty state.
      var tbody = this.querySelector('tbody');
      var empty = this.querySelector('#records-empty');
      if (!tbody) return;
      // Don't blow away anything that isn't a <dorm-history-row>.
      // Defensive: only remove <dorm-history-row> nodes so the static
      // SSR table from the template stays.
      Array.from(tbody.querySelectorAll('dorm-history-row')).forEach(function (r) {
        r.parentNode.removeChild(r);
      });
      if (!this._rows.length) {
        if (empty) empty.style.display = 'block';
        return;
      }
      if (empty) empty.style.display = 'none';
      var frag = document.createDocumentFragment();
      var list = this._rows.slice().reverse();
      list.forEach(function (r) {
        var row = document.createElement('dorm-history-row');
        row.setAttribute('data-ts', r && r.ts ? String(r.ts) : '');
        row.setAttribute(
          'data-read-time',
          r && r.read_time ? String(r.read_time) : ''
        );
        row.setAttribute(
          'data-remain',
          r && r.remain != null && !isNaN(Number(r.remain)) ? String(r.remain) : ''
        );
        frag.appendChild(row);
      });
      tbody.appendChild(frag);
    }
  }

  customElements.define('dorm-records-table', DormRecordsTable);
})();