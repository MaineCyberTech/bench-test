"use strict";
/* bench-test control plane — service worker (vanilla, no build step, no deps).
 *
 * Goal: an installable PWA that still opens to the static shell when the
 * network is gone, while every control-plane call stays live.
 *
 *   * the static shell is precached and served cache-first;
 *   * /api/** (including the SSE event stream) is network-only and never
 *     cached — a stale run list or a replayed event stream would be worse
 *     than an honest failure;
 *   * a navigation request falls back to the cached index.html when offline.
 *
 * Registered from app.js only when the browser supports service workers and
 * the page is not file://, so an unsupported/blocked worker changes nothing.
 */
const CACHE = "bench-test-shell-v1";
// shell files, relative so the worker keeps working when the control plane is
// mounted under a sub-path (the worker's own scope decides the base)
const SHELL = ["index.html", "app.js", "style.css", "icon.svg", "manifest.webmanifest"];
const PRECACHE = ["./"].concat(SHELL);

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;              // let POST/DELETE go straight to the network
  let url;
  try { url = new URL(req.url); } catch (_) { return; }
  if (url.origin !== self.location.origin) return;   // cross-origin: leave it alone
  // API + SSE are network-only: never precached, never read from cache, never
  // written to it. Returning here lets the browser fetch them normally.
  if (url.pathname.startsWith("/api/")) return;
  // a page navigation goes to the network, falling back to the cached shell offline
  if (req.mode === "navigate") {
    e.respondWith(fetch(req).catch(() => caches.match("index.html")));
    return;
  }
  // the static shell is cache-first
  if (SHELL.some((p) => url.pathname.endsWith("/" + p))) {
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
});
