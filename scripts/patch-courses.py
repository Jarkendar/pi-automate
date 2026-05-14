#!/usr/bin/env python3
"""
patch-courses.py - Courses Hub patcher (module pi-automate).

Reads private course HTML files from --src, auto-detects their storage strategy,
injects the Courses Hub sync adapter, and writes patched copies to --dst.
Also generates a manifest.json that the dashboard uses to render the course list.

Why auto-detect: courses are private and not committed to the repo, so the
patcher cannot hardcode their slugs/keys. It introspects each file instead.

Detection logic
---------------
1. Extract course slug - from filename (course-<slug>.html) or COURSE.slug if present.
2. Extract title - from COURSE.title literal in the HTML.
3. Detect storage strategy:
   - Single-key (most courses): file contains `const COURSE_KEY = "..."` or
     `const STORAGE_KEY = COURSE.storageKey` plus `storageKey: "..."`.
   - Multi-key (e.g. biznes-tech-communication): file contains
     `const LS_PREFIX = `course_${COURSE.slug}_``  pattern.
4. Skip files that don't reference localStorage at all (treat as static).

The patch is idempotent - files containing the marker are skipped.
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
# Status indicator HTML
# ---------------------------------------------------------------------------
INDICATOR_HTML = """
<div id="__courses_sync_indicator" style="
  position: fixed; bottom: 12px; right: 12px; z-index: 9999;
  font: 12px ui-sans-serif, system-ui, sans-serif;
  background: rgba(0,0,0,0.75); color: #fff;
  padding: 6px 10px; border-radius: 999px;
  opacity: 0; transition: opacity 0.3s ease;
  pointer-events: none;
">syncing…</div>
<script>
(function(){
  const el = document.getElementById('__courses_sync_indicator');
  const labels = {
    syncing: ['☁ syncing…', 'rgba(0,0,0,0.75)'],
    synced:  ['✓ synced',   'rgba(29,158,117,0.9)'],
    pulled:  ['↓ updated',  'rgba(29,158,117,0.9)'],
    error:   ['✗ sync error','rgba(192,57,43,0.9)'],
    offline: ['⊘ offline',  'rgba(120,120,120,0.85)'],
  };
  let hideT = null;
  window.addEventListener('courses-sync', (ev) => {
    const [text, bg] = labels[ev.detail.state] || ['', ''];
    if (!text) return;
    el.textContent = text;
    el.style.background = bg;
    el.style.opacity = '1';
    if (hideT) clearTimeout(hideT);
    if (ev.detail.state === 'synced' || ev.detail.state === 'pulled') {
      hideT = setTimeout(() => { el.style.opacity = '0'; }, 1500);
    }
  });
})();
</script>
"""


@dataclass
class DetectedCourse:
    """Auto-detected course config."""
    src_path: Path
    course_id: str
    title: str
    strategy: str | None = None     # "single" | "multi" | None (static)
    single_key: str | None = None
    multi_prefix: str | None = None
    total_lessons: int = 0
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _strip_diacritics(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def slug_from_filename(path: Path) -> str:
    name = path.stem
    name = re.sub(r"^course[-_]", "", name)   # strip "course-" / "course_"
    return name.replace("_", "-").lower()


def extract_string_literal(html: str, pattern: str) -> str | None:
    """Find first regex match and return capture group 1 if present."""
    m = re.search(pattern, html)
    return m.group(1) if m else None


def humanize_slug(slug: str) -> str:
    """Convert 'android-architecture' -> 'Android Architecture'."""
    parts = re.split(r"[-_]+", slug)
    return " ".join(p[:1].upper() + p[1:] for p in parts if p)


def extract_course_title(html: str) -> str | None:
    """Extract the top-level course title.

    Only trust a `title:` that:
      (a) lives within the first ~400 chars of `const COURSE = {`, AND
      (b) is NOT inside a `modules: [` array - i.e. there is no `modules:` token
          between `const COURSE = {` and the `title:` we found.

    Many of our courses define `COURSE = { modules: [...] }` with no top-level
    title - in that case the first `title:` we'd find belongs to module 1, not
    to the course. We refuse to return such false positives.
    """
    m = re.search(r"const\s+COURSE\s*=\s*\{", html)
    if not m:
        return None
    window = html[m.end(): m.end() + 400]
    # If `modules:` precedes the first `title:`, the title belongs to module 1
    mods = re.search(r"\bmodules\s*:\s*\[", window)
    t = re.search(r"\btitle\s*:\s*['\"]([^'\"]+)['\"]", window)
    if not t:
        return None
    if mods and mods.start() < t.start():
        return None  # would be a module title, not course title
    return t.group(1)


def load_overrides(path: Path | None) -> dict:
    """Load optional YAML/JSON overrides keyed by course_id.

    Format (YAML, JSON, or plain dict - we accept any of these via stdlib):
      android-architecture:
        title: "Android Architecture"
      kmp:
        title: "Kotlin Multiplatform"
        # future: aliases, totalLessons override, etc.
    """
    if not path or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # Try JSON first (zero deps); fall back to a tiny YAML subset parser
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML parser - enough for `course_id:\\n  field: value` shape.

    Avoids adding PyYAML as a dependency; the override file is intentionally
    simple. For anything richer, rename the file to .json.
    """
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


def detect_course(path: Path, overrides: dict) -> DetectedCourse:
    html = path.read_text(encoding="utf-8", errors="replace")
    course_id = (
        extract_string_literal(html, r"\bslug\s*:\s*['\"]([^'\"]+)['\"]")
        or slug_from_filename(path)
    )
    title = (
        overrides.get(course_id, {}).get("title")
        or extract_course_title(html)
        or humanize_slug(course_id)
    )

    info = DetectedCourse(src_path=path, course_id=course_id, title=title)

    # No localStorage -> treat as static
    if "localStorage" not in html:
        return info  # strategy=None

    # Single-key strategies (in order of specificity)
    single_key = (
        extract_string_literal(html, r"const\s+COURSE_KEY\s*=\s*['\"]([^'\"]+)['\"]")
        or extract_string_literal(html, r"\bstorageKey\s*:\s*['\"]([^'\"]+)['\"]")
    )
    if single_key:
        info.strategy = "single"
        info.single_key = single_key
        info.total_lessons = count_lessons(html)
        return info

    # Multi-key strategy - look for LS_PREFIX template literal
    # const LS_PREFIX = `course_${COURSE.slug}_`;
    m = re.search(r"const\s+LS_PREFIX\s*=\s*`([^`]+)`", html)
    if m:
        # Substitute ${COURSE.slug} with the detected slug
        prefix = m.group(1).replace("${COURSE.slug}", course_id)
        info.strategy = "multi"
        info.multi_prefix = prefix
        info.total_lessons = count_lessons(html)
        return info

    # localStorage present but no recognised pattern - warn, skip patch
    info.extra["warning"] = "Uses localStorage but no recognised key pattern"
    return info


def count_lessons(html: str) -> int:
    """Two-strategy lesson counter - picks the larger of explicit-id and implicit-title counts."""
    explicit = len(re.findall(r"\{\s*id\s*:\s*['\"]", html))
    titles = len(re.findall(r"\btitle\s*:\s*['\"]", html))
    modules = len(re.findall(r"\blessons\s*:\s*\[", html))
    implicit = max(titles - modules - 1, 0)  # -1 for COURSE.title itself
    return max(explicit, implicit)


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------


def build_bootstrap(info: DetectedCourse) -> str:
    if info.strategy == "single":
        cfg = (
            f'courseId: "{info.course_id}",\n      '
            f'title: "{js_escape(info.title)}",\n      '
            f'singleKey: "{info.single_key}",\n      '
            f'totalLessons: {info.total_lessons},'
        )
    elif info.strategy == "multi":
        # COURSE.modules is in scope; collect lesson ids using its `id` field or fallback to m{idx}_l{idx}.
        cfg = (
            f'courseId: "{info.course_id}",\n      '
            f'title: "{js_escape(info.title)}",\n      '
            f'multiKey: {{ prefix: "{info.multi_prefix}", lessonIds: '
            f"(function(){{var ids=[];COURSE.modules.forEach(function(m,mi){{m.lessons.forEach(function(l,li){{"
            f"ids.push(l.id || ('m'+(mi+1)+'_l'+(li+1)));}});}});return ids;}})() }},\n      "
            f'totalLessons: {info.total_lessons},'
        )
    else:
        raise ValueError(f"Cannot build bootstrap for strategy {info.strategy!r}")

    return f"""
{MARKER}
<script src="/courses-sync.js"></script>
{INDICATOR_HTML}
<script>
// Wait for both the adapter and the course's own COURSE object before attaching.
(function tryAttach() {{
  if (typeof CoursesSync === "undefined") return setTimeout(tryAttach, 50);
  if (typeof COURSE === "undefined")      return setTimeout(tryAttach, 50);
  try {{
    CoursesSync.attach({{
      {cfg}
    }});
  }} catch (e) {{
    console.error("[CoursesSync] attach failed:", e);
  }}
}})();
</script>
"""


def js_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


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


def write_manifest(courses: list[DetectedCourse], dst: Path) -> None:
    """Generate manifest.json the dashboard consumes."""
    entries = []
    for c in courses:
        entry = {
            "id": c.course_id,
            "title": c.title,
            "file": c.src_path.name,
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path,
                        help="Source dir with private course HTML files (gitignored)")
    parser.add_argument("--dst", required=True, type=Path,
                        help="Output dir for patched copies (gitignored)")
    parser.add_argument("--overrides", type=Path, default=None,
                        help="Optional YAML/JSON file with per-course title overrides")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    overrides = load_overrides(args.overrides)
    if overrides:
        print(f"Loaded {len(overrides)} overrides from {args.overrides}")

    if not args.src.is_dir():
        print(f"ERROR: --src {args.src} is not a directory", file=sys.stderr)
        return 2

    args.dst.mkdir(parents=True, exist_ok=True)

    courses = []
    patched = 0
    static = 0
    warnings = 0

    for src_file in sorted(args.src.glob("*.html")):
        info = detect_course(src_file, overrides)
        courses.append(info)

        html = src_file.read_text(encoding="utf-8", errors="replace")
        dst_file = args.dst / src_file.name

        if info.strategy is None:
            dst_file.write_text(html, encoding="utf-8")
            tag = "warn " if "warning" in info.extra else "stat "
            print(f"  {tag}: {src_file.name}  ({info.course_id})"
                  + (f"  - {info.extra['warning']}" if "warning" in info.extra else "  - no progress tracking"))
            static += 1
            if "warning" in info.extra:
                warnings += 1
        else:
            new_html = patch_html(html, info)
            dst_file.write_text(new_html, encoding="utf-8")
            print(f"  patch: {src_file.name}  ({info.course_id}, "
                  f"{info.strategy}-key, {info.total_lessons} lessons)")
            patched += 1
            if args.verbose:
                key = info.single_key or info.multi_prefix
                print(f"         title={info.title!r}, key={key!r}")

    # Generate manifest for dashboard
    write_manifest(courses, args.dst)

    print(f"\nDone: {patched} patched, {static} static, {warnings} warnings.")
    print(f"      manifest.json -> {args.dst / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
