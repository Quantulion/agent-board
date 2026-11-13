// Клиент HTTP API доски. Авторизация — кука, выставленная сервером при входе по ссылке с токеном.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function pageLanguage() {
  return (globalThis.document && globalThis.document.documentElement.lang) || 'en';
}

export function api(slug, fetchFn = (...args) => globalThis.fetch(...args)) {
  const base = slug ? '/api/p/' + encodeURIComponent(slug) : '/api';

  async function request(method, path, body, signal) {
    // ошибки доски приходят на языке страницы
    const init = { method, credentials: 'same-origin', headers: { 'X-Board-Lang': pageLanguage() } };
    if (signal) init.signal = signal;
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let resp;
    try {
      resp = await fetchFn(base + path, init);
    } catch (e) {
      throw new ApiError(0, e && e.message ? e.message : String(e));
    }
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || 'HTTP ' + resp.status);
    return data;
  }

  return {
    get: (path, opts = {}) => request('GET', path, undefined, opts.signal),
    post: (path, body) => request('POST', path, body || {}),
    put: (path, body) => request('PUT', path, body || {}),
  };
}
