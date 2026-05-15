/**
 * Courses Hub Sync Adapter - pi-automate module.
 *
 * Single, uniform adapter. Each patched course provides callbacks that
 * encapsulate its own localStorage format:
 *
 *   readLessons()  -> { [lessonId]: { status, notes } }
 *   writeLessons({ [lessonId]: { status, notes } })  // restores into LS
 *   lessonCount()  -> integer (authoritative total from the course's data)
 *
 * The adapter knows nothing about single-key/multi-key/hybrid storage shapes.
 * All storage details live in the patcher-injected callbacks.
 *
 * Sync strategy:
 *   - On page load: pull server state, merge via writeLessons() (server wins).
 *   - On every localStorage mutation: re-read via readLessons(), compute a
 *     stable hash; if it differs from the last pushed state, schedule a
 *     debounced PUT.
 *   - On tab hide: best-effort flush if dirty.
 */
(function (global) {
  "use strict";

  const API_BASE = "/api/v1";
  const DEBOUNCE_MS = 800;
  const SYNC_TIMEOUT_MS = 5000;
  const LS_DIRTY_FLAG = "_coursesSync_dirty_";
  const LS_LAST_PULL = "_coursesSync_lastPull_";

  const STATUS_IN = {
    "": "not_started",
    "not-started": "not_started",
    "not_started": "not_started",
    "in-progress": "in_progress",
    "in_progress": "in_progress",
    "started": "in_progress",
    "completed": "completed",
    "done": "completed",
    "finished": "completed",
  };
  function normStatus(s) {
    if (s == null) return "not_started";
    return STATUS_IN[String(s).trim().toLowerCase()] || "not_started";
  }

  async function apiRequest(method, path, body) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), SYNC_TIMEOUT_MS);
    try {
      const res = await fetch(API_BASE + path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined,
        credentials: "include",
        signal: ctrl.signal,
      });
      if (!res.ok && res.status !== 404) {
        throw new Error("API " + method + " " + path + " -> HTTP " + res.status);
      }
      return res.status === 404 ? null : await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function normalizeLessons(raw) {
    const out = {};
    for (const [id, lp] of Object.entries(raw || {})) {
      out[id] = {
        status: normStatus(lp && lp.status),
        notes: (lp && lp.notes) || "",
      };
    }
    return out;
  }

  // Stable hash of lessons map for change detection
  function hashLessons(lessons) {
    const keys = Object.keys(lessons).sort();
    return keys.map(k => k + ":" + lessons[k].status + ":" + lessons[k].notes.length).join("|");
  }

  // Install global LS hook exactly once. Each attach() registers a listener
  // that gets called after every setItem/removeItem.
  const listeners = [];
  let hookInstalled = false;
  function installLSHook() {
    if (hookInstalled) return;
    hookInstalled = true;
    const origSet = Storage.prototype.setItem;
    const origRemove = Storage.prototype.removeItem;
    Storage.prototype.setItem = function (k, v) {
      origSet.call(this, k, v);
      if (this === localStorage) {
        for (const fn of listeners) {
          try { fn(); } catch (e) { console.error("[CoursesSync] listener err:", e); }
        }
      }
    };
    Storage.prototype.removeItem = function (k) {
      origRemove.call(this, k);
      if (this === localStorage) {
        for (const fn of listeners) {
          try { fn(); } catch (e) { console.error("[CoursesSync] listener err:", e); }
        }
      }
    };
  }

  function attach(config) {
    const {
      courseId,
      title = "",
      readLessons,
      writeLessons,
      lessonCount,
      onSync = null,
    } = config;

    if (!courseId) throw new Error("CoursesSync.attach: courseId is required");
    if (typeof readLessons !== "function" || typeof writeLessons !== "function") {
      throw new Error("CoursesSync.attach: readLessons / writeLessons are required");
    }

    let pending = null;
    let inFlight = false;
    let lastPushedHash = null;
    let suppressHook = false; // true while we apply remote state to avoid push loop

    function setStatusUI(s) {
      try { onSync && onSync(s); } catch { /* ignore */ }
      window.dispatchEvent(new CustomEvent("courses-sync", { detail: { courseId, ...s } }));
    }

    async function push() {
      if (inFlight) return;
      inFlight = true;
      setStatusUI({ state: "syncing" });
      try {
        const lessons = normalizeLessons(readLessons());
        const total = typeof lessonCount === "function" ? lessonCount() : 0;
        const payload = {
          course_id: courseId,
          title,
          total_lessons: total,
          lessons,
          meta: {},
        };
        await apiRequest("PUT", "/courses/" + encodeURIComponent(courseId), payload);
        lastPushedHash = hashLessons(lessons);
        localStorage.removeItem(LS_DIRTY_FLAG + courseId);
        setStatusUI({ state: "synced", at: new Date().toISOString() });
      } catch (err) {
        console.warn("[CoursesSync] push failed:", err);
        localStorage.setItem(LS_DIRTY_FLAG + courseId, "1");
        setStatusUI({ state: "error", error: String(err) });
      } finally {
        inFlight = false;
      }
    }

    function schedulePush() {
      if (pending) clearTimeout(pending);
      pending = setTimeout(push, DEBOUNCE_MS);
    }

    function onStorageChange() {
      if (suppressHook) return;
      const lessons = normalizeLessons(readLessons());
      const h = hashLessons(lessons);
      if (h !== lastPushedHash) schedulePush();
    }

    async function pull() {
      try {
        const remote = await apiRequest("GET", "/courses/" + encodeURIComponent(courseId));
        if (!remote || !remote.lessons || Object.keys(remote.lessons).length === 0) {
          // Server has no data yet - seed from local if local has anything
          const localLessons = normalizeLessons(readLessons());
          if (Object.keys(localLessons).length > 0) {
            await push();
          } else {
            lastPushedHash = hashLessons({});
          }
          return;
        }
        const lessons = normalizeLessons(remote.lessons);
        suppressHook = true;
        try {
          writeLessons(lessons);
        } finally {
          suppressHook = false;
        }
        lastPushedHash = hashLessons(lessons);
        localStorage.setItem(LS_LAST_PULL + courseId, new Date().toISOString());
        setStatusUI({ state: "pulled", at: new Date().toISOString() });
      } catch (err) {
        console.warn("[CoursesSync] pull failed, working offline:", err);
        setStatusUI({ state: "offline", error: String(err) });
      }
    }

    installLSHook();
    listeners.push(onStorageChange);

    // Best-effort flush on tab hide
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden" &&
          localStorage.getItem(LS_DIRTY_FLAG + courseId)) {
        push();
      }
    });

    // Initial sync
    pull();

    return { pushNow: push, pullNow: pull };
  }

  global.CoursesSync = { attach, normStatus };
})(window);
