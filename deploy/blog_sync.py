#!/usr/bin/env python3
"""Синхронизация блога на проде: GitHub main → /var/www/verf/blog. Таймер раз в 5 минут.

    python3 /opt/vebverf-blog/deploy/blog_sync.py            # обычный прогон
    python3 /opt/vebverf-blog/deploy/blog_sync.py --force    # пересобрать без проверки коммита

Почему так, а не git pull: git на РФ-сервере нет (решение ВебПриёмки №14), а GitHub отсюда виден по HTTPS.

- Из архива берётся ТОЛЬКО content/ (статьи и картинки). Сборщик — свой, установленный при деплое
  (/opt/vebverf-blog/build.py): правка build.py в репозитории не исполняется на сервере сама —
  иначе любой, кто может закоммитить, запускал бы код от root.
- Пересборка — при новом коммите или раз в сутки (дата статьи работает как отложенная публикация).
- Подмена атомарная: собираем в blog.new, переименованием ставим на место, старую версию удаляем.
- Статья с ошибкой пропускается, остальные публикуются; сбой скачивания — сайт остаётся как был.
- Стоп-кран: файл /opt/vebverf-blog/STOP — прогон ничего не делает.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = os.environ.get("BLOG_REPO", "andd-maker/vebverf-blog")
BRANCH = os.environ.get("BLOG_BRANCH", "main")
HOME = Path(os.environ.get("BLOG_HOME", "/opt/vebverf-blog"))
WWW = Path(os.environ.get("BLOG_WWW", "/var/www/verf/blog"))
STATE = HOME / "state.json"
STOP = HOME / "STOP"
MSK = timezone(timedelta(hours=3))
MAX_ARCHIVE = 200 * 1024 * 1024
MAX_FILE = 25 * 1024 * 1024

log = logging.getLogger("blog_sync")


def http_get(url: str, accept: str | None = None, limit: int = MAX_ARCHIVE) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "vebverf-blog-sync"})
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"{url}: ответ больше {limit} байт")
    return data


def head_sha() -> str:
    sha = http_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                   accept="application/vnd.github.sha", limit=100).decode().strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        raise RuntimeError(f"странный ответ вместо sha: {sha[:60]!r}")
    return sha


def extract_content(archive: bytes, dest: Path) -> int:
    """Достать из tar.gz только <корень>/content/**: обычные файлы, без ссылок и выхода за пределы."""
    n = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = Path(m.name).parts
            if len(parts) < 3 or parts[1] != "content" or not m.isfile():
                continue
            rel = Path(*parts[2:])
            if rel.is_absolute() or ".." in rel.parts or m.size > MAX_FILE:
                log.warning("пропускаю %s", m.name)
                continue
            target = dest / "content" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    return n


def load_builder():
    spec = importlib.util.spec_from_file_location("blog_build", HOME / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def side(live: Path, tag: str) -> Path:
    """Временный каталог рядом с корнем сайта, а не внутри: /var/www/.verf-blog.new не раздаётся nginx,
    но лежит на той же файловой системе — переименование остаётся атомарным."""
    return live.parent.parent / f".{live.parent.name}-{live.name}.{tag}"


def swap(new: Path, live: Path) -> None:
    old = side(live, f"old_{int(time.time())}")
    if live.exists():
        live.rename(old)
    try:
        new.rename(live)
    except OSError:
        if old.exists():
            old.rename(live)   # вернуть как было
        raise
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def write_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(STATE)


def run(force: bool = False) -> int:
    if STOP.exists():
        log.info("стоп-кран %s — ничего не делаю", STOP)
        return 0
    st = read_state()
    today = datetime.now(MSK).date()
    sha = head_sha()
    if not force and st.get("sha") == sha and st.get("day") == today.isoformat() and WWW.exists():
        return 0
    archive = http_get(f"https://codeload.github.com/{REPO}/tar.gz/{sha}")
    builder = load_builder()
    with tempfile.TemporaryDirectory(prefix="blog-") as tmp:
        files = extract_content(archive, Path(tmp))
        new = side(WWW, "new")
        if new.exists():
            shutil.rmtree(new)
        res = builder.build(Path(tmp) / "content", new, today)
    for e in res["errors"]:
        log.error("статья пропущена: %s", e)
    swap(new, WWW)
    st.update({"sha": sha, "day": today.isoformat(), "ts": datetime.now(MSK).isoformat(timespec="seconds"),
               "published": res["published"], "drafts": res["drafts"], "scheduled": res["scheduled"],
               "errors": res["errors"], "files": files})
    write_state(st)
    log.info("блог собран: коммит %s, опубликовано %d, черновиков %d, отложено %d, ошибок %d",
             sha[:7], res["published"], res["drafts"], res["scheduled"], len(res["errors"]))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return run(force=a.force)
    except Exception as e:   # сеть, GitHub, архив — сайт остаётся как был, таймер повторит через 5 минут
        log.error("синхронизация не прошла: %r", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
