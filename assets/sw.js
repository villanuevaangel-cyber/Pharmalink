self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: "PharmaLink", body: event.data ? event.data.text() : "" };
  }
  var title = data.title || "PharmaLink";
  var options = {
    body: data.body || "",
    tag: data.tag || "pharmalink",
    renotify: true,
    data: {
      url: data.url || "/",
      target: data.target || "",
      filter: data.filter || "",
      orderId: data.orderId || 0
    }
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var data = event.notification.data || {};
  var target = data.url || "/";
  var path = target.split("?")[0].split("#")[0];
  var message = {
    type: "pharmalink-push",
    target: data.target || "",
    filter: data.filter || "",
    orderId: data.orderId || 0
  };
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clients) {
      var match = null;
      for (var i = 0; i < clients.length; i++) {
        if (clients[i].url.indexOf(self.location.origin + path) === 0) {
          match = clients[i];
          break;
        }
      }
      if (match) {
        match.postMessage(message);
        return match.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })
  );
});
