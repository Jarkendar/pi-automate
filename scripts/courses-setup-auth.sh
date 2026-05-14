#!/usr/bin/env bash
# courses-setup-auth.sh - generate bcrypt hash and write credentials to .env.
#
# After running, the script adds (or updates) two entries in .env:
#   COURSES_HUB_USER=<user>
#   COURSES_HUB_PASS_HASH=<bcrypt hash>
#
# Caddy reads these on startup via {$VAR} substitution in Caddyfile.

set -euo pipefail

# Repo root = parent of scripts/
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

USER="${1:-jarek}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "-> Creating $ENV_FILE"
  touch "$ENV_FILE"
fi

echo -n "Password for user '$USER': "
read -rs PASSWORD
echo
echo -n "Repeat: "
read -rs PASSWORD2
echo

if [[ "$PASSWORD" != "$PASSWORD2" ]]; then
  echo "ERROR: passwords do not match" >&2
  exit 1
fi
if [[ -z "$PASSWORD" ]]; then
  echo "ERROR: password cannot be empty" >&2
  exit 1
fi

echo "-> Generating bcrypt hash via Caddy..."
HASH=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$PASSWORD")
if [[ -z "$HASH" ]]; then
  echo "ERROR: caddy hash-password returned empty result" >&2
  exit 1
fi

# Backup before modifying
cp "$ENV_FILE" "$ENV_FILE.bak"

# Upsert COURSES_HUB_USER and COURSES_HUB_PASS_HASH in .env
python3 - "$ENV_FILE" "$USER" "$HASH" <<'PY'
import sys, pathlib, re
path, user, hsh = sys.argv[1:]
p = pathlib.Path(path)
text = p.read_text() if p.exists() else ""

def upsert(text: str, key: str, val: str) -> str:
    line = f"{key}={val}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line + "\n"

text = upsert(text, "COURSES_HUB_USER", user)
text = upsert(text, "COURSES_HUB_PASS_HASH", hsh)
p.write_text(text)
print("OK")
PY

echo
echo "Done. Credentials written to $ENV_FILE."
echo "User: $USER"
echo
echo "Restart Caddy to pick up the new credentials:"
echo "  docker compose restart courses-caddy"
