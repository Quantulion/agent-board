// Минимальный DOM для проверки помощников из dom.js в Node.
class FakeNode {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(text) {
    this.childNodes.forEach(n => { n.parentNode = null; });
    this.childNodes = text === '' ? [] : [new FakeText(text)];
    this.childNodes.forEach(n => { n.parentNode = this; });
  }
  _adopt(node) {
    if (node.nodeType === 11) return [...node.childNodes].flatMap(n => this._adopt(n));
    if (node.parentNode) node.parentNode._detach(node);
    node.parentNode = this;
    return [node];
  }
  _detach(node) { this.childNodes.splice(this.childNodes.indexOf(node), 1); node.parentNode = null; }
  append(...nodes) {
    for (const n of nodes) {
      const node = typeof n === 'string' ? new FakeText(n) : n;
      this.childNodes.push(...this._adopt(node));
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) {
    for (const c of [...this.childNodes]) this._detach(c);
    this.append(...nodes);
  }
  insertBefore(node, ref) {
    if (ref === null) return this.appendChild(node);
    const adopted = this._adopt(node);
    this.childNodes.splice(this.childNodes.indexOf(ref), 0, ...adopted);
    return node;
  }
  remove() { if (this.parentNode) this.parentNode._detach(this); }
  querySelectorAll(tag) {
    const out = [];
    const walk = n => n.childNodes.forEach(c => { if (c.nodeType === 1 && (tag === '*' || c.tagName === tag.toUpperCase())) out.push(c); walk(c); });
    walk(this);
    return out;
  }
}

class FakeText extends FakeNode {
  constructor(data) { super(); this.nodeType = 3; this.data = String(data); }
  get textContent() { return this.data; }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.listeners = {};
    const props = {};
    this.style = { setProperty: (k, v) => { props[k] = v; }, props };
  }
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.has(k) ? this.attributes.get(k) : null; }
  hasAttribute(k) { return this.attributes.has(k); }
  get hidden() { return this.attributes.has('hidden'); }
  set hidden(on) { if (on) this.attributes.set('hidden', ''); else this.attributes.delete('hidden'); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
}

class FakeFragment extends FakeNode {
  constructor() { super(); this.nodeType = 11; }
}

export function installFakeDocument() {
  globalThis.document = {
    // разметку шаблона (иконки) не разбираем: содержимое шаблона пустое
    createElement: tag => Object.assign(new FakeElement(tag), tag === 'template' ? { content: new FakeFragment() } : {}),
    createTextNode: text => new FakeText(text),
    createDocumentFragment: () => new FakeFragment(),
  };
  return globalThis.document;
}
