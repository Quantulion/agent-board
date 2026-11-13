// Вкладка браузера: заголовок экрана с числом пунктов «Ждут вас» и иконка с точкой, когда что-то ждёт.

let base = 'agent-board';
let count = 0;

export function tabTitle(text, n) {
  return n > 0 ? '(' + n + ') ' + text : text;
}

export function tabIcon(n) {
  return n > 0 ? '/static/favicon-attn.svg' : '/static/favicon.svg';
}

function apply() {
  document.title = tabTitle(base, count);
  const link = document.querySelector('link[rel="icon"]');
  const href = tabIcon(count);
  if (link && link.getAttribute('href') !== href) link.setAttribute('href', href);
}

export function setTabTitle(text) {
  base = text;
  apply();
}

export function setTabAttention(n) {
  count = n;
  apply();
}
