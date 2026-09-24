#!/bin/bash
#
# First-time install for the Aged Ticket Advisor.
# Idempotent - safe to re-run after a config change.
#
#   ./ops/install.sh
#
# Nothing here touches the existing itsm_analytics deployment.

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${ATA_VENV:-/genai/etc/scripts/genai_venv_1}"
PYTHON="$VENV/bin/python"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

cd "$BASE_DIR"

# ---------------------------------------------------------------------------
say "Checking prerequisites"
# ---------------------------------------------------------------------------
[[ -x "$PYTHON" ]] || die "Python not found at $PYTHON. Set ATA_VENV to your venv."
command -v mysql >/dev/null || die "mysql client not on PATH"

"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is required"

echo "Python:  $($PYTHON --version)"
echo "Project: $BASE_DIR"

# ---------------------------------------------------------------------------
say "Installing dependencies"
# ---------------------------------------------------------------------------
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -r requirements.txt
echo "Dependencies installed."

# ---------------------------------------------------------------------------
say "Preparing configuration"
# ---------------------------------------------------------------------------
if [[ ! -f config/conf.json ]]; then
  cp config/conf.sample.json config/conf.json
  echo "Created config/conf.json from the sample."
  echo "EDIT IT before continuing - the ServiceNow URL and Vault paths are placeholders."
else
  echo "config/conf.json already present, leaving it alone."
fi

if [[ ! -f config/.vault_role_id || ! -f config/.vault_secret_id ]]; then
  echo
  echo "AppRole credentials are missing. Create the role on the Vault server:"
  echo "    vault auth enable approle"
  echo "    vault write auth/approle/role/sd-advisor \\"
  echo "        token_policies=sd-advisor token_ttl=1h token_max_ttl=24h \\"
  echo "        secret_id_ttl=0 secret_id_num_uses=0 \\"
  echo "        secret_id_bound_cidrs=\"\$(hostname -I | awk '{print \$1}')/32\""
  echo
  echo "Then install both credentials here (do NOT reuse the audit tool's token):"
  echo "    install -m 600 /dev/stdin config/.vault_role_id   <<< '<ROLE_ID>'"
  echo "    install -m 600 /dev/stdin config/.vault_secret_id <<< '<SECRET_ID>'"
  echo
  echo "Set vault.auth_method to 'approle' in config/conf.json."
fi

mkdir -p logs
chmod 700 config logs 2>/dev/null || true
for f in config/.vault_token config/.vault_role_id config/.vault_secret_id; do
  [[ -f "$f" ]] && chmod 600 "$f"
done

# ---------------------------------------------------------------------------
say "Creating the database schema"
# ---------------------------------------------------------------------------
read -r -p "Apply db/schema.sql now? Needs a MySQL user that can CREATE DATABASE [y/N] " reply
if [[ "${reply,,}" == "y" ]]; then
  read -r -p "MySQL admin user [root]: " db_user
  mysql -u "${db_user:-root}" -p < db/schema.sql
  echo "Schema applied to sd_advisor_db."
else
  echo "Skipped. Run manually:  mysql -u root -p < db/schema.sql"
fi

# ---------------------------------------------------------------------------
say "Required Vault secrets"
# ---------------------------------------------------------------------------
cat <<'SECRETS'
Write these before starting the service (paths are configurable in conf.json):

  vault kv put secret/sd_advisor_db      <dbuser>=<dbpassword>
  vault kv put secret/snow_advisor       <snowuser>=<snowpassword>
  vault kv put secret/sd_advisor_llm     azure_endpoint=https://... \
                                         azure_api_key=... \
                                         azure_api_version=2024-10-21
  vault kv put secret/sd_advisor_web     secret_key=$(openssl rand -base64 48)

  # NOT required: secret/sd_advisor_smtp
  #   mailhost.kohler.com:25 is an anonymous internal relay. The mailer treats
  #   a missing SMTP secret as an open relay. Add it only if auth is introduced.

The ServiceNow account needs READ on: incident, sys_history_line, task_sla,
kb_knowledge, m2m_kb_task, change_request, problem, sys_user. It needs no
write permission anywhere.
SECRETS

# ---------------------------------------------------------------------------
say "Next steps"
# ---------------------------------------------------------------------------
cat <<NEXT
  1. Edit config/conf.json (ServiceNow URL, assignment groups, SMTP, base_url).
  2. Verify everything:        $PYTHON run.py doctor
  3. Create an admin account:  $PYTHON run.py adduser --username you --role ADMIN --email you@corp
  4. Build the evidence index: $PYTHON run.py backfill --days 180     # takes a while, run once
  5. First pass by hand:       $PYTHON run.py sync --full && $PYTHON run.py signals
  6. Preview the digest:       $PYTHON run.py preview -o /tmp/digest.html
  7. Install the service:      sudo cp ops/aged-ticket-advisor.service /etc/systemd/system/
                               sudo systemctl daemon-reload
                               sudo systemctl enable --now aged-ticket-advisor

Shadow mode is ON in the sample config. Nothing will be emailed until you set
runtime.shadow_mode to false - leave it on for the first two weeks.
NEXT
