#!/usr/bin/env python3
"""
patch-courses.py - Courses Hub patcher (pi-automate module).

For each HTML file in --src, the patcher:
  1. Detects course id (slug) and title.
  2. Detects the course's localStorage storage shape.
  3. Injects a per-course adapter bootstrap that wires three callbacks
     (readLessons / writeLessons / lessonCount) into CoursesSync.attach().
  4. Writes a patched copy to --dst.
  5. Generates a manifest.json the dashboard consumes.

Storage shapes supported (all yield the same uniform API on top):
  A) Single JSON blob keyed by `const COURSE_KEY = "..."`:
       { status: {id: "completed"}, notes: {id: "..."}, ... }
       (also accepts "statuses" instead of "status")
  B) Single JSON blob with hybrid keys (e.g. android-expert):
       { "status_l01": "completed", "status_l07": "...", "statuses": {}, "notes": {} }
  C) Multi-key with prefix (e.g. biznes-tech-communication):
       LS["${LS_PREFIX}status_${id}"] = "completed"
       LS["${LS_PREFIX}notes_${id}"]  = "..."

Lesson count is derived at *runtime* from the course's own data structure
(COURSE.modules or COURSE_DATA) so it's always authoritative.

Idempotent: files already containing the marker are not re-patched.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "<!-- COURSES_HUB_SYNC_INJECTED -->"

# ---------------------------------------------------------------------------
# Sync status indicator (visible in bottom-right of every patched course)
# ---------------------------------------------------------------------------
INDICATOR_HTML = """
<div id="__courses_sync_indicator" style="
  position: fixed; bottom: 12px; right: 12px; z-index: 9999;
  font: 12px ui-sans-serif, system-ui, sans-serif;
  background: rgba(0,0,0,0.75); color: #fff;
  padding: 6px 10px; border-radius: 999px;
  opacity: 0; transition: opacity 0.3s ease;
  pointer-events: none;
">syncing...</div>
<script>
(function(){
  var el = document.getElementById('__courses_sync_indicator');
  var labels = {
    syncing: ['sync...',  'rgba(0,0,0,0.75)'],
    synced:  ['synced',   'rgba(29,158,117,0.9)'],
    pulled:  ['updated',  'rgba(29,158,117,0.9)'],
    error:   ['sync err', 'rgba(192,57,43,0.9)'],
    offline: ['offline',  'rgba(120,120,120,0.85)']
  };
  var hideT = null;
  window.addEventListener('courses-sync', function(ev) {
    var p = labels[ev.detail.state] || ['', ''];
    if (!p[0]) return;
    el.textContent = p[0];
    el.style.background = p[1];
    el.style.opacity = '1';
    if (hideT) clearTimeout(hideT);
    if (ev.detail.state === 'synced' || ev.detail.state === 'pulled') {
      hideT = setTimeout(function(){ el.style.opacity = '0'; }, 1500);
    }
  });
})();
</script>
"""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@dataclass
class DetectedCourse:
    src_path: Path
    course_id: str
    title: str
    strategy: str | None = None    # 'single' | 'multi' | None
    single_key: str | None = None
    multi_prefix: str | None = None
    # JS expression that, evaluated at runtime in the course's scope, yields
    # an array of lesson id strings. Adapter uses this to know what to read.
    lesson_ids_js: str | None = None
    extra: dict = field(default_factory=dict)


def slug_from_filename(path: Path) -> str:
    name = path.stem
    name = re.sub(r"^course[-_]", "", name)
    return name.replace("_", "-").lower()


def humanize_slug(slug: str) -> str:
    parts = re.split(r"[-_]+", slug)
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


def extract_course_title(html: str) -> str | None:
    """
    Find the COURSE object's top-level `title`, refusing matches that actually
    belong to module 1 (i.e. `title:` appearing after a `modules:` or
    `lessons:` token inside the COURSE block).
    """
    m = re.search(r"const\s+COURSE\s*=\s*\{", html)
    if not m:
        return None
    window = html[m.end(): m.end() + 400]
    mods = re.search(r"\b(modules|lessons)\s*:\s*\[", window)
    t = re.search(r"\btitle\s*:\s*['\"]([^'\"]+)['\"]", window)
    if not t:
        return None
    if mods and mods.start() < t.start():
        return None
    return t.group(1)


def detect_modules_var(html: str) -> str | None:
    """
    Return the JS expression that references the course's modules array.
    Most courses use `COURSE.modules`; some use a standalone `COURSE_DATA`.
    """
    if re.search(r"\bCOURSE\.modules\b", html) or re.search(r"const\s+COURSE\s*=\s*\{[^}]*modules\s*:", html, re.DOTALL):
        return "COURSE.modules"
    if re.search(r"\bCOURSE_DATA\b", html):
        return "COURSE_DATA"
    return None


def detect_lesson_id_kind(html: str, modules_var: str) -> str:
    """
    Returns one of:
      'explicit'   - lessons have `id: "..."` fields (use l.id)
      'generated'  - lessons rely on synthetic ids like `m${mi}_l${li}`
    """
    # Look for a lessonId() helper used by the course
    if re.search(r"function\s+lessonId\s*\(", html):
        return "generated"
    # Look at any lesson literal that has id: present
    if re.search(modules_var.replace(".", r"\.") + r"\s*=\s*[\[\{]", html) or True:
        # Conservative: if there's any { id: '...' } occurrence, assume explicit
        if re.search(r"\{\s*id\s*:\s*['\"]", html):
            return "explicit"
    return "generated"


def build_lesson_ids_js(modules_var: str, id_kind: str) -> str:
    """JS expression returning array of all lesson ids at runtime.

    For 'generated' kind we mirror the convention used by courses that have a
    `lessonId(mIdx, lIdx)` helper: 0-indexed `m${mi}_l${li}`. This must match
    exactly what the course already wrote to localStorage.
    """
    if id_kind == "explicit":
        return (
            "(function(){var ids=[];"
            + modules_var + ".forEach(function(m,mi){m.lessons.forEach(function(l,li){"
            "ids.push(l.id || ('m'+mi+'_l'+li));"
            "});});return ids;})()"
        )
    return (
        "(function(){var ids=[];"
        + modules_var + ".forEach(function(m,mi){m.lessons.forEach(function(l,li){"
        "ids.push('m'+mi+'_l'+li);"
        "});});return ids;})()"
    )


def detect_course(path: Path, overrides: dict) -> DetectedCourse:
    html = path.read_text(encoding="utf-8", errors="replace")
    course_id = (
        _first_match(html, r"\bslug\s*:\s*['\"]([^'\"]+)['\"]")
        or slug_from_filename(path)
    )
    title = (
        (overrides.get(course_id) or {}).get("title")
        or extract_course_title(html)
        or humanize_slug(course_id)
    )
    info = DetectedCourse(src_path=path, course_id=course_id, title=title)

    if "localStorage" not in html:
        return info

    modules_var = detect_modules_var(html)
    if modules_var is None:
        info.extra["warning"] = "Cannot find modules array (COURSE.modules / COURSE_DATA)"
        return info

    id_kind = detect_lesson_id_kind(html, modules_var)
    info.lesson_ids_js = build_lesson_ids_js(modules_var, id_kind)

    # Multi-key strategy: presence of a `LS_PREFIX` constant
    m = re.search(r"const\s+LS_PREFIX\s*=\s*[`'\"]([^`'\"]+)[`'\"]", html)
    if m:
        prefix = m.group(1).replace("${COURSE.slug}", course_id)
        info.strategy = "multi"
        info.multi_prefix = prefix
        return info

    # Single-key strategy: presence of a known single-key constant
    single_key = (
        _first_match(html, r"const\s+COURSE_KEY\s*=\s*['\"]([^'\"]+)['\"]")
        or _first_match(html, r"const\s+STORAGE_KEY\s*=\s*['\"]([^'\"]+)['\"]")
        or _first_match(html, r"\bstorageKey\s*:\s*['\"]([^'\"]+)['\"]")
    )
    if single_key:
        info.strategy = "single"
        info.single_key = single_key
        return info

    info.extra["warning"] = "Uses localStorage but no recognized key pattern"
    return info


def _first_match(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Bootstrap code generation
# ---------------------------------------------------------------------------

def build_callbacks_single(course_id: str, single_key: str, ids_js: str) -> str:
    """
    Single-key strategy with hybrid support.
    Reads { status: {...}, statuses: {...}, notes: {...} } AND inline
    keys like "status_<id>" / "notes_<id>" living at the top level.
    Writes back into nested form using the same field name the course uses.
    """
    return f"""
    var SINGLE_KEY = "{single_key}";
    function readBlob() {{
      try {{ return JSON.parse(localStorage.getItem(SINGLE_KEY)) || {{}}; }}
      catch (e) {{ return {{}}; }}
    }}
    function writeBlob(b) {{
      localStorage.setItem(SINGLE_KEY, JSON.stringify(b));
    }}
    function statusFieldName(b) {{
      // Preserve whichever field the course already uses; default to 'status'.
      if (b && typeof b === 'object' && 'statuses' in b && !('status' in b)) return 'statuses';
      return 'status';
    }}
    function readLessons() {{
      var b = readBlob();
      var ids = {ids_js};
      var statusMap = (b && b.status) || (b && b.statuses) || {{}};
      var notesMap  = (b && b.notes) || {{}};
      var out = {{}};
      ids.forEach(function(id) {{
        var s = statusMap[id];
        if (s == null) s = b ? b["status_" + id] : null;
        var n = notesMap[id];
        if (n == null) n = b ? b["notes_" + id] : null;
        if (s != null || n != null) {{
          out[id] = {{ status: s || "not_started", notes: n || "" }};
        }}
      }});
      return out;
    }}
    function writeLessons(lessons) {{
      var b = readBlob();
      var field = statusFieldName(b);
      // Clear all inline status_X / notes_X to avoid stale data overriding nested form
      Object.keys(b).forEach(function(k) {{
        if (k.indexOf("status_") === 0 || k.indexOf("notes_") === 0) delete b[k];
      }});
      b[field] = {{}};
      b.notes  = {{}};
      Object.keys(lessons).forEach(function(id) {{
        b[field][id] = lessons[id].status;
        if (lessons[id].notes) b.notes[id] = lessons[id].notes;
      }});
      writeBlob(b);
    }}
    function lessonCount() {{ return {ids_js}.length; }}
"""


def build_callbacks_multi(course_id: str, prefix: str, ids_js: str) -> str:
    """
    Multi-key strategy. Reads/writes one LS entry per lesson per field.
    """
    return f"""
    var PREFIX = "{prefix}";
    function readLessons() {{
      var ids = {ids_js};
      var out = {{}};
      ids.forEach(function(id) {{
        var s = localStorage.getItem(PREFIX + "status_" + id);
        var n = localStorage.getItem(PREFIX + "notes_"  + id);
        if (s || n) out[id] = {{ status: s || "not_started", notes: n || "" }};
      }});
      return out;
    }}
    function writeLessons(lessons) {{
      Object.keys(lessons).forEach(function(id) {{
        localStorage.setItem(PREFIX + "status_" + id, lessons[id].status);
        if (lessons[id].notes) localStorage.setItem(PREFIX + "notes_" + id, lessons[id].notes);
        else localStorage.removeItem(PREFIX + "notes_" + id);
      }});
    }}
    function lessonCount() {{ return {ids_js}.length; }}
"""


def build_bootstrap(info: DetectedCourse) -> str:
    if info.strategy == "single":
        callbacks = build_callbacks_single(info.course_id, info.single_key, info.lesson_ids_js)
    elif info.strategy == "multi":
        callbacks = build_callbacks_multi(info.course_id, info.multi_prefix, info.lesson_ids_js)
    else:
        raise ValueError(f"Cannot build bootstrap for strategy {info.strategy!r}")

    return f"""
{MARKER}
<script src="/courses-sync.js"></script>
{INDICATOR_HTML}
<script>
// Wait until both the adapter and the course's data are available.
(function waitForReady() {{
  if (typeof CoursesSync === "undefined") return setTimeout(waitForReady, 50);
  // The course uses either COURSE.modules or COURSE_DATA. We check both.
  var modulesReady =
       (typeof COURSE !== "undefined" && COURSE && COURSE.modules)
    || (typeof COURSE_DATA !== "undefined" && COURSE_DATA);
  if (!modulesReady) return setTimeout(waitForReady, 50);
  try {{
    {callbacks.strip()}
    CoursesSync.attach({{
      courseId: "{info.course_id}",
      title: {json.dumps(info.title)},
      readLessons: readLessons,
      writeLessons: writeLessons,
      lessonCount: lessonCount
    }});
  }} catch (e) {{
    console.error("[CoursesSync] attach failed:", e);
  }}
}})();
</script>
"""


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------

def patch_html(html: str, info: DetectedCourse) -> str:
    if MARKER in html:
        return html
    bootstrap = build_bootstrap(info)
    if "</body>" in html:
        return html.replace("</body>", bootstrap + "\n</body>", 1)
    return html + bootstrap


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def write_manifest(courses: list[DetectedCourse], dst: Path, html_lesson_counts: dict[str, int]) -> None:
    entries = []
    for c in courses:
        entry = {
            "id": c.course_id,
            "title": c.title,
            "file": c.src_path.name,
            "total_lessons": html_lesson_counts.get(c.course_id, 0),
        }
        if c.strategy is None:
            entry["noProgress"] = True
        entries.append(entry)
    manifest = {"courses": entries, "generated_at": _iso_now()}
    (dst / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def count_lessons_runtime(html: str) -> int:
    """
    Count lessons by evaluating the course's own data declaration in a
    Node.js sandbox. This is the same code path the patched course uses at
    runtime, so the manifest count is guaranteed to match what
    `CoursesSync.attach({ lessonCount })` reports back to the API.

    Strategy:
      1. Extract the top-level `const COURSE = { ... };` or
         `const COURSE_DATA = [ ... ];` block from the HTML.
      2. Append a tiny epilogue that prints the lesson count.
      3. Run with `node -e` and parse the integer.

    Returns 0 if extraction or evaluation fails - the manifest will then
    fall back to the API-supplied count once the course is opened.
    """
    import subprocess

    pattern = r"(const\s+(COURSE_DATA|COURSE)\s*=\s*[\[\{][\s\S]*?^[\]\}];)"
    m = re.search(pattern, html, re.MULTILINE)
    if not m:
        return 0

    decl, var_name = m.group(1), m.group(2)
    modules_ref = "COURSE.modules" if var_name == "COURSE" else "COURSE_DATA"
    js = decl + (
        f"\nconsole.log({modules_ref}"
        f".reduce(function(s,m){{return s+(m.lessons?m.lessons.length:0);}},0));"
    )

    try:
        res = subprocess.run(
            ["node", "-e", js],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if res.returncode != 0:
            return 0
        return int(res.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return 0


# ---------------------------------------------------------------------------
# YAML / JSON overrides
# ---------------------------------------------------------------------------

def load_overrides(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict:
    out: dict = {}
    current_key: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" ") and line.endswith(":"):
            current_key = line[:-1].strip()
            out[current_key] = {}
        elif line.startswith(" ") and ":" in line and current_key:
            k, _, v = line.strip().partition(":")
            v = v.strip().strip('"').strip("'")
            out[current_key][k.strip()] = v
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--dst", required=True, type=Path)
    parser.add_argument("--overrides", type=Path, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not args.src.is_dir():
        print(f"ERROR: --src {args.src} is not a directory", file=sys.stderr)
        return 2

    args.dst.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides(args.overrides)
    if overrides:
        print(f"Loaded {len(overrides)} overrides from {args.overrides}")

    courses = []
    counts = {}
    patched = static = warned = 0

    for src_file in sorted(args.src.glob("*.html")):
        info = detect_course(src_file, overrides)
        courses.append(info)
        html = src_file.read_text(encoding="utf-8", errors="replace")
        counts[info.course_id] = count_lessons_runtime(html)
        dst_file = args.dst / src_file.name

        if info.strategy is None:
            dst_file.write_text(html, encoding="utf-8")
            label = "warn " if "warning" in info.extra else "stat "
            note = info.extra.get("warning", "no progress tracking")
            print(f"  {label}: {src_file.name}  ({info.course_id}) - {note}")
            static += 1
            if "warning" in info.extra:
                warned += 1
        else:
            new_html = patch_html(html, info)
            dst_file.write_text(new_html, encoding="utf-8")
            print(
                f"  patch: {src_file.name}  ({info.course_id}, "
                f"{info.strategy}-key, ~{counts[info.course_id]} lessons est.)"
            )
            patched += 1
            if args.verbose:
                key = info.single_key or info.multi_prefix
                print(f"         key={key!r}")

    write_manifest(courses, args.dst, counts)
    print(f"\nDone: {patched} patched, {static} static, {warned} warnings.")
    print(f"      manifest.json -> {args.dst / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
