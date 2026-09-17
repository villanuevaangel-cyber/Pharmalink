self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("message", function (event) {
  var data = event.data || {};
  if (data.type === "PHARMALINK_ALERT") {
    event.waitUntil(
      self.registration.showNotification(data.title || "PharmaLink Alert", {
        body: data.body || "",
        icon: "/assets/logo.png",
        tag: (data.body || "alert").slice(0, 60),
      })
    );
  }
});

self.addEventListener("push", function (event) {
  var payload = { title: "PharmaLink Alert", body: "You have a new pharmacy alert." };
  try {
    if (event.data) payload = Object.assign(payload, event.data.json());
  } catch (e) {
    if (event.data) payload.body = event.data.text();
  }
  event.waitUntil(
    self.registration.showNotification(payload.title || "PharmaLink Alert", {
      body: payload.body || "",
      icon: "/assets/logo.png",
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil(self.clients.openWindow("/"));
});
