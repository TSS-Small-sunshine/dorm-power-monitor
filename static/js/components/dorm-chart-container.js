/**
 * <dorm-chart-container> — R66 (Web Components refactor)
 *
 * Wraps a chart panel: title (.panel-title), optional sub label
 * (.panel-sub), an optional range button group, and a <canvas> that the
 * existing dashboard.js buildChart() / buildDailyChart() still queries
 * by ID.  The component sets a view-transition-name on the inner
 * chart-wrap so chart re-renders can cross-fade via the View
 * Transitions API.
 *
 * Attributes:
 *   data-title     — panel title text
 *   data-sub       — optional sub-text under the title
 *   data-vt-name   — view-transition-name token (default "chart-canvas")
 *
 * Children:
 *   Anything tagged slot="head" lands inside .panel-head; everything
 *   else lands inside .chart-wrap.  This way the legacy <canvas
 *   id="chart"> stays findable via document.getElementById() and the
 *   existing range-group can be slotted into the head if present.
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<div class="panel">',
    '  <div class="panel-head">',
    '    <h5 class="panel-title"></h5>',
    '    <div class="panel-head-slot"></div>',
    '  </div>',
    '  <div class="chart-wrap"></div>',
    '</div>',
  ].join('\n');

  class DormChartContainer extends HTMLElement {
    static get observedAttributes() {
      return ['data-title', 'data-sub', 'data-vt-name'];
    }

    constructor() {
      super();
      var frag = tpl.content.cloneNode(true);
      this._panel = frag.querySelector('.panel');
      this._title = frag.querySelector('.panel-title');
      this._headSlot = frag.querySelector('.panel-head-slot');
      this._wrap = frag.querySelector('.chart-wrap');
      this.appendChild(this._panel);
      // Distribute light children: anything with class panel-sub or with
      // slot="head" goes into the panel head; everything else into
      // chart-wrap.
      var headKids = [];
      var bodyKids = [];
      while (this.firstChild) {
        var c = this.firstChild;
        if (
          c.nodeType === 1 &&
          (
            (c.getAttribute && c.getAttribute('slot') === 'head') ||
            (c.classList && (c.classList.contains('panel-sub') || c.classList.contains('range-group')))
          )
        ) {
          headKids.push(c);
        } else {
          bodyKids.push(c);
        }
      }
      // Move them out of `this` (they were direct children); appendChild
      // moves nodes, so we re-attach in order.
      headKids.forEach(function (c) { this._headSlot.appendChild(c); }.bind(this));
      bodyKids.forEach(function (c) { this._wrap.appendChild(c); }.bind(this));
    }

    connectedCallback() { this._sync(); }
    attributeChangedCallback() { this._sync(); }

    _sync() {
      if (!this._title) return;
      this._title.textContent = this.getAttribute('data-title') || '';
      var sub = this.getAttribute('data-sub') || '';
      // Inject a sub label span next to title when data-sub is set, so
      // we don't need a separate <span class="panel-sub"> child.
      var subEl = this._panel.querySelector('.panel-sub-auto');
      if (sub) {
        if (!subEl) {
          subEl = document.createElement('span');
          subEl.className = 'panel-sub panel-sub-auto';
          this._title.parentNode.appendChild(subEl);
        }
        subEl.textContent = sub;
      } else if (subEl) {
        subEl.parentNode.removeChild(subEl);
      }
      var vt = this.getAttribute('data-vt-name') || 'chart-canvas';
      this._wrap.style.viewTransitionName = vt;
    }
  }

  customElements.define('dorm-chart-container', DormChartContainer);
})();