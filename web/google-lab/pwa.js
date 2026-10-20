"use strict";
(() => {
  const button = document.getElementById("install-app");
  const status = document.getElementById("pwa-status");
  const standalone = window.matchMedia("(display-mode: standalone)");
  let pending = null;
  let installInteraction = false;
  const installed = () => standalone.matches || navigator.standalone === true;
  const sync = () => { button.hidden = installed() || !pending; };
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    pending = event;
    sync();
  });
  window.addEventListener("appinstalled", () => {
    pending = null;
    installInteraction = true;
    sync();
    status.textContent = "Приложение установлено. Для подбора прогулки нужно подключение.";
  });
  if (standalone.addEventListener) standalone.addEventListener("change", sync);
  button.addEventListener("click", async () => {
    const prompt = pending;
    if (!prompt) return;
    pending = null;
    installInteraction = true;
    sync();
    try { await prompt.prompt(); await prompt.userChoice; }
    catch (_) { status.textContent = "Откройте меню браузера для установки. Можно продолжить пользоваться сайтом."; }
  });
  sync();
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", {scope: "/", updateViaCache: "none"})
      .then(() => navigator.serviceWorker.ready)
      .then(() => { if (!installInteraction) status.textContent = "Оболочка доступна без сети. Для поиска, ответа и карты нужно подключение."; })
      .catch(() => { if (!installInteraction) status.textContent = "Оболочку не удалось сохранить. Можно продолжить пользоваться сайтом."; });
  } else {
    status.textContent = "Установка зависит от браузера и HTTPS. Для поиска, ответа и карты нужно подключение.";
  }
})();
