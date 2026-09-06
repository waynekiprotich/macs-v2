#!/bin/bash
# Guard against committing credentials.
#
# Written after a live Deriv API token was found hardcoded in two tracked
# scratch scripts (deriv_auth_test.py, deriv_symbols.py) and pushed to a
# GitHub remote. .env was correctly gitignored, but those scripts inlined
# the token directly and bypassed that protection entirely.
#
# Install as a pre-commit hook:
#   ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit
#
# Or run manually:  ./scripts/check_secrets.sh

set -uo pipefail

# Patterns for credentials this project actually uses.
PATTERNS=(
    'pat_[a-zA-Z0-9]{20,}'                      # Deriv API token
    'AQ\.[a-zA-Z0-9_-]{20,}'                    # Google/Gemini API key
    'discord\.com/api/webhooks/[0-9]+/[a-zA-Z0-9_-]+'   # Discord webhook
    'postgresql://[^:]+:[^@]+@'                 # Postgres URL with inline password
)

staged=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$staged" ] && exit 0

found=0
for file in $staged; do
    # .env is gitignored; skip anything that isn't actually readable text
    [ -f "$file" ] || continue
    # Skip this script itself — it necessarily contains the patterns it hunts for.
    case "$file" in */check_secrets.sh|check_secrets.sh) continue ;; esac
    for pattern in "${PATTERNS[@]}"; do
        if grep -qE "$pattern" "$file" 2>/dev/null; then
            echo "BLOCKED: possible credential in $file (pattern: $pattern)"
            found=1
        fi
    done
done

if [ "$found" -ne 0 ]; then
    echo ""
    echo "Move the value into .env (already gitignored) and read it with"
    echo "os.environ.get(...) instead of hardcoding it."
    echo "To override for a false positive: git commit --no-verify"
    exit 1
fi

exit 0
