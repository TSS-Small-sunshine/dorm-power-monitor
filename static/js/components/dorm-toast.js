/**
 * <dorm-toast> — R66 (Web Components refactor)
 *
 * Self-contained toast notification widget.  Uses **Shadow DOM** with
 * a <slot> so callers can write
 *   <dorm-toast kind="error">连接失败</dorm-toast>
 * and have the body projected into the rendered template.
 *
 * Attributes:
 *   kind      — "info" | "success" | "error"
 *   duration  — auto-dismiss timeout in ms (0 = sticky). Default 3000.
 *
 * The toast emits a `dorm-toast-leave` CustomEvent before it removes
 * itself so callers can wrap removal in document.startViewTransition
 * to get a from-bottom fade.  When the View Transitions API is
 * available, the component requests the transition itself; otherwise it
 * falls back to the existing CSS keyframes (.toast { animation: ... }).
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<style>',
    '  :host {',
    '    display: block;',
    '    pointer-events: auto;',
    '    view-transition-name: toast-stack;',
    '  }',
    '  .toast {',
    '    padding: 12px 16px;',
    '    background: var(--bg-card, #131316);',
    '    border: 1px solid var(--border, rgba(255,255,255,0.08));',
    '    border-radius: 8px;',
    '    color: var(--text-primary, #fafafa);',
    '    font-size: 13px;',
    '    display: flex;',
    '    align-items: center;',
    '    gap: 10px;',
    '    min-width: 200px;',
    '    max-width: 360px;',
    '    animation: dorm-slide-in 0.2s ease-out;',
    '  }',
    '  .toast.leaving { animation: dorm-slide-out 0.2s ease-in forwards; }',
    '  @keyframes dorm-slide-in {',
    '    from { transform: translateY(20px); opacity: 0; }',
    '    to   { transform: translateY(0);    opacity: 1; }',
    '  }',
    '  @keyframes dorm-slide-out {',
    '    to { transform: translateY(20px); opacity: 0; }',
    '  }',
    '  .toast.success { border-left: 3px solid var(--online, #22c55e); }',
    '  .toast.error   { border-left: 3px solid var(--offline, #ef4444); }',
    '  .toast.info    { border-left: 3px solid var(--accent, #6366f1); }',
    '  .toast-icon { font-size: 16px; }',
    '  .toast-close {',
    '    margin-left: auto;',
    '    background: transparent;',
    '    border: none;',
    '    color: var(--text-secondary, #a1a1aa);',
    '    cursor: pointer;',
    '    font-size: 16px;',
    '    padding: 0;',
    '    width: 18px;',
    '    height: 18px;',
    '  }',
    '</style>',
    '<div class="toast" part="root">',
    '  <span class="toast-icon" part="icon"></span>',
    '  <span class="toast-msg"><slot></slot></span>',
    '  <button class="toast-close" part="close" type="button" aria-label="close">×</button>',
    '</div>',
  ].join('\n');

  var ICONS = { success: '✓', error: '✕', info: 'ⓘ' };

  function withViewTransition(callback) {
    if (typeof document.startViewTransition === 'function') {
      try {
        document.startViewTransition(callback);
        return;
      } catch (e) { /* fall through */ }
    }
    callback();
  }

  class DormToast extends HTMLElement {
    static get observedAttributes() {
      return ['kind', 'duration'];
    }

    constructor() {
      super();
      this.attachShadow({ mode: 'open' });
      this.shadowRoot.appendChild(tpl.content.cloneNode(true));
      this._root = this.shadowRoot.querySelector('.toast');
      this._icon = this.shadowRoot.querySelector('.toast-icon');
      this._close = this.shadowRoot.querySelector('.toast-close');
      this._close.addEventListener('click', () => this.dismiss());
    }

    connectedCallback() {
      this._applyKind();
      var d = parseInt(this.getAttribute('duration') || '3000', 10);
      if (!isNaN(d) && d > 0) {
        this._timer = setTimeout(() => this.dismiss(), d);
      }
    }

    disconnectedCallback() {
      if (this._timer) clearTimeout(this._timer);
    }

    attributeChangedCallback(name) {
      if (name === 'kind') this._applyKind();
    }

    _applyKind() {
      if (!this._root) return;
      var k = this.getAttribute('kind') || 'info';
      this._root.classList.remove('success', 'error', 'info');
      this._root.classList.add(k);
      this._icon.textContent = ICONS[k] || ICONS.info;
    }

    dismiss() {
      var self = this;
      this._root.classList.add('leaving');
      this.dispatchEvent(new CustomEvent('dorm-toast-leave', { bubbles: true }));
      withViewTransition(function () {
        // The transition callback is sync; defer the actual node removal
        // to after the View Transition has captured both states.
        setTimeout(function () {
          if (self.parentNode) self.parentNode.removeChild(self);
        }, 200);
      });
    }
  }

  customElements.define('dorm-toast', DormToast);
})();