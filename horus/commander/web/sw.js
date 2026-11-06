"use strict";
// HORUS control app service worker. Keeps only the app shell, network first: the page always
// loads fresh from the laptop when it answers; the cached copy is only for starting offline.
// Anything else (/api/*, /events, /task, /emergency, /reset, every non-GET) is never touched.

const CACHE_PREFIX = "horus-shell-";
const CACHE = `${CACHE_PREFIX}v1`;
const SHELL = [
  "/",
  "/app.js",
  "/app.css",
  "/manifest.webmanifest",
  "/icon.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
];
const SHELL_PATHS = new Set(SHELL);
const NETWORK_WAIT_MS = 5000; // then answer from the cache if it has a copy

self.addEventListener("install", (event) => {
  // One missing file must not stop the worker from installing.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.allSettled(SHELL.map((path) => cache.add(new Request(path, { cache: "no-store" })))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE).map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !SHELL_PATHS.has(url.pathname)) return;
  // The page is stored under its path alone: /?view=phone and / are the same shell.
  event.respondWith(networkFirst(request, url.pathname));
});

async function networkFirst(request, path) {
  const cache = await caches.open(CACHE);
  const fromNetwork = fetch(request).then((response) => {
    if (response.ok) cache.put(path, response.clone()).catch(() => {});
    return response;
  });
  fromNetwork.catch(() => {}); // a late failure after the cached copy was used is expected
  const cached = await cache.match(path);
  if (!cached) return fromNetwork; // nothing to fall back to: wait for the network
  const timeout = new Promise((resolve) => setTimeout(() => resolve(null), NETWORK_WAIT_MS));
  try {
    return (await Promise.race([fromNetwork, timeout])) || cached;
  } catch {
    return cached; // offline or the laptop is unreachable
  }
}
