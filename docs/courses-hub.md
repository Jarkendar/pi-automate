# Courses Hub

Moduł `pi-automate` do hostowania prywatnych kursów HTML z centralną synchronizacją progresu między urządzeniami.

## Co to robi

- Serwuje kursy HTML pod `http://courses.lan/courses/<plik>.html` (Basic Auth)
- Centralny dashboard pod `http://courses.lan/` z procentem ukończenia każdego kursu
- Jednolite API `/api/v1/*` jako *publiczny kontrakt* (Swagger UI: `/api/v1/docs`)
- Offline-first: kursy działają bez sieci, sync uzupełnia się gdy backend wróci
- Integracja z n8n: webhook gdy kurs osiągnie 100% (np. powiadomienie na Telegramie)
- Auto-detekcja struktury kursu — nie trzeba ręcznie konfigurować każdego pliku

## Architektura

```
courses.lan (Caddy + BasicAuth) ──┬── /              → dashboard
                                   ├── /courses/*    → spatchowane HTML (prywatne)
                                   ├── /api/v1/*     → FastAPI + SQLite
                                   └── /courses-sync.js → adapter
                                                ↓
                                   n8n-network ←──── n8n_pi_automate
                                                ↑
                                  (opcjonalny webhook na completion)
```

Wszystkie serwisy w istniejącej sieci `n8n-network`, co umożliwia n8n wołanie API kursów po nazwie kontenera.

## Co jest, a co nie jest w repo

| W repo (committed) | Poza repo (gitignored) |
|---|---|
| `courses_hub_backend/` — kod backendu | `courses_src/` — oryginalne HTML kursów |
| `courses_hub_frontend/` — dashboard, adapter | `courses_patched/` — spatchowane HTML (artefakt buildu) |
| `caddy/Caddyfile` — konfiguracja proxy | `courses_data/` — SQLite z progresem |
| `scripts/patch-courses.py` — patcher | `courses.local.yaml` — overrides tytułów |
| `scripts/courses-setup-auth.sh` — generator hasła | `.env` — credentials i webhook URL |
| `courses.example.yaml` — szablon overrides | |

## Setup (pierwsze uruchomienie)

### 1. Ustaw hasło Basic Auth

```bash
./scripts/courses-setup-auth.sh
```

Skrypt zapyta o hasło, wygeneruje bcrypt hash i zapisze `COURSES_HUB_USER` + `COURSES_HUB_PASS_HASH` do `.env`.

### 2. Wrzuć kursy

```bash
mkdir -p courses_src
cp ~/Downloads/course-*.html courses_src/
```

### 3. (Opcjonalnie) nadpisz tytuły, których auto-detekcja nie wykryła dobrze

```bash
cp courses.example.yaml courses.local.yaml
# edytuj courses.local.yaml — tylko kursy, których tytuł jest nie taki
```

### 4. Spatchuj kursy

```bash
make courses-patch
```

Wynik:
- W `courses_patched/` lądują kopie HTML z wstrzykniętym adapterem
- Generuje się `courses_patched/manifest.json` (dashboard go czyta)
- Skrypt jest idempotentny — można uruchamiać po każdej edycji kursu

### 5. Dodaj wpis DNS dla `courses.lan`

**Najlepiej**: dodaj w AdGuard/Pi-hole/routerze:
```
courses.lan → <IP_RPi>
```

**Alternatywnie**: w `/etc/hosts` na każdym urządzeniu. Lub pomiń — działa po IP RPi.

### 6. Start

```bash
docker compose up -d courses-hub courses-caddy
```

Otwórz `http://courses.lan/` (lub `http://<IP_RPi>/`), zaloguj się, ciesz się dashboardem.

## Dodanie kolejnego kursu

```bash
cp ~/Downloads/course-new-thing.html courses_src/
make courses-patch
# nie trzeba restartować — Caddy serwuje pliki na bieżąco
```

Auto-detekcja:
- Wykrywa `course_id` ze `slug:` w pliku lub z nazwy `course-<slug>.html`
- Wykrywa tytuł z `COURSE.title`; jeśli brak, humanizuje slug
- Wykrywa strategię zapisu (`single-key` z `const COURSE_KEY`/`storageKey:`, lub `multi-key` z `const LS_PREFIX`)
- Wstrzykuje adapter przed `</body>`
- Aktualizuje manifest

Jeśli auto-detekcja źle wykryła tytuł, dorzuć override w `courses.local.yaml`.

## Backup progresu

```bash
make courses-backup
```

Eksportuje SQLite jako SQL dump do `backups/courses_<ts>.sql`. Trzymaj w git/cloud (sam plik backupu jest bezpieczny — zawiera tylko progres, nie treść kursów).

Restore: `cat backups/courses_*.sql | docker compose exec -T courses-hub sqlite3 /data/courses.db`

## Integracja z n8n

Backend POSTuje JSON event do `COURSES_HUB_N8N_WEBHOOK_URL` gdy kurs przechodzi z `<100%` do `100%`. Tylko transition, nie re-fired przy każdym save.

**Payload:**
```json
{
  "event": "course.completed",
  "data": {
    "course_id": "kmp",
    "title": "Kotlin Multiplatform",
    "total_lessons": 53,
    "completed": 53,
    "progress_percent": 100.0,
    "notes_count": 12,
    "updated_at": "2026-05-14T..."
  },
  "ts": "2026-05-14T..."
}
```

**Setup w n8n:**
1. Stwórz workflow z node `Webhook` (method: POST)
2. Skopiuj jego URL z trybu test/prod (np. `http://n8n_pi_automate:5678/webhook/courses-completed`)
3. Wklej do `.env`: `COURSES_HUB_N8N_WEBHOOK_URL=http://n8n_pi_automate:5678/webhook/courses-completed`
4. `docker compose restart courses-hub`
5. Dorzuć w n8n co chcesz (Telegram, email, Notion entry, etc.)

Sieć `n8n-network` ogarnia DNS po nazwie kontenera, więc nie trzeba IP.

## Publiczny kontrakt API

| Metoda | Endpoint | Co robi |
|--------|----------|---------|
| GET    | `/api/health`                                | Liveness |
| GET    | `/api/v1/courses`                            | Lista kursów + procent |
| GET    | `/api/v1/courses/{id}`                       | Pełny stan kursu |
| PUT    | `/api/v1/courses/{id}`                       | Upsert pełnego stanu |
| PATCH  | `/api/v1/courses/{id}/lessons/{lesson_id}`   | Update jednej lekcji |
| DELETE | `/api/v1/courses/{id}`                       | Reset progresu |

Interaktywne docs: `http://courses.lan/api/v1/docs`.

Status normalizuje się serwerowo: `done`/`completed`/`finished` → `completed`, `in-progress`/`started` → `in_progress`.

## Troubleshooting

**Dashboard pokazuje 0% mimo że klikałem w kursie.**
Adapter wysyła PUT po debounce 800ms. Sprawdź konsolę przeglądarki — błędy z `[CoursesSync]` powiedzą czy backend nie odpowiada lub auth padł.

**Caddy nie startuje: `unknown directive`.**
Caddyfile używa `{$ENV_VAR}` substitution — wymaga obecności zmiennych w środowisku. Sprawdź czy `.env` ma `COURSES_HUB_USER` i `COURSES_HUB_PASS_HASH`.

**Spatchowany kurs nie synchronizuje.**
Otwórz DevTools → konsola, szukaj `[CoursesSync]`. Jeśli widzisz `attach failed: courseId is required` → patcher nie wykrył strategii. Dodaj kurs do `courses.local.yaml` z polem `strategy` (TODO: na razie patcher tego nie czyta z overrides — w razie czego edytuj wstrzyknięty bootstrap ręcznie).

**Port 80 zajęty na RPi.**
Zmień mapowanie w `docker-compose.yml`: `ports: ["8080:80"]`. Wtedy `courses.lan:8080`.
