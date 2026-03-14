(async () => {
  const btn = document.getElementById("btn");
  const status = document.getElementById("status");

  btn.addEventListener("click", async () => {
    if (!("serviceWorker" in navigator)) {
      if (status) status.textContent = "Service workers not supported in this browser.";
      return;
    }
    try {
      btn.disabled = true;
      if (status) status.textContent = "Registering service worker…";
      const reg = await navigator.serviceWorker.register("/static/sw.js");

      if (status) status.textContent = "Fetching VAPID key…";
      const vapidKey = await fetch("/push/vapid_public_key").then(r => r.text());

      if (status) status.textContent = "Subscribing to push…";
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidKey.trim()),
      });

      if (status) status.textContent = "Registering with server…";
      const resp = await fetch("/push/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subscription: sub.toJSON() }),
      });

      if (resp.ok) {
        if (status) status.textContent = "Subscribed! You will receive escalation alerts.";
        btn.textContent = "Subscribed";
      } else {
        if (status) status.textContent = "Server registration failed: " + resp.status;
        btn.disabled = false;
      }
    } catch (err) {
      console.error(err);
      if (status) status.textContent = "Error: " + err.message;
      btn.disabled = false;
    }
  });

  function urlBase64ToUint8Array(base64String) {
    const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }
})();
