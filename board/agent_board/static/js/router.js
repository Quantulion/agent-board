// Маршруты по hash: #/ — проекты, #/p/<slug> — доска, #/p/<slug>/decomposition — разбивка.

export function parseRoute(hash) {
  const path = String(hash || '').replace(/^#/, '').replace(/\/+$/, '');
  if (path === '') return { name: 'projects' };
  const parts = path.split('/');
  if (parts[0] !== '' || parts[1] !== 'p' || !parts[2] || parts.length > 4) return { name: 'not_found' };
  let slug;
  try {
    slug = decodeURIComponent(parts[2]);
  } catch (e) {
    return { name: 'not_found' };
  }
  if (parts.length === 3) return { name: 'board', slug };
  if (parts[3] === 'decomposition') return { name: 'decomposition', slug };
  return { name: 'not_found' };
}

export function boardHref(slug) {
  return '#/p/' + encodeURIComponent(slug);
}
