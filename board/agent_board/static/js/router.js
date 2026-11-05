// Маршруты по hash: #/ — проекты, #/p/<slug> — доска, #/p/<slug>/decomposition — разбивка;
// ?task=<KEY> — открытая карточка задачи поверх вида; ?focus=<KEY> — задача, к которой прокрутить разбивку.

function splitQuery(hash) {
  const raw = String(hash || '').replace(/^#/, '');
  const at = raw.indexOf('?');
  return at < 0 ? [raw, ''] : [raw.slice(0, at), raw.slice(at + 1)];
}

export function parseRoute(hash) {
  const [rawPath, query] = splitQuery(hash);
  const path = rawPath.replace(/\/+$/, '');
  if (path === '') return { name: 'projects' };
  const parts = path.split('/');
  if (parts[0] !== '' || parts[1] !== 'p' || !parts[2] || parts.length > 4) return { name: 'not_found' };
  let slug;
  let task = null;
  try {
    slug = decodeURIComponent(parts[2]);
    task = new URLSearchParams(query).get('task');
  } catch (e) {
    return { name: 'not_found' };
  }
  const extra = task ? { task } : {};
  if (parts.length === 3) return { name: 'board', slug, ...extra };
  if (parts[3] === 'decomposition') {
    const focus = new URLSearchParams(query).get('focus');
    return { name: 'decomposition', slug, ...extra, ...(focus ? { focus } : {}) };
  }
  return { name: 'not_found' };
}

// Страница разбивки; key — задача, к которой прокрутить и раскрыть её.
export function decompositionHref(slug, key) {
  return boardHref(slug) + '/decomposition' + (key ? '?focus=' + encodeURIComponent(key) : '');
}

export function boardHref(slug) {
  return '#/p/' + encodeURIComponent(slug);
}

// Тот же адрес с открытой карточкой key (null — без карточки).
export function withTask(hash, key) {
  const [path] = splitQuery(hash);
  return '#' + path + (key ? '?task=' + encodeURIComponent(key) : '');
}
