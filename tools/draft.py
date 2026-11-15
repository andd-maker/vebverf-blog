#!/usr/bin/env python3
"""Черновик статьи блога ВебВерфи через DeepSeek API.

Было: `claude -p` по подписке Claude Code (CLAUDE_CODE_OAUTH_TOKEN). 8 ноября 2026 организация
отключила подписку — генератор встал. Теперь модель зовём напрямую по ключу DEEPSEEK_API_KEY,
без установки npm-пакета и без подписки.

Два шага, как и раньше по смыслу:
  1) исследование — поиск в вебе с ссылками на источники (deepseek_web_search);
  2) письмо — статья по заданию prompts/draft.md и найденным фактам, без инструментов.

Черновик создаётся с draft: true — публикует человек (Аня). Скрипт не публикует ничего сам.
Только stdlib: на GitHub-раннере ничего ставить не нужно.

Запуск:  DEEPSEEK_API_KEY=… python3 tools/draft.py        (TEMA=… — тема из кнопки)
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
MSK = timezone(timedelta(hours=3))
POSTS = ROOT / "content" / "posts"
TEMY = ROOT / "content" / "temy.yml"
MAX_CHARS = 7000

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def key() -> str:
    k = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not k:
        raise SystemExit("нет DEEPSEEK_API_KEY — генератор выключен (секрет репозитория)")
    return k


def call(messages: list[dict], *, tools: bool = False, max_tokens: int = 6000,
         temperature: float = 0.8, timeout: int = 600) -> str:
    body = {"model": MODEL, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": False,
            "thinking": {"type": "disabled"}}
    if tools:                       # серверный поиск и чтение ссылок: клиент ничего не исполняет
        body["tools"] = [{"type": "deepseek_web_search"}, {"type": "deepseek_read_url"}]
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": f"Bearer {key()}", "Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"DeepSeek ответил {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SystemExit(f"DeepSeek недоступен: {e}")
    out = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if not out.strip():
        raise SystemExit("DeepSeek вернул пустой ответ")
    return out


def slugify(title: str) -> str:
    words = re.findall(r"[а-яёa-z0-9]+", title.lower())
    lat = ["".join(TRANSLIT.get(ch, ch) for ch in w) for w in words]
    lat = [w for w in lat if w]
    return "-".join(lat[:6])[:60].strip("-") or "statya"


def topics() -> list[dict]:
    """Разбор content/temy.yml без PyYAML: блоки по четыре пробела, ключи простые."""
    items: list[dict] = []
    cur: dict | None = None
    for i, line in enumerate(TEMY.read_text(encoding="utf-8").splitlines()):
        m = re.match(r"^  - (\w+):\s*(.*)$", line)
        if m:
            if cur:
                items.append(cur)
            cur = {m.group(1): m.group(2), "_start": i}
            continue
        m = re.match(r"^    (\w+):\s*(.*)$", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2)
            if m.group(1) == "sdelano":
                cur["_line"] = i
    if cur:
        items.append(cur)
    for it in items:
        for k, v in list(it.items()):
            if isinstance(v, str):
                it[k] = v.strip().strip('"').strip()
    return items


def mark_done(item: dict, day: str) -> None:
    """Проставить sdelano у выбранной темы — иначе она будет выбираться каждый день."""
    lines = TEMY.read_text(encoding="utf-8").splitlines()
    idx = item.get("_line")
    if idx is None:                     # поля не было — вставляем после primenimost/utverzhdeno
        idx = item.get("_start", 0) + 4
        lines.insert(idx, f'    sdelano: "{day}"')
    else:
        lines[idx] = f'    sdelano: "{day}"'
    TEMY.write_text("\n".join(lines) + "\n", encoding="utf-8")


def recent_titles() -> list[str]:
    out = []
    for p in sorted(POSTS.glob("*.md"), reverse=True)[:40]:
        head = p.read_text(encoding="utf-8").split("\n---", 1)[0]
        m = re.search(r'^title:\s*"(.*)"$', head, re.M)
        if m:
            out.append(m.group(1))
    return out


def style_sample() -> str:
    """Одна свежая непустая статья как образец голоса — не больше 4000 знаков."""
    for p in sorted(POSTS.glob("*.md"), reverse=True):
        text = p.read_text(encoding="utf-8")
        if "generated: true" in text and len(text) > 2000:
            return text[:4000]
    return ""


def research(topic: str, note: str) -> str:
    system = ("Ты исследователь. Ищешь свежие проверяемые факты и цифры для статьи на русском. "
              "Данные не старше двух лет, у каждого факта — ссылка на источник. Ничего не выдумывай: "
              "не нашёл — так и напиши.")
    user = (f"Тема статьи: {topic}\nПодсказка редактора: {note or '—'}\n\n"
            "Найди в интернете 5–8 конкретных фактов по теме: цифры, нормы, статистика, реальные практики. "
            "Формат каждой строки: «факт — источник: URL — год». Без вступлений и выводов. "
            "Если тема про процессы сервиса и цифр по ней нет — верни 3–5 практических наблюдений с ссылками.")
    return call([{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=True, max_tokens=1600, timeout=420)


def write_article(topic: str, note: str, primenimost: str, day: str, facts: str) -> str:
    brief = (ROOT / "prompts" / "draft.md").read_text(encoding="utf-8")
    fakty = (ROOT / "prompts" / "fakty.md").read_text(encoding="utf-8")
    system = (brief + "\n\n## Факты о нас (только отсюда)\n" + fakty[:6000] +
              "\n\n## Как выглядит готовая статья нашего блога\n" + style_sample())
    user = (f"Сегодня {day} (МСК).\nТема: {topic}\nПодсказка редактора: {note or '—'}\n"
            f"Поле primenimost: {primenimost or 'net'}\n\n"
            "Уже написанные статьи (не повторяй):\n- " + "\n- ".join(recent_titles()[:25]) + "\n\n"
            "Проверенные факты из поиска (используй только их для цифр, с теми же ссылками):\n" + facts[:5000] +
            "\n\nВерни ТОЛЬКО содержимое файла статьи: front matter по формату и текст в Markdown. "
            "Без ```-блоков, без пояснений до и после.")
    return call([{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=8000, temperature=0.75, timeout=600)


def clean(raw: str) -> str:
    t = raw.strip()
    t = re.sub(r"^```(?:markdown|md)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    i = t.find("---")
    if i > 0:
        t = t[i:]
    return t


def save(text: str, day: str) -> Path:
    if not text.startswith("---"):
        raise SystemExit("модель вернула текст без front matter")
    head, _, body = text.partition("\n---\n")
    if not body.strip():
        raise SystemExit("модель вернула статью без текста")
    if re.search(r"^draft:\s*true", head, re.M) is None:
        head += "\ndraft: true"
    if re.search(r"^generated:\s*true", head, re.M) is None:
        head += "\ngenerated: true"
    if body.lstrip().startswith("# "):
        body = body.split("\n", 1)[1]                     # H1 не нужен: заголовок из title
    title = ""
    m = re.search(r'^title:\s*"(.*)"$', head, re.M)
    if m:
        title = m.group(1)
    m = re.search(r"^slug:\s*([a-z0-9-]+)\s*$", head, re.M)      # slug из front matter главнее
    slug = m.group(1) if m else (slugify(title) if title else "statya")
    path = POSTS / f"{day}-{slug}.md"
    n = 1
    while path.exists():                                   # вторую статью в тот же день — с суффиксом
        n += 1
        path = POSTS / f"{day}-{slug}-{n}.md"
    path.write_text(head + "\n---\n" + body.lstrip("\n"), encoding="utf-8")
    return path


def main() -> int:
    day = datetime.now(MSK).strftime("%Y-%m-%d")
    env_topic = " ".join((os.environ.get("TEMA") or "").split())[:300]
    item = None
    if env_topic:
        topic, note, primenimost = env_topic, "", "net"
    else:
        for it in topics():
            if it.get("utverzhdeno") == "true" and not it.get("sdelano"):
                item = it
                break
        if not item:
            log("нет темы")
            return 0
        topic, note, primenimost = item.get("tema", ""), item.get("zametka", ""), item.get("primenimost", "net")
    log(f"тема: {topic[:90]}")
    facts = research(topic, note)
    log(f"фактов найдено: {len(facts.splitlines())} строк")
    text = clean(write_article(topic, note, primenimost or "net", day, facts))
    path = save(text, day)
    if item is not None:
        mark_done(item, day)
    log(str(path.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
