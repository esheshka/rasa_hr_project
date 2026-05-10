#!/usr/bin/env bash
# Публикация на GitHub по SSH (ключ ~/.ssh/id_ed25519_hr_ml_zadanie1).
#
# Репозиторий: https://github.com/esheshka/rasa_hr_project
#
# Запуск из корня этого репозитория:
#   ./publish_to_github.sh
#
# Другой аккаунт/репозиторий:
#   export GITHUB_USER=... GITHUB_REPO_NAME=... && ./publish_to_github.sh

set -euo pipefail
cd "$(dirname "$0")"

GITHUB_USER="${GITHUB_USER:-esheshka}"
REPO_NAME="${GITHUB_REPO_NAME:-rasa_hr_project}"
REMOTE_URL="git@github.com-hrml-zadanie1:${GITHUB_USER}/${REPO_NAME}.git"

git remote remove origin 2>/dev/null || true
git remote add origin "$REMOTE_URL"
echo "remote: $REMOTE_URL"
GIT_SSH_COMMAND="ssh -o BatchMode=yes" git push -u origin main
echo "Готово: https://github.com/${GITHUB_USER}/${REPO_NAME}"
