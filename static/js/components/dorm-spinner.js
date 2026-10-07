/**
 * <dorm-spinner> — R66 (Web Components refactor)
 *
 * Decorative spinning glyph for the manual-refresh button.  Toggles its
 * own "spinning" attribute when the host button enters its loading
 * state (class .loading on the closest .refresh-btn ancestor), so it
 * animates without any extra wiring.
 *
 * Attributes:
 *   glyph     — text glyph shown (default "⟳")
 *   speed     — animation duration in ms (default 1000)
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<style>',
    '  :host {',
    '    display: inline-block;',
    '    transition: transform 0.6s;',
    '  }',
    '  :host([spinning]) {',
    '    animation: dorm-spin var(--dorm-spin-speed, 1s) linear infinite;',
    '  }',
    '  @keyframes dorm-spin {',
    '    from { transform: rotate(0deg); }',
    '    to   { transform: rotate(360deg); }',
    '  }',
    '</style>',
    '<span class="dorm-spin-glyph"></span>',
  ].join('\n');

  class DormSpinner extends HTMLElement {
    static get observedAttributes() {
      return ['glyph', 'speed', 'spinning'];
    }

    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(tpl.content.cloneNode(true));
      this._glyph = this.shadowRoot.querySelector('.dorm-spin-glyph');
    }

    connectedCallback() {
      this._sync();
    }

    attributeChangedCallback() {
      this._sync();
    }

    _sync() {
      if (!this._glyph) return;
      this._glyph.textContent = this.getAttribute('glyph') || '⟳';
      var speed = this.getAttribute('speed') || '1000';
      this.style.setProperty('--dorm-spin-speed', speed + 'ms');
      this.toggleAttribute('spinning', this.hasAttribute('spinning'));
    }
  }

  customElements.define('dorm-spinner', DormSpinner);
})();