"use strict";
(() => {
  const installButton = document.getElementById("install-app");
  const dialog = document.getElementById("install-dialog");
  const installStatus = document.getElementById("pwa-status");
  const connection = document.getElementById("connection-status");
  const form = document.getElementById("planner-form");
  const submit = document.getElementById("submit-button");
  const standalone = window.matchMedia("(display-mode: standalone)");
  let installPrompt = null;
  let waitingForRoute = false;

  function updateInstalledState() {
    installButton.hidden = standalone.matches || navigator.standalone === true;
  }
  updateInstalledState();
  standalone.addEventListener("change", updateInstalledState);
  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    installButton.hidden = true;
    dialog.close();
  });
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    installPrompt = event;
    installButton.hidden = false;
  });
  installButton.addEventListener("click", async () => {
    if (!installPrompt) {
      dialog.showModal();
      return;
    }
    const prompt = installPrompt;
    installPrompt = null;
    try {
      await prompt.prompt();
      await prompt.userChoice;
    } catch {
      dialog.showModal();
    }
  });
  document.getElementById("close-install").addEventListener("click", () => dialog.close());

  function updateConnection() {
    connection.hidden = navigator.onLine;
    if (!navigator.onLine) {
      connection.textContent = "Нет сети. Интерфейс доступен, но для подбора нового маршрута нужно подключение.";
    }
  }
  updateConnection();
  window.addEventListener("offline", updateConnection);
  window.addEventListener("online", updateConnection);
  form.addEventListener("submit", event => {
    if (!navigator.onLine) {
      event.preventDefault();
      event.stopImmediatePropagation();
      updateConnection();
      document.getElementById("status").textContent = "Подключитесь к интернету, чтобы подобрать прогулку. Запрос не отправлен.";
    } else {
      waitingForRoute = true;
    }
  }, true);
  form.addEventListener("input", () => { waitingForRoute = false; });
  form.addEventListener("change", () => { waitingForRoute = false; });
  for (const button of document.querySelectorAll("[data-scenario]")) {
    button.addEventListener("click", () => { waitingForRoute = false; });
  }
  new MutationObserver(() => {
    if (waitingForRoute && !submit.disabled) {
      waitingForRoute = false;
      if (window.matchMedia("(max-width: 760px)").matches) {
        document.getElementById("route-results").scrollIntoView({
          block: "start",
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"
        });
      }
    }
  }).observe(submit, {attributes: true, attributeFilter: ["disabled"]});

  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("/sw.js", {scope: "/", updateViaCache: "none"})
      .then(() => navigator.serviceWorker.ready)
      .then(() => {
        installStatus.textContent = "Интерфейс сохранён для открытия без сети. Для подбора маршрута нужно подключение.";
      })
      .catch(() => {
        installStatus.textContent = navigator.serviceWorker.controller
          ? "Открыт сохранённый интерфейс. Для подбора маршрута нужно подключение."
          : "Не удалось сохранить интерфейс для работы без сети. Приложением можно пользоваться в браузере.";
      });
  } else if (!window.isSecureContext) {
    installStatus.textContent = "Для установки откройте приложение по защищённому HTTPS-адресу.";
  } else {
    installStatus.textContent = "Этот браузер не поддерживает сохранение интерфейса без сети.";
  }
})();
