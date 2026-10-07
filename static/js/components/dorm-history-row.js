/**
 * <dorm-history-row> — R66 (Web Components refactor)
 *
 * Represents a single row in the records table.  The component reads
 * data-* attributes and renders its own <td> cells directly inside
 * the host, which uses `display: table-row` via CSS so it slots into
 * <tbody> as a regular row.  Using <td> directly (instead of nesting
 * <tr>) avoids the HTML parser's quirks around non-<tr> children
 * inside <tbody>.
 *
 * Attributes:
 *   data-ts        — collection timestamp (e.g. "2026-09-19 14:30:00")
 *   data-read-time — meter read time (e.g. "2026-09-19 14:00:00")
 *   data-remain    — remaining kW·h (number | null)
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<td class="ts"></td>',
    '<td class="ts"></td>',
    '<td class="text-end"></td>',
  ].join('\n');

  class DormHistoryRow extends HTMLElement {
    static get observedAttributes() {
      return ['data-ts', 'data-read-time', 'data-remain'];
    }

    constructor() {
      super();
      var frag = tpl.content.cloneNode(true);
      this._cells = frag.querySelectorAll('td');
      // Move <td> cells directly into the host.  Combined with the
      // `display: table-row` CSS rule, this makes the host render as
      // a single <tr>.
      this._cells.forEach(function (td) { this.appendChild(td); }.bind(this));
    }

    connectedCallback() { this._sync(); }
    attributeChangedCallback() { this._sync(); }

    _sync() {
      if (!this._cells || this._cells.length < 3) return;
      this._cells[0].textContent = this.getAttribute('data-ts') || '—';
      this._cells[1].textContent = this.getAttribute('data-read-time') || '—';
      var remain = this.getAttribute('data-remain');
      var display = '—';
      if (remain !== null && remain !== '' && remain != null) {
        var n = Number(remain);
        if (!isNaN(n)) display = n.toFixed(2);
      }
      this._cells[2].textContent = display;
    }
  }

  customElements.define('dorm-history-row', DormHistoryRow);
})();