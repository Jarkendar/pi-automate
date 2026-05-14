/**
 * Courses Hub Sync Adapter - pi-automate module.
 *
 * Drop-in replacement for localStorage usage in course HTML files.
 * Intercepts read/write and syncs with the Courses Hub API
 * (PUT /api/v1/courses/{course_id}).
 *
 * Offline-first: reads come from localStorage, writes go there first and are
 * then debounced to the API. On page load the adapter pulls server state.
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

  // Strategy A: single JSON blob in one localStorage key
  function singleKeyExtract(rawObj) {
    const obj = rawObj || {};
    const statuses = obj.statuses || obj.status || {};
    const notes = obj.notes || {};
    const meta = {};
    for (const k of Object.keys(obj)) {
      if (k !== "statuses" && k !== "status" && k !== "notes") meta[k] = obj[k];
    }
    const lessons = {};
    for (const id of new Set([...Object.keys(statuses), ...Object.keys(notes)])) {
      lessons[id] = {
        status: normStatus(statuses[id]),
        notes: notes[id] || "",
      };
    }
    return { lessons, meta };
  }

  function singleKeyApply(rawObj, lessons, meta) {
    const out = { ...(rawObj || {}) };
    const statusKey = "statuses" in out ? "statuses" : ("status" in out ? "status" : "statuses");
    out[statusKey] = {};
    out.notes = {};
    for (const [id, lp] of Object.entries(lessons)) {
      out[statusKey][id] = lp.status;
      if (lp.notes) out.notes[id] = lp.notes;
    }
    for (const [k, v] of Object.entries(meta || {})) out[k] = v;
    return out;
  }

  // Strategy B: multiple keys, one per lesson
  function multiKeyExtract(prefix, lessonIds) {
    const lessons = {};
    for (const id of lessonIds) {
      const s = localStorage.getItem(prefix + "status_" + id);
      const n = localStorage.getItem(prefix + "notes_" + id);
      if (s || n) {
        lessons[id] = { status: normStatus(s), notes: n || "" };
      }
    }
    return { lessons, meta: {} };
  }

  function multiKeyApply(prefix, lessons) {
    for (const [id, lp] of Object.entries(lessons)) {
      localStorage.setItem(prefix + "status_" + id, lp.status);
      if (lp.notes) {
        localStorage.setItem(prefix + "notes_" + id, lp.notes);
      } else {
        localStorage.removeItem(prefix + "notes_" + id);
      }
    }
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
        throw new Error(`API ${method} ${path} -> HTTP ${res.status}`);
      }
      return res.status === 404 ? null : await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  function attach(config) {
    const {
      courseId,
      title = "",
      singleKey = null,
      multiKey = null,
      totalLessons = 0,
      onSync = null,
    } = config;

    if (!courseId) throw new Error("CoursesSync.attach: courseId is required");
    if (!singleKey && !multiKey) throw new Error("CoursesSync.attach: singleKey or multiKey required");

    let pending = null;
    let inFlight = false;

    function readLocal() {
      if (singleKey) {
        const raw = localStorage.getItem(singleKey);
        try {
          return singleKeyExtract(raw ? JSON.parse(raw) : {});
        } catch {
          return { lessons: {}, meta: {} };
        }
      }
      return multiKeyExtract(multiKey.prefix, multiKey.lessonIds);
    }

    function writeLocal(lessons, meta) {
      if (singleKey) {
        const raw = localStorage.getItem(singleKey);
        let prev = {};
        try { prev = raw ? JSON.parse(raw) : {}; } catch { prev = {}; }
        const merged = singleKeyApply(prev, lessons, meta);
        localStorage.setItem(singleKey, JSON.stringify(merged));
      } else {
        multiKeyApply(multiKey.prefix, lessons);
      }
    }

    function setStatusUI(s) {
      try { onSync && onSync(s); } catch { /* ignore */ }
      window.dispatchEvent(new CustomEvent("courses-sync", { detail: { courseId, ...s } }));
    }

    async function push() {
      if (inFlight) return;
      inFlight = true;
      setStatusUI({ state: "syncing" });
      try {
        const { lessons, meta } = readLocal();
        const payload = {
          course_id: courseId,
          title,
          total_lessons: totalLessons,
          lessons,
          meta,
        };
        await apiRequest("PUT", "/courses/" + encodeURIComponent(courseId), payload);
        localStorage.removeItem(LS_DIRTY_FLAG + courseId);
        setStatusUI({ state: "synced", at: new Date().toISOString() });
      } catch (err) {
        console.warn("[CoursesSync] push failed, keeping dirty flag:", err);
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

    async function pull() {
      try {
        const remote = await apiRequest("GET", "/courses/" + encodeURIComponent(courseId));
        if (!remote || !remote.lessons) {
          if (Object.keys(readLocal().lessons).length > 0) await push();
          return;
        }
        const lessons = {};
        for (const [id, lp] of Object.entries(remote.lessons)) {
          lessons[id] = { status: normStatus(lp.status), notes: lp.notes || "" };
        }
        writeLocal(lessons, remote.meta || {});
        localStorage.setItem(LS_LAST_PULL + courseId, new Date().toISOString());
        setStatusUI({ state: "pulled", at: new Date().toISOString() });
      } catch (err) {
        console.warn("[CoursesSync] pull failed, working offline:", err);
        setStatusUI({ state: "offline", error: String(err) });
      }
    }

    const origSetItem = Storage.prototype.setItem;
    const origRemoveItem = Storage.prototype.removeItem;
    const keysOfInterest = singleKey
      ? (k) => k === singleKey
      : (k) => k.startsWith(multiKey.prefix);

    Storage.prototype.setItem = function (k, v) {
      origSetItem.call(this, k, v);
      if (this === localStorage && keysOfInterest(k)) schedulePush();
    };
    Storage.prototype.removeItem = function (k) {
      origRemoveItem.call(this, k);
      if (this === localStorage && keysOfInterest(k)) schedulePush();
    };

    pull();

    return {
      pushNow: push,
      pullNow: pull,
      readLocal,
    };
  }

  global.CoursesSync = { attach, normStatus };
})(window);
