// Сохранённая тема применяется до загрузки стилей, чтобы страница не мигала другой темой.
(function () {
  try {
    var t = window.localStorage.getItem('ab-theme');
    if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  } catch (e) {
    // хранилище недоступно — остаётся системная тема
  }
})();
