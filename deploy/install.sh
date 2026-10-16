#!/usr/bin/env bash
# Установка/обновление блога на проде 83.220.175.197 (алиас priemka). Запуск с Mac из корня репозитория:
#     deploy/install.sh            # показать, что будет сделано
#     deploy/install.sh --go       # сделать
# Ставит сборщик и синхронизацию в /opt/vebverf-blog, юниты в systemd, строку Sitemap в robots.txt.
# Анонсы (bot/blog_announce.py) приезжают отдельно, деплоем vebverf-bot.
# git на сервере нет — файлы идут через ssh сжатыми, с проверкой sha256.
set -euo pipefail
HOST=${HOST:-priemka}
GO=${1:-}
cd "$(dirname "$0")/.."

FILES=(build.py deploy/blog_sync.py)
UNITS=(deploy/vebverf-blog-sync.service deploy/vebverf-blog-sync.timer)

run() { if [ "$GO" = "--go" ]; then "$@"; else echo "  [план] $*"; fi; }

echo "▸ проверка: тесты"
python3 -m unittest discover -s tests >/dev/null 2>&1 || { echo "✗ тесты красные — не ставлю"; exit 1; }

put() {  # put <локальный> <удалённый>
  local src=$1 dst=$2 sum
  sum=$(shasum -a 256 "$src" | cut -d' ' -f1)
  # gzip: канал до РФ рвёт передачу больше ~20 КБ, сжатый build.py — около 13 КБ
  run ssh "$HOST" "mkdir -p $(dirname "$dst") && gunzip -c > $dst.tmp" < <(gzip -9c "$src")
  run ssh "$HOST" "echo '$sum  $dst.tmp' | sha256sum -c --quiet && mv $dst.tmp $dst"
}

echo "▸ код → /opt/vebverf-blog"
for f in "${FILES[@]}"; do put "$f" "/opt/vebverf-blog/$f"; done
echo "▸ юниты → /etc/systemd/system"
for u in "${UNITS[@]}"; do put "$u" "/etc/systemd/system/$(basename "$u")"; done

echo "▸ robots.txt: строка Sitemap блога (с бэкапом)"
run ssh "$HOST" "grep -q 'blog/sitemap.xml' /var/www/verf/robots.txt || { cp /var/www/verf/robots.txt /var/www/verf/robots.txt.bak_blog_\$(date +%Y%m%d_%H%M) && printf '\nSitemap: https://xn--90abbkc0e2a8d.xn--p1ai/blog/sitemap.xml\n' >> /var/www/verf/robots.txt; }"

echo "▸ первая сборка и таймер"
run ssh "$HOST" "systemctl daemon-reload && python3 /opt/vebverf-blog/deploy/blog_sync.py --force && systemctl enable --now vebverf-blog-sync.timer"
run ssh "$HOST" "curl -sk -o /dev/null -w 'blog/ → %{http_code}\n' https://xn--90abbkc0e2a8d.xn--p1ai/blog/ && cat /opt/vebverf-blog/state.json"
[ "$GO" = "--go" ] || echo "Сухой прогон. Ставить: deploy/install.sh --go"
