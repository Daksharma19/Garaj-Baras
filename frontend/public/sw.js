// Garaj Baras — service worker for rain alert push notifications.

self.addEventListener('push', (event) => {
  let data = { title: 'Garaj Baras', body: 'Rain update' }
  try {
    data = event.data.json()
  } catch (e) { /* keep defaults */ }
  event.waitUntil(
    self.registration.showNotification(data.title || 'Garaj Baras', {
      body: data.body || '',
      icon: '/favicon.svg',
      badge: '/favicon.svg',
      tag: 'garaj-baras-rain',   // replace older rain alerts instead of stacking
    })
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ('focus' in c) return c.focus()
      }
      return clients.openWindow('/')
    })
  )
})
