# ============================================================================
# Courses Hub - module targets.
# Append to your main Makefile, or use as a standalone Makefile.
# ============================================================================

.PHONY: courses-patch courses-auth courses-up courses-down courses-logs courses-test courses-backup

# Default paths - override on command line, e.g.:
#   make courses-patch COURSES_SRC=~/Documents/courses
COURSES_SRC      ?= ./courses_src
COURSES_DST      ?= ./courses_patched
COURSES_OVERRIDE ?= ./courses.local.yaml

courses-patch:
	@if [ ! -d "$(COURSES_SRC)" ]; then \
		echo "ERROR: $(COURSES_SRC) does not exist. Put original course HTML files there."; \
		exit 1; \
	fi
	@OVR=""; [ -f "$(COURSES_OVERRIDE)" ] && OVR="--overrides $(COURSES_OVERRIDE)"; \
	python3 scripts/patch-courses.py --src $(COURSES_SRC) --dst $(COURSES_DST) $$OVR

courses-auth:
	bash scripts/courses-setup-auth.sh

courses-up:
	docker compose up -d courses-hub courses-caddy

courses-down:
	docker compose stop courses-hub courses-caddy

courses-logs:
	docker compose logs -f --tail=100 courses-hub courses-caddy

courses-test:
	@curl -sf http://localhost/api/health \
	  || { echo "Backend not responding"; exit 1; }
	@echo "OK: API is up"

courses-backup:
	@mkdir -p backups
	@ts=$$(date +%Y%m%d_%H%M%S); \
	docker compose exec -T courses-hub sqlite3 /data/courses.db .dump \
	  > backups/courses_$$ts.sql && \
	echo "-> backups/courses_$$ts.sql"
