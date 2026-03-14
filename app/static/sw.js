self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : { title: "Alert", body: "Notification" };
  event.waitUntil(self.registration.showNotification(data.title, { body: data.body }));
});
