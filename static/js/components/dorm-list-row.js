/**
 * <dorm-list-row> — R66 (Web Components refactor)
 *
 * Wraps a single list-row (used in finance section: violations and
 * payments).  Reads data-* attributes and renders the inner DOM with
 * a tag pill on the right (warning | danger | default).
 *
 * Attributes:
 *   data-title        — primary text
 *   data-sub          — secondary line
 *   data-tag          — tag text shown in the pill
 *   data-tag-tone     — "warning" | "danger" | ""
 *   data-value        — monetary / amount text on the right
 *   data-value-empty  — "true" forces is-empty state
 *   data-vt-name      — view-transition-name for animations
 */
(function () {
  var tpl = document.createElement('template');
  tpl.innerHTML = [
    '<div class="list-row">',
    '  <div class="list-left">',
    '    <span class="list-title"></span>',
    '    <span class="list-sub"></span>',
    '  </div>',
    '  <div class="list-right">',
    '    <span class="tag"></span>',
    '    <span class="list-value"></span>',
    '  </div>',
    '</div>',
  ].join('\n');

  class DormListRow extends HTMLElement {
    static get observedAttributes() {
      return [
        'data-title',
        'data-sub',
        'data-tag',
        'data-tag-tone',
        'data-value',
        'data-value-empty',
        'data-vt-name',
      ];
    }

    constructor() {
      super();
      this._row = tpl.content.firstElementChild.cloneNode(true);
      this._title = this._row.querySelector('.list-title');
      this._sub = this._row.querySelector('.list-sub');
      this._tag = this._row.querySelector('.tag');
      this._value = this._row.querySelector('.list-value');
      this.appendChild(this._row);
    }

    connectedCallback() { this._sync(); }
    attributeChangedCallback() { this._sync(); }

    _sync() {
      if (!this._title) return;
      this._title.textContent = this.getAttribute('data-title') || '';
      this._sub.textContent = this.getAttribute('data-sub') || '—';

      var tag = this.getAttribute('data-tag') || '';
      var tone = this.getAttribute('data-tag-tone') || '';
      this._tag.textContent = tag;
      this._tag.classList.remove('tag-warning', 'tag-red');
      if (tone === 'warning') this._tag.classList.add('tag-warning');
      else if (tone === 'danger') this._tag.classList.add('tag-red');

      var val = this.getAttribute('data-value');
      var empty = this.getAttribute('data-value-empty') === 'true';
      if (val === null || val === '' || val == null || empty) {
        this._value.textContent = empty ? '—' : (val || '');
        this._value.classList.add('is-empty');
      } else {
        this._value.textContent = val;
        this._value.classList.remove('is-empty');
      }

      var vt = this.getAttribute('data-vt-name');
      if (vt) this.style.viewTransitionName = vt;
    }
  }

  customElements.define('dorm-list-row', DormListRow);
})();