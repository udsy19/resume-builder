#!/usr/bin/env bash
# Provision resume.udsy.in on a fresh Debian/Ubuntu VPS. Idempotent — safe to re-run.
#
#   sudo bash deploy/setup-vps.sh
#
# Secrets are NOT set here. After this finishes, edit /etc/resume-builder.env and
# restart the service. Nothing in this script writes a credential to disk in the repo.
set -euo pipefail

DOMAIN="${DOMAIN:-resume.udsy.in}"
APP_DIR="/opt/resume-builder"
REPO="${REPO:-https://github.com/udsy19/resume-builder.git}"
ENV_FILE="/etc/resume-builder.env"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

log "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# texlive-fonts-extra and cm-super are not optional: the templates use
# CormorantGaramond, FiraSans, roboto, sourcesanspro and noto-sans, and the default
# template needs T1 fontenc. Without these, every compile fails.
# Deliberately does NOT install a web server. A host that already terminates TLS —
# this one runs Caddy for other apps — must not have nginx dropped on top of it,
# fighting for ports 80 and 443 and taking the existing sites down with it.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip git curl ca-certificates \
    texlive-latex-base texlive-latex-recommended texlive-latex-extra \
    texlive-fonts-recommended texlive-fonts-extra cm-super

log "Service user"
id -u resume >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin resume

log "Application"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --quiet origin main
    git -C "$APP_DIR" reset --hard --quiet origin/main
else
    git clone --quiet "$REPO" "$APP_DIR"
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R resume:resume "$APP_DIR"

log "Environment file"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'ENV'
# Secrets for resume-builder. Root-owned, 0600. Fill these in, then:
#   systemctl restart resume-builder

ANTHROPIC_API_KEY=
# OPENAI_API_KEY=

# Gate the server's own API key behind a PIN. Without this, anyone who finds the
# URL can spend your credits.
ACCESS_PIN=
# Long random string. Without it, session tokens are signed with a secret derived
# from the PIN, so changing the PIN logs everyone out.
SESSION_SECRET=

# This host has a real pdflatex and no request ceiling, so the full loop can run.
# 900s is what a complete run costs; shorter measurably reduces keyword coverage.
RUN_BUDGET_SECONDS=900
ENV
    chmod 600 "$ENV_FILE"
    echo "  created $ENV_FILE — fill in the secrets before starting"
else
    echo "  $ENV_FILE already exists, left untouched"
fi

log "Verifying the LaTeX toolchain"
sudo -u resume "$APP_DIR/.venv/bin/python" "$APP_DIR/tests/check_offline.py" \
    || { echo "Offline checks failed — fix before serving traffic." >&2; exit 1; }

log "systemd service"
cp "$APP_DIR/deploy/resume-builder.service" /etc/systemd/system/resume-builder.service
systemctl daemon-reload
systemctl enable --quiet resume-builder

log "Reverse proxy"
if command -v caddy >/dev/null 2>&1; then
    # Caddy is already terminating TLS for other sites on this host. Append our block
    # rather than replacing anything, and only once.
    if ! grep -q "resume-builder" /etc/caddy/Caddyfile 2>/dev/null; then
        {
            echo ""
            echo "# resume-builder (appended by deploy/setup-vps.sh — do not duplicate)"
            cat "$APP_DIR/deploy/Caddyfile-resume"
        } >> /etc/caddy/Caddyfile
        echo "  appended a site block to /etc/caddy/Caddyfile"
    else
        echo "  Caddyfile already has a resume-builder block, left untouched"
    fi
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null \
        || { echo "Caddyfile did not validate — not reloading." >&2; exit 1; }
    systemctl reload caddy
    echo "  Caddy reloaded; it provisions TLS automatically once DNS resolves here"
elif command -v nginx >/dev/null 2>&1; then
    cp "$APP_DIR/deploy/nginx-resume.conf" "/etc/nginx/sites-available/$DOMAIN"
    ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
    nginx -t && systemctl reload nginx
else
    echo "  No Caddy or nginx found. Install one and proxy it to 127.0.0.1:8020;"
    echo "  deploy/Caddyfile-resume and deploy/nginx-resume.conf are ready to use."
fi

log "Starting"
systemctl restart resume-builder
sleep 3
systemctl --no-pager --lines=5 status resume-builder || true

cat <<DONE

Done. Remaining steps:

  1. Fill in the secrets:      sudo nano $ENV_FILE
  2. Restart:                  sudo systemctl restart resume-builder
  3. Check:                    curl -s https://$DOMAIN/api/health

  Logs:                        sudo journalctl -u resume-builder -f
  Update to latest main:       sudo bash $APP_DIR/deploy/setup-vps.sh

DONE
