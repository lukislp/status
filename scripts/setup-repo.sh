#!/usr/bin/env bash
# One-time setup of the lukislp/status repository: everything that can be done without the
# GitHub or Cloudflare web UI. Idempotent - safe to run again; finished steps are skipped.
#
#   scripts/setup-repo.sh [--dry-run] [--no-automerge] [--rotate-keys] [--repo owner/name]
#
# Runs in Git Bash on Windows and on Linux. Needs: gh (logged in), ssh-keygen, python3/python,
# curl (only for the optional Cloudflare step). Optional env: CLOUDFLARE_API_TOKEN and
# CLOUDFLARE_ZONE_ID (token with Zone.DNS edit on lktec.org) to create the DNS record.
#
# Private key material is written only to a temporary directory, handed to `gh secret set` on
# stdin and deleted on exit. Nothing secret is printed.
set -euo pipefail

REPO="lukislp/status"
DOMAIN="status.lktec.org"
PAGES_TARGET="lukislp.github.io"
RULESET_SOURCE="lukislp/demosites"
RULESET_NAME="main protection"
REQUIRED_CHECKS=("test" "lint")
DRY_RUN=0
AUTOMERGE=1
ROTATE=0

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-automerge) AUTOMERGE=0 ;;
    --rotate-keys) ROTATE=1 ;;
    --repo) REPO="$2"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

cd "$(dirname "$0")/.."

DONE=()
LEFT=()
note_done() { DONE+=("$1"); }
note_left() { LEFT+=("$1"); }
say() { printf '\n==> %s\n' "$*"; }
warn() { printf '    ! %s\n' "$*" >&2; }

# Run a command, or only print it with --dry-run.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

TMP="$(mktemp -d)"
# shellcheck disable=SC2329 # invoked through the trap below
cleanup() {
  if [ -d "$TMP" ]; then
    # shellcheck disable=SC2016 # $1 is expanded by the inner sh, on purpose
    find "$TMP" -type f -exec sh -c 'command -v shred >/dev/null 2>&1 && shred -u "$1" || rm -f "$1"' _ {} \;
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- prerequisites

say "Checking prerequisites"
for tool in gh ssh-keygen; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing: $tool" >&2; exit 1; }
done
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
[ -n "$PY" ] || { echo "missing: Python 3.11+ (python3 or python)" >&2; exit 1; }

if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not logged in - run: gh auth login" >&2
  exit 1
fi
SCOPES="$(gh auth status 2>&1 | grep -i 'token scopes' || true)"
MISSING=()
# gh prints e.g.: Token scopes: 'gist', 'read:org', 'repo'
[[ "$SCOPES" == *"'repo'"* ]] || MISSING+=("repo")
# write:ssh_signing_key is enough for adding a signing key; admin: includes it.
if [[ "$SCOPES" != *"'admin:ssh_signing_key'"* && "$SCOPES" != *"'write:ssh_signing_key'"* ]]; then
  MISSING+=("admin:ssh_signing_key")
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "gh token is missing scopes: ${MISSING[*]}" >&2
  echo "run:  gh auth refresh -h github.com -s $(IFS=,; echo "${MISSING[*]}")" >&2
  exit 1
fi
gh repo view "$REPO" >/dev/null 2>&1 || { echo "repository $REPO not found or not accessible" >&2; exit 1; }
SECRETS="$(gh secret list --repo "$REPO" 2>/dev/null | cut -f1 || true)"
has_secret() { printf '%s\n' "$SECRETS" | grep -qx "$1"; }
echo "    ok: gh, ssh-keygen, $PY; repository $REPO"
[ "$DRY_RUN" = 1 ] && echo "    dry run: nothing will be changed"

# --------------------------------------------------------------------------- 1. signing key

say "1/7 Bot commit signing key (secret DEPLOY_SIGNING_KEY)"
if has_secret DEPLOY_SIGNING_KEY && [ "$ROTATE" = 0 ]; then
  echo "    secret exists - skipped (use --rotate-keys to replace it)"
else
  title="status bot signing $(date +%Y-%m-%d)"
  ssh-keygen -q -t ed25519 -N "" -C "$title" -f "$TMP/signing"
  # Registered as an account *signing* key so the bot commits show "Verified".
  run gh ssh-key add "$TMP/signing.pub" --type signing --title "$title"
  if [ "$DRY_RUN" = 1 ]; then
    echo "    [dry-run] gh secret set DEPLOY_SIGNING_KEY --repo $REPO < (private key)"
  else
    gh secret set DEPLOY_SIGNING_KEY --repo "$REPO" < "$TMP/signing"
  fi
  note_done "signing key registered and stored as DEPLOY_SIGNING_KEY"
  [ "$ROTATE" = 1 ] && note_left "remove the previous 'status bot signing' key at https://github.com/settings/keys"
fi

# --------------------------------------------------------------------------- 2. deploy key

say "2/7 Write deploy key for bot pushes (secret SEMANTIC_RELEASE_DEPLOY_KEY)"
if has_secret SEMANTIC_RELEASE_DEPLOY_KEY && [ "$ROTATE" = 0 ]; then
  echo "    secret exists - skipped (use --rotate-keys to replace it)"
else
  title="status bot push $(date +%Y-%m-%d)"
  ssh-keygen -q -t ed25519 -N "" -C "$title" -f "$TMP/deploy"
  run gh repo deploy-key add "$TMP/deploy.pub" --repo "$REPO" --allow-write --title "$title"
  if [ "$DRY_RUN" = 1 ]; then
    echo "    [dry-run] gh secret set SEMANTIC_RELEASE_DEPLOY_KEY --repo $REPO < (private key)"
  else
    gh secret set SEMANTIC_RELEASE_DEPLOY_KEY --repo "$REPO" < "$TMP/deploy"
  fi
  note_done "write deploy key added and stored as SEMANTIC_RELEASE_DEPLOY_KEY"
  [ "$ROTATE" = 1 ] && note_left "delete the old 'status bot push' deploy key: gh repo deploy-key list --repo $REPO"
fi

# --------------------------------------------------------------------------- 3. labels

say "3/7 Issue labels"
while IFS=$'\t' read -r name description; do
  [ -n "$name" ] || continue
  case "$name" in
    incident) color="d93a4b" ;;
    maintenance) color="6e63e0" ;;
    impact:*) color="e5712a" ;;
    *) color="c5d0dc" ;;
  esac
  run gh label create "$name" --repo "$REPO" --description "$description" --color "$color" --force
done < <("$PY" -m statuspage labels | tr -d '\r')
note_done "labels created/updated"

# --------------------------------------------------------------------------- 4. pages

say "4/7 GitHub Pages (build from Actions, custom domain $DOMAIN)"
if gh api "repos/$REPO/pages" >/dev/null 2>&1; then
  run gh api -X PUT "repos/$REPO/pages" -f build_type=workflow --silent
else
  run gh api -X POST "repos/$REPO/pages" -f build_type=workflow --silent
fi
run gh api -X PUT "repos/$REPO/pages" -f cname="$DOMAIN" --silent
if [ "$DRY_RUN" = 1 ]; then
  run gh api -X PUT "repos/$REPO/pages" -F https_enforced=true --silent
elif gh api -X PUT "repos/$REPO/pages" -F https_enforced=true --silent 2>/dev/null; then
  note_done "Pages enabled (workflow) with custom domain $DOMAIN and HTTPS enforced"
else
  warn "HTTPS cannot be enforced yet - GitHub issues the certificate once DNS points at $PAGES_TARGET."
  note_left "after DNS works (minutes to an hour), run this script again to enforce HTTPS"
  note_done "Pages enabled (workflow) with custom domain $DOMAIN"
fi

# --------------------------------------------------------------------------- 5. ruleset

say "5/7 Ruleset for main (copied from $RULESET_SOURCE)"
if gh api "repos/$REPO/rulesets" --jq '.[].name' 2>/dev/null | tr -d '\r' | grep -qx "$RULESET_NAME"; then
  echo "    ruleset '$RULESET_NAME' exists - skipped"
else
  source_id="$(gh api "repos/$RULESET_SOURCE/rulesets" --jq ".[] | select(.name == \"$RULESET_NAME\") | .id" | tr -d '\r' | head -n1)"
  [ -n "$source_id" ] || { echo "no ruleset '$RULESET_NAME' in $RULESET_SOURCE" >&2; exit 1; }
  gh api "repos/$RULESET_SOURCE/rulesets/$source_id" > "$TMP/source-ruleset.json"
  CHECKS="${REQUIRED_CHECKS[*]}" "$PY" - "$TMP/source-ruleset.json" "$TMP/ruleset.json" <<'PYEOF'
import json, os, sys

src = json.load(open(sys.argv[1], encoding="utf-8"))
checks = os.environ["CHECKS"].split()
rules = []
for rule in src["rules"]:
    if rule["type"] == "required_status_checks":
        rule = {"type": rule["type"], "parameters": dict(rule["parameters"])}
        rule["parameters"]["required_status_checks"] = [{"context": c} for c in checks]
    rules.append(rule)
if not any(r["type"] == "required_signatures" for r in rules):
    rules.append({"type": "required_signatures"})
# The bot pushes data/ to main with the write deploy key; admins merge through pull requests.
bypass = [{"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"},
          {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}]
for actor in src.get("bypass_actors") or []:
    if actor not in bypass:
        bypass.append({k: actor[k] for k in ("actor_id", "actor_type", "bypass_mode")})
out = {"name": src["name"], "target": src["target"], "enforcement": src["enforcement"],
       "conditions": src["conditions"], "rules": rules, "bypass_actors": bypass}
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), indent=1)
print("    rules:", ", ".join(r["type"] for r in rules))
print("    required checks:", ", ".join(checks))
PYEOF
  run gh api -X POST "repos/$REPO/rulesets" --input "$TMP/ruleset.json" --silent
  note_done "ruleset '$RULESET_NAME' created (checks: ${REQUIRED_CHECKS[*]}; signed commits; deploy key bypass)"
  note_left "review the ruleset once: https://github.com/$REPO/settings/rules"
fi

# --------------------------------------------------------------------------- 6. DNS

say "6/7 DNS: CNAME status -> $PAGES_TARGET (DNS only, not proxied)"
if [ -n "${CLOUDFLARE_API_TOKEN:-}" ] && [ -n "${CLOUDFLARE_ZONE_ID:-}" ]; then
  command -v curl >/dev/null 2>&1 || { echo "missing: curl" >&2; exit 1; }
  api="https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records"
  auth_header="$TMP/cf-auth"
  printf 'Authorization: Bearer %s\n' "$CLOUDFLARE_API_TOKEN" > "$auth_header"
  record_id="$(curl -fsS -H @"$auth_header" "$api?type=CNAME&name=$DOMAIN" \
    | "$PY" -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")')"
  body="{\"type\":\"CNAME\",\"name\":\"$DOMAIN\",\"content\":\"$PAGES_TARGET\",\"proxied\":false,\"ttl\":1,\"comment\":\"GitHub Pages: lukislp/status\"}"
  if [ -n "$record_id" ]; then
    run curl -fsS -o /dev/null -X PUT -H @"$auth_header" -H "Content-Type: application/json" --data "$body" "$api/$record_id"
  else
    run curl -fsS -o /dev/null -X POST -H @"$auth_header" -H "Content-Type: application/json" --data "$body" "$api"
  fi
  note_done "Cloudflare CNAME $DOMAIN -> $PAGES_TARGET (DNS only)"
else
  echo "    CLOUDFLARE_API_TOKEN / CLOUDFLARE_ZONE_ID not set - create this record by hand in Cloudflare:"
  echo "      Type CNAME   Name status   Target $PAGES_TARGET   Proxy status: DNS only (grey cloud)"
  note_left "create the DNS record: CNAME status -> $PAGES_TARGET, DNS only"
fi

# --------------------------------------------------------------------------- 7. automerge

say "7/7 AUTOMERGE_TOKEN for Dependabot auto-merge"
if [ "$AUTOMERGE" = 0 ]; then
  echo "    skipped (--no-automerge)"
  note_left "Dependabot PRs will not auto-merge until AUTOMERGE_TOKEN is set"
elif has_secret AUTOMERGE_TOKEN; then
  echo "    secret exists - skipped"
elif [ "$DRY_RUN" = 1 ]; then
  echo "    [dry-run] read -s token; gh secret set AUTOMERGE_TOKEN --repo $REPO"
else
  echo "    Paste the fine-grained PAT used for auto-merge in your other repos (input hidden, empty = skip)."
  echo "    Add $REPO to that PAT's repository access first."
  token=""
  read -r -s -p "    AUTOMERGE_TOKEN: " token || true
  echo
  if [ -n "$token" ]; then
    printf '%s' "$token" | gh secret set AUTOMERGE_TOKEN --repo "$REPO"
    unset token
    note_done "AUTOMERGE_TOKEN stored"
  else
    note_left "AUTOMERGE_TOKEN not set (Dependabot auto-merge stays off)"
  fi
fi

# --------------------------------------------------------------------------- summary

say "Summary"
if [ "${#DONE[@]}" -gt 0 ]; then
  echo "  done:"
  printf '    - %s\n' "${DONE[@]}"
fi
if [ "${#LEFT[@]}" -gt 0 ]; then
  echo "  left to do:"
  printf '    - %s\n' "${LEFT[@]}"
fi
[ "$DRY_RUN" = 1 ] && echo "  (dry run - nothing was changed)"
exit 0
