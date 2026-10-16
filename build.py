#!/usr/bin/env python3
"""Сборщик блога вебверфь.рф: content/ (Markdown от Pages CMS) → статические страницы /blog/.

Только стандартная библиотека: на проде (83.220.175.197) нет pip-пакета markdown, а тянуть
зависимости ради блога — лишняя движущаяся часть.

    python3 build.py                      # content/ → _site/blog/
    python3 build.py --content DIR --out DIR --today 2026-10-16
    python3 build.py --check              # только проверить статьи, ничего не писать

Правила:
- HTML в тексте статьи не пропускается, всё экранируется: статью правят из браузера, и тег
  <script> из CMS не должен попасть на сайт;
- ссылки — только http(s), mailto, tel и относительные; картинки — только из /blog/media/;
- черновики (draft: true) и статьи с датой в будущем не публикуются — дата работает как отложенная
  публикация, поэтому сервер пересобирает блог раз в сутки, даже если коммитов не было;
- posts.json — список опубликованного для анонсов в каналы (bot/blog_announce.py в vebverf-bot).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

SITE = "https://xn--90abbkc0e2a8d.xn--p1ai"      # вебверфь.рф в punycode — так ссылки не ломаются в RSS и мессенджерах
BLOG = "/blog"
MSK = timezone(timedelta(hours=3))
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
YM_ID = 110117064

ROOT = Path(__file__).resolve().parent


# ── frontmatter ─────────────────────────────────────────────────────
# Pages CMS пишет YAML через js-yaml: плоские ключи, строки в кавычках или без, длинные — блоком
# «>-»/«|-», списки «- ». Полный YAML не нужен, а PyYAML на проде нет.

def _scalar(v: str):
    v = v.strip()
    if v == "" or v in ("~", "null"):
        return None
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_yaml(text: str) -> dict:
    """Подмножество YAML: ключ: значение, блоки | и >, списки скаляров и списки плоских объектов."""
    lines = text.splitlines()
    out: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            raise ValueError(f"строка {i + 1}: не понял «{line[:60]}»")
        key, rest = m.group(1), m.group(2)
        i += 1
        if rest.strip() in ("|", "|-", ">", ">-", "|+", ">+"):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t"))):
                block.append(lines[i])
                i += 1
            ind = min((len(b) - len(b.lstrip()) for b in block if b.strip()), default=0)
            block = [b[ind:] for b in block]
            while block and not block[-1].strip():
                block.pop()
            if rest.strip().startswith(">"):
                paras, cur = [], []
                for b in block:
                    if b.strip():
                        cur.append(b.strip())
                    else:
                        paras.append(" ".join(cur))
                        cur = []
                paras.append(" ".join(cur))
                out[key] = "\n".join(paras)
            else:
                out[key] = "\n".join(block)
            continue
        if rest.strip() == "":
            items: list = []
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith((" ", "\t", "-"))):
                s = lines[i].strip()
                i += 1
                if not s:
                    continue
                if s.startswith("- "):
                    s = s[2:]
                    km = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", s)
                    if km and not s.startswith(('"', "'")):
                        items.append({km.group(1): _scalar(km.group(2))})
                    else:
                        items.append(_scalar(s))
                elif s == "-":
                    items.append({})
                else:
                    km = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", s)
                    if km and items and isinstance(items[-1], dict):
                        items[-1][km.group(1)] = _scalar(km.group(2))
            out[key] = items if items else None
            continue
        out[key] = _scalar(rest)
    return out


def split_frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)$", text, re.S)
    if not m:
        raise ValueError("frontmatter не закрыт строкой ---")
    return parse_yaml(m.group(1)), m.group(2)


# ── Markdown → HTML ──────────────────────────────────────────────────

_YT = re.compile(r"^https?://(?:www\.|m\.)?(?:youtube\.com/(shorts/|watch\?v=|live/)|youtu\.be/)([A-Za-z0-9_-]{6,20})(?:[?&#][^\s]*)?$")
_YT_LINE = re.compile(r"^\s*(?:\[([^\]]+)\]\((\S+?)\)|<?(\S+?)>?)\s*$")
YT_ICON = ('<svg viewBox="0 0 28 20" aria-hidden="true"><rect width="28" height="20" rx="5" fill="#e62117"/>'
           '<path d="M11 5.5v9l7.5-4.5z" fill="#fff"/></svg>')


def youtube_url(url: str) -> str | None:
    """Ссылка на ролик YouTube → канонический адрес (Shorts остаётся Shorts), иначе None."""
    m = _YT.match((url or "").strip())
    if not m:
        return None
    kind, vid = m.group(1) or "", m.group(2)
    return f"https://www.youtube.com/shorts/{vid}" if kind.startswith("shorts") else f"https://www.youtube.com/watch?v={vid}"


def youtube_button(url: str, label: str = "") -> str:
    """Кнопка-карточка вместо встроенного плеера: плеер тянет трекеры Google до согласия на cookies,
    а CSP сайта (frame-src) его всё равно не пустит."""
    short = "/shorts/" in url
    label = label or ("Ролик на YouTube" if not short else "Короткое видео на YouTube")
    return (f'<a class="yt" href="{escape(url)}" target="_blank" rel="noopener">{YT_ICON}'
            f'<span><b>{_emph(escape(label, quote=False))}</b><small>Смотреть на YouTube{" · Shorts" if short else ""}</small></span></a>')


def safe_url(url: str, image: bool = False) -> str | None:
    url = url.strip()
    if image:
        # CSP сайта пускает картинки только со своего домена; чужая ссылка всё равно не загрузится
        if url.startswith(f"{BLOG}/media/") or url.startswith("media/"):
            return url if ".." not in url else None
        return None
    low = url.lower()
    if low.startswith(("http://", "https://", "mailto:", "tel:", "/", "#")) or not re.match(r"^[a-z][a-z0-9+.-]*:", low):
        return url
    return None   # javascript:, data: и прочие схемы


_INLINE = re.compile(
    r"(?P<code>`+)(?P<ctext>.+?)(?P=code)"
    r"|!\[(?P<ialt>[^\]]*)\]\((?P<isrc>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\[(?P<ltext>[^\]]+)\]\((?P<lhref>[^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|<(?P<auto>https?://[^>\s]+)>"
)


def inline(text: str) -> str:
    """Строка Markdown → HTML. Сначала вынимаем код, ссылки и картинки, остальное экранируем."""
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(_emph(escape(text[pos:m.start()], quote=False)))
        if m.group("code"):
            out.append(f"<code>{escape(m.group('ctext').strip(), quote=False)}</code>")
        elif m.group("isrc") is not None:
            src = safe_url(m.group("isrc"), image=True)
            if src:
                out.append(f'<img src="{escape(src)}" alt="{escape(m.group("ialt"))}" loading="lazy">')
            else:
                out.append(escape(m.group("ialt")))
        elif m.group("lhref") is not None:
            href = safe_url(m.group("lhref"))
            label = _emph(escape(m.group("ltext"), quote=False))
            if href:
                ext = href.startswith(("http://", "https://")) and not href.startswith(SITE)
                rel = ' target="_blank" rel="noopener"' if ext else ""
                out.append(f'<a href="{escape(href)}"{rel}>{label}</a>')
            else:
                out.append(label)
        else:
            u = m.group("auto")
            out.append(f'<a href="{escape(u)}" target="_blank" rel="noopener">{escape(u, quote=False)}</a>')
        pos = m.end()
    out.append(_emph(escape(text[pos:], quote=False)))
    return "".join(out)


def _emph(s: str) -> str:
    s = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<strong>\1</strong>", s)
    s = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", s)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])", r"<em>\1</em>", s)
    s = s.replace("  \n", "<br>\n").replace("\\\n", "<br>\n")
    return s


_UL = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_OL = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+(.*)$")
_H = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_TROW = re.compile(r"^\s*\|.*\|\s*$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def markdown(md: str) -> tuple[str, list[dict]]:
    """Markdown → (HTML, оглавление). Заголовки статьи начинаются с h2: h1 — название страницы."""
    lines = md.replace("\r\n", "\n").split("\n")
    html: list[str] = []
    toc: list[dict] = []
    used: set[str] = set()
    para: list[str] = []
    i = 0

    def flush():
        if para:
            html.append(f"<p>{inline(chr(10).join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush()
            i += 1
            continue
        if _FENCE.match(line):
            flush()
            fence = _FENCE.match(line).group(1)
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1
            html.append(f"<pre><code>{escape(chr(10).join(code), quote=False)}</code></pre>")
            continue
        h = _H.match(line)
        if h:
            flush()
            level = min(max(len(h.group(1)), 2), 4)
            text = h.group(2)
            anchor = slugify(re.sub(r"[*_`\[\]]", "", text)) or f"r{len(toc) + 1}"
            base, n = anchor, 2
            while anchor in used:
                anchor, n = f"{base}-{n}", n + 1
            used.add(anchor)
            if level == 2:
                toc.append({"id": anchor, "text": re.sub(r"<[^>]+>", "", inline(text))})
            html.append(f'<h{level} id="{anchor}">{inline(text)}</h{level}>')
            i += 1
            continue
        if _HR.match(line):
            flush()
            html.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            flush()
            quote_lines = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner, _ = markdown("\n".join(quote_lines))
            html.append(f"<blockquote>{inner}</blockquote>")
            continue
        if _TROW.match(line) and i + 1 < len(lines) and _TSEP.match(lines[i + 1]):
            flush()
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and _TROW.match(lines[i]):
                rows.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            trs = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows)
            html.append(f'<div class="tbl"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>')
            continue
        lm = _UL.match(line) or _OL.match(line)
        if lm:
            flush()
            tag = "ul" if _UL.match(line) else "ol"
            rx = _UL if tag == "ul" else _OL
            items: list[str] = []
            while i < len(lines):
                cur = lines[i]
                im = rx.match(cur)
                if im:
                    items.append(im.group(1))
                elif cur.strip() and cur.startswith((" ", "\t")) and items:
                    items[-1] += "\n" + cur.strip()
                else:
                    break
                i += 1
            lis = "".join(f"<li>{inline(it)}</li>" for it in items)
            html.append(f"<{tag}>{lis}</{tag}>")
            continue
        ym = _YT_LINE.match(line)
        if ym and not para:
            yurl = youtube_url(ym.group(2) or ym.group(3) or "")
            if yurl:
                html.append(youtube_button(yurl, ym.group(1) or ""))
                i += 1
                continue
        im = re.fullmatch(r"\s*!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)\s*", line)
        if im:
            flush()
            src = safe_url(im.group(2), image=True)
            if src:
                cap = im.group(3) or ""
                fig = f'<img src="{escape(src)}" alt="{escape(im.group(1))}" loading="lazy">'
                if cap:
                    fig += f"<figcaption>{inline(cap)}</figcaption>"
                html.append(f"<figure>{fig}</figure>")
            i += 1
            continue
        para.append(line.rstrip() if not line.endswith("  ") else line)
        i += 1
    flush()
    return "\n".join(html), toc


# ── статьи ─────────────────────────────────────────────────────────

_TR = dict(zip("абвгдеёзийклмнопрстуфхыэ", "abvgdeezijklmnoprstufhye"))
_TR.update({"ж": "zh", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ь": "", "ю": "yu", "я": "ya"})


def slugify(s: str) -> str:
    s = "".join(_TR.get(ch, ch) for ch in s.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:80].strip("-")


@dataclass
class Post:
    slug: str
    title: str
    day: date
    description: str
    body_html: str
    toc: list = field(default_factory=list)
    cover: str | None = None
    cover_alt: str = ""
    draft: bool = False
    source: str = ""
    words: int = 0
    youtube: str | None = None
    youtube_title: str = ""

    @property
    def url(self) -> str:
        return f"{BLOG}/{self.slug}.html"

    @property
    def minutes(self) -> int:
        return max(1, round(self.words / 180))


def _to_date(v, src: str) -> date:
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        raise ValueError(f"{src}: нет даты (поле date, формат ГГГГ-ММ-ДД)")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def load_post(path: Path) -> Post:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    title = str(meta.get("title") or "").strip()
    if not title:
        raise ValueError(f"{path.name}: нет заголовка (поле title)")
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-?", "", path.stem)
    slug = slugify(str(meta.get("slug") or "") or stem or title) or slugify(title)
    if not slug:
        raise ValueError(f"{path.name}: не получился адрес статьи — заполни поле slug латиницей")
    day = _to_date(meta.get("date") or path.stem[:10], path.name)
    html, toc = markdown(body)
    plain = re.sub(r"<[^>]+>", " ", html)
    desc = str(meta.get("description") or "").strip()
    if not desc:
        first = re.search(r"<p>(.*?)</p>", html, re.S)
        desc = re.sub(r"<[^>]+>", "", first.group(1)) if first else title
        desc = desc[:200].rsplit(" ", 1)[0] + "…" if len(desc) > 200 else desc
    cover = meta.get("cover")
    cover = safe_url(str(cover), image=True) if cover else None
    return Post(slug=slug, title=title, day=day, description=desc, body_html=html, toc=toc,
                cover=cover, cover_alt=str(meta.get("cover_alt") or title), draft=bool(meta.get("draft")),
                source=path.name, words=len(plain.split()),
                youtube=youtube_url(str(meta.get("youtube") or "")),
                youtube_title=str(meta.get("youtube_title") or "").strip())


def load_posts(content: Path) -> tuple[list[Post], list[str]]:
    posts, errors, seen = [], [], {}
    for p in sorted((content / "posts").glob("*.md")):
        try:
            post = load_post(p)
        except (ValueError, UnicodeDecodeError) as e:
            errors.append(str(e))
            continue
        if post.slug in seen:
            errors.append(f"{p.name}: адрес {post.slug} уже занят статьёй {seen[post.slug]}")
            continue
        seen[post.slug] = p.name
        posts.append(post)
    posts.sort(key=lambda x: (x.day, x.slug), reverse=True)
    return posts, errors


# ── шаблоны ─────────────────────────────────────────────────────────

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября",
          "октября", "ноября", "декабря"]


def ru_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


CSS = """
:root{--bg:#f3f1ea;--pa:#fff;--ink:#1d2a36;--navy:#223240;--mut:#5d6b76;--dim:#8a949c;
--line:rgba(34,50,64,.16);--line2:rgba(34,50,64,.08);--ac:#d98a34;--ac2:#c2762a;
--plate-ink:#eef1f3;--plate-soft:#9fb0bd;--maxw:1240px}
*{box-sizing:border-box}
html{overflow-x:hidden;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,Arial,sans-serif;
font-weight:400;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit}
h1,h2,h3,h4{font-family:Manrope,sans-serif;font-weight:800;margin:0;line-height:1.12;letter-spacing:-.02em;text-wrap:balance}
.wrap{max-width:var(--maxw);margin:0 auto;padding:0 clamp(16px,3vw,32px)}
.top{padding:22px 0 26px}
.top .in{display:flex;align-items:center;justify-content:space-between;gap:20px}
.brand{display:flex;align-items:center;gap:12px;text-decoration:none}
.brand svg{height:26px;width:auto;display:block}
.brand b{font-family:Manrope;font-weight:800;font-size:17px;letter-spacing:-.02em;color:var(--navy)}
.brand i{font-style:normal;font-family:'IBM Plex Mono',monospace;font-size:11.5px;letter-spacing:.16em;
text-transform:uppercase;color:var(--ac2);margin-left:2px}
.back{font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.06em;color:var(--mut);text-decoration:none}
.back:hover{color:var(--ac)}
.kicker{font-family:'IBM Plex Mono',monospace;font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--ac2)}
.plate{background:var(--navy);color:var(--plate-ink);border-radius:6px;padding:clamp(24px,4vw,40px);position:relative;
box-shadow:inset 0 0 0 1px rgba(217,138,52,.18),0 14px 38px rgba(34,50,64,.16)}
.plate::after{content:"";position:absolute;inset:8px;border:1px dashed rgba(217,138,52,.24);border-radius:3px;pointer-events:none}
.plate .kicker{color:var(--ac)}
.plate h1{font-size:clamp(30px,5.4vw,56px);letter-spacing:-.035em;margin:16px 0 12px;max-width:22ch}
.plate p{margin:0;max-width:62ch;color:var(--plate-soft);font-size:clamp(16px,1.3vw,18px);font-weight:300}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:18px;font-family:'IBM Plex Mono',monospace;
font-size:12px;letter-spacing:.06em;color:var(--plate-soft)}
/* лента */
.feed{display:grid;gap:clamp(14px,2vw,22px);margin:clamp(26px,4vw,44px) 0;
grid-template-columns:repeat(auto-fill,minmax(min(340px,100%),1fr))}
.card{display:flex;flex-direction:column;background:var(--pa);border:1px solid var(--line);border-radius:8px;
overflow:hidden;text-decoration:none;transition:border-color .2s,transform .2s}
.card:hover{border-color:var(--ac);transform:translateY(-2px)}
.card .img{aspect-ratio:16/9;background:var(--navy);overflow:hidden}
.card .img img{width:100%;height:100%;object-fit:cover;display:block}
.card .img.empty{display:flex;align-items:flex-end;padding:18px;background:
linear-gradient(135deg,var(--navy),#2d4152)}
.card .img.empty span{font-family:Manrope;font-weight:800;font-size:clamp(20px,2vw,26px);color:var(--plate-ink);
line-height:1.15;letter-spacing:-.02em}
.card .tx{padding:18px 20px 20px;display:flex;flex-direction:column;gap:9px;flex:1}
.card h2{font-size:clamp(19px,1.6vw,22px)}
.card p{margin:0;color:var(--mut);font-size:15px;line-height:1.5}
.card .d{margin-top:auto;padding-top:6px;font-family:'IBM Plex Mono',monospace;font-size:11.5px;letter-spacing:.08em;color:var(--dim)}
.card.lead{grid-column:1/-1;display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr)}
.card.lead .img{aspect-ratio:auto;min-height:280px}
.card.lead h2{font-size:clamp(24px,2.6vw,34px)}
.empty-feed{background:var(--pa);border:1px dashed var(--line);border-radius:8px;padding:28px;color:var(--mut)}
/* статья */
.post{display:grid;gap:clamp(24px,4vw,64px);margin:clamp(26px,4vw,44px) 0;
grid-template-columns:minmax(0,1fr) minmax(220px,300px);align-items:start}
.body{background:var(--pa);border:1px solid var(--line);border-radius:8px;padding:clamp(20px,4vw,52px);
font-size:clamp(16.5px,1.15vw,18px);min-width:0}
.body>*:first-child{margin-top:0}
.body p,.body ul,.body ol,.body blockquote,.body figure,.body pre,.body .tbl{margin:0 0 1.1em}
.body h2{font-size:clamp(23px,2.2vw,30px);margin:1.7em 0 .6em;scroll-margin-top:20px}
.body h3{font-size:clamp(19px,1.6vw,22px);margin:1.5em 0 .5em}
.body h4{font-size:17px;margin:1.3em 0 .4em}
.body a{color:var(--ac2);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}
.body ul,.body ol{padding-left:1.3em}
.body li{margin:.35em 0}
.body li::marker{color:var(--ac)}
.body blockquote{border-left:3px solid var(--ac);padding:4px 0 4px 18px;color:var(--mut)}
.body blockquote p:last-child{margin:0}
.body figure img,.body p img{max-width:100%;height:auto;border-radius:6px;display:block}
.body figcaption{font-size:14px;color:var(--dim);margin-top:8px}
.body code{font-family:'JetBrains Mono',monospace;font-size:.88em;background:var(--line2);padding:.1em .35em;border-radius:3px}
.body pre{background:var(--navy);color:var(--plate-ink);padding:16px 18px;border-radius:6px;overflow-x:auto}
.body pre code{background:none;padding:0;font-size:14px}
.body hr{border:0;border-top:1px solid var(--line);margin:2em 0}
.body .tbl{overflow-x:auto}
.body table{border-collapse:collapse;width:100%;font-size:.94em}
.body th,.body td{border:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}
.body th{background:var(--line2);font-family:Manrope;font-weight:700}
.yt{display:flex;align-items:center;gap:14px;max-width:440px;margin:0 0 1.1em;padding:12px 16px;border:1px solid var(--line);
border-radius:10px;background:var(--pa);color:var(--ink)!important;text-decoration:none!important;transition:border-color .2s,transform .2s}
.yt:hover{border-color:#e62117;transform:translateY(-1px)}
.yt svg{width:34px;height:24px;flex:none}
.yt span{display:flex;flex-direction:column;gap:2px;min-width:0}
.yt b{font-family:Manrope;font-weight:700;font-size:15.5px;line-height:1.3}
.yt small{font-family:'IBM Plex Mono',monospace;font-size:11.5px;letter-spacing:.04em;color:var(--dim)}
.plate .yt{margin:18px 0 0;background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.16);color:var(--plate-ink)!important}
.plate .yt small{color:var(--plate-soft)}
.cover{margin:0 0 clamp(18px,3vw,30px);border-radius:6px;overflow:hidden}
.cover img{width:100%;height:auto;display:block}
.side{position:sticky;top:20px;display:grid;gap:16px}
.box{background:var(--pa);border:1px solid var(--line);border-radius:8px;padding:18px 20px}
.box h3{font-size:15px;margin:8px 0 10px}
.box ol{margin:0;padding-left:1.2em;font-size:14.5px;color:var(--mut)}
.box ol li{margin:.4em 0}
.box ol a{text-decoration:none}.box ol a:hover{color:var(--ac2)}
.box p{margin:0 0 12px;font-size:14.5px;color:var(--mut)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:11px 16px;border-radius:6px;
font-family:Manrope;font-weight:700;font-size:14.5px;text-decoration:none;border:1px solid var(--navy);color:var(--navy)}
.btn.solid{background:var(--ac);border-color:var(--ac);color:#1a1206}
.btns{display:flex;flex-wrap:wrap;gap:8px}
.more{margin:0 0 clamp(30px,5vw,60px)}
.more h2{font-size:clamp(21px,2vw,26px);border-top:2px solid var(--navy);padding-top:14px}
.more .feed{margin-top:18px}
.foot{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px 24px;border-top:1px solid var(--line);
padding:22px 0 40px;font-size:13.5px;color:var(--mut)}
.foot a{color:var(--mut)}
@media (max-width:900px){.post{grid-template-columns:1fr}.side{position:static}.card.lead{grid-template-columns:1fr}
.card.lead .img{aspect-ratio:16/9;min-height:0}}
@media (max-width:560px){.top .back{display:none}.body{border-left:0;border-right:0;border-radius:0;margin:0 calc(-1*clamp(16px,3vw,32px))}}
.vvck{position:fixed;left:12px;right:12px;bottom:12px;z-index:99;display:flex;gap:14px;align-items:center;flex-wrap:wrap;
background:#223240;color:#e9e5db;border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:12px 16px;
font-family:Inter,system-ui,sans-serif;font-size:13.5px;line-height:1.45;box-shadow:0 12px 40px rgba(0,0,0,.35)}
.vvck p{margin:0;flex:1 1 320px}.vvck a{color:#eaa44f}
.vvck .b{display:flex;gap:8px;flex:0 0 auto}
.vvck button{font:inherit;cursor:pointer;border-radius:4px;padding:8px 14px;border:1px solid rgba(255,255,255,.25);background:transparent;color:#e9e5db}
.vvck button.ok{background:#d98a34;border-color:#d98a34;color:#1a1206;font-weight:600}
.vvck[hidden]{display:none}
@media(max-width:560px){.vvck{left:8px;right:8px;padding:12px}.vvck .b{width:100%}.vvck button{flex:1}}
"""

LOGO = ('<svg viewBox="-12 -12 144 97" fill="none" aria-hidden="true">'
        '<polygon points="10.8,0 109.2,0 120,47 0,47" fill="#223240"/>'
        '<rect x="0" y="56" width="120" height="5" rx="2.5" fill="#d98a34"/>'
        '<rect x="16" y="70" width="88" height="3" rx="1.5" fill="#223240" opacity="0.35"/></svg>')

# Метрика — как на site.html: только после «Принять» (152-ФЗ, 04.09.2026), согласие общее для сайта
# (localStorage vv_consent на одном домене), webvisor выключен.
CONSENT = """<div class="vvck" id="vvck" hidden role="dialog" aria-label="Cookies">
<p>Сайт использует cookies и Яндекс.Метрику для статистики посещений. До вашего согласия счётчик не загружается. Подробнее — в <a href="/legal.html#cookies">политике конфиденциальности</a>.</p>
<div class="b"><button type="button" class="ok" id="vvckYes">Принять</button><button type="button" id="vvckNo">Отклонить</button></div>
</div>
<script>
(function(){
var YM=%d;
function load(){if(window._ymLoaded)return;window._ymLoaded=true;
(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};m[i].l=1*new Date();
k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})
(window,document,"script","https://mc.yandex.ru/metrika/tag.js","ym");
ym(YM,"init",{clickmap:true,trackLinks:true,accurateTrackBounce:true,webvisor:false});}
function get(){try{var c=JSON.parse(localStorage.getItem('vv_consent')||'null');
if(c&&c.v&&c.ts&&(Date.now()-c.ts)<365*864e5)return c.v;}catch(e){}return null;}
var v=get();if(v==='yes'){load();return;}if(v!==null)return;
var el=document.getElementById('vvck');el.hidden=false;
function set(x){try{localStorage.setItem('vv_consent',JSON.stringify({v:x,ts:Date.now()}));}catch(e){}el.hidden=true;if(x==='yes')load();}
document.getElementById('vvckYes').onclick=function(){set('yes');};
document.getElementById('vvckNo').onclick=function(){set('no');};
})();
</script>""" % YM_ID


def page(title: str, description: str, canonical: str, body: str, og_image: str | None = None,
         og_type: str = "website", extra_head: str = "") -> str:
    img = f'<meta property="og:image" content="{escape(SITE + og_image)}">\n' if og_image else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description)}">
<link rel="canonical" href="{escape(SITE + canonical)}">
<link rel="alternate" type="application/rss+xml" title="Блог ВебВерфи" href="{BLOG}/rss.xml">
<link rel="icon" href="/logo.png">
<meta property="og:type" content="{og_type}">
<meta property="og:site_name" content="ВебВерфь">
<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}">
<meta property="og:url" content="{escape(SITE + canonical)}">
{img}<link rel="stylesheet" href="/fonts/fonts.css">
<style>{CSS}</style>
{extra_head}</head>
<body>
<div class="wrap top"><div class="in">
<a class="brand" href="{BLOG}/">{LOGO}<span><b>ВебВерфь</b> <i>блог</i></span></a>
<a class="back" href="/site.html">← на сайт</a>
</div></div>
<div class="wrap">
{body}
<div class="foot"><span>© {date.today().year} ВебВерфь · цифровые инструменты для грузового сервиса и малого дела</span>
<span><a href="{BLOG}/rss.xml">RSS</a> · <a href="/letopis.html">Летопись верфи</a> · <a href="/legal.html">Правовая информация</a></span></div>
</div>
{CONSENT}
</body>
</html>
"""


def card(p: Post, lead: bool = False) -> str:
    if p.cover:
        img = f'<div class="img"><img src="{escape(p.cover)}" alt="{escape(p.cover_alt)}" loading="lazy"></div>'
    else:
        img = f'<div class="img empty"><span>{escape(p.title)}</span></div>' if lead else ""
    return (f'<a class="card{" lead" if lead else ""}" href="{escape(p.url)}">{img}<div class="tx">'
            f'<h2>{escape(p.title)}</h2><p>{escape(p.description)}</p>'
            f'<span class="d">{ru_date(p.day)} · {p.minutes} мин</span></div></a>')


ORDER_BOX = """<div class="box"><span class="kicker">ВебВерфь</span>
<h3>Нужен бот или приложение под ваше дело?</h3>
<p>Разбор задачи — бесплатно, смета за день. Бот — от 7 дней, готовый продукт — за 14.</p>
<div class="btns"><a class="btn solid" href="https://max.ru/se13308383_bot?start=blog" target="_blank" rel="noopener">Написать в MAX</a>
<a class="btn" href="https://t.me/vebverf_bot?start=blog" target="_blank" rel="noopener">Telegram</a></div></div>"""


def render_post(p: Post, others: list[Post]) -> str:
    cover = (f'<figure class="cover"><img src="{escape(p.cover)}" alt="{escape(p.cover_alt)}"></figure>'
             if p.cover else "")
    toc = ""
    if len(p.toc) >= 3:
        lis = "".join(f'<li><a href="#{t["id"]}">{t["text"]}</a></li>' for t in p.toc)
        toc = f'<div class="box"><span class="kicker">В статье</span><ol>{lis}</ol></div>'
    more = ""
    if others:
        more = ('<section class="more"><h2>Ещё в блоге</h2><div class="feed">'
                + "".join(card(o) for o in others[:3]) + "</div></section>")
    ld = {
        "@context": "https://schema.org", "@type": "BlogPosting", "headline": p.title,
        "description": p.description, "datePublished": p.day.isoformat(),
        "mainEntityOfPage": SITE + p.url, "inLanguage": "ru",
        "author": {"@type": "Organization", "name": "ВебВерфь", "url": SITE + "/"},
        "publisher": {"@type": "Organization", "name": "ВебВерфь",
                      "logo": {"@type": "ImageObject", "url": SITE + "/logo.png"}},
    }
    if p.cover:
        ld["image"] = SITE + p.cover
    ld_json = json.dumps(ld, ensure_ascii=False).replace("</", "<\\/")
    yt = youtube_button(p.youtube, p.youtube_title) if p.youtube else ""
    body = f"""<header class="plate"><span class="kicker">Блог · {ru_date(p.day)}</span>
<h1>{escape(p.title)}</h1><p>{escape(p.description)}</p>
<div class="meta"><span>{p.minutes} мин чтения</span><a href="{BLOG}/">← все статьи</a></div>{yt}</header>
<div class="post"><article class="body">{cover}{p.body_html}</article>
<aside class="side">{toc}{ORDER_BOX}</aside></div>
{more}"""
    return page(f"{p.title} — блог ВебВерфи", p.description, p.url, body, og_image=p.cover, og_type="article",
                extra_head=f'<script type="application/ld+json">{ld_json}</script>\n')


def render_index(posts: list[Post]) -> str:
    if posts:
        cards = card(posts[0], lead=True) + "".join(card(p) for p in posts[1:])
        feed = f'<div class="feed">{cards}</div>'
    else:
        feed = '<div class="feed"><div class="empty-feed">Первые статьи скоро появятся.</div></div>'
    body = f"""<header class="plate"><span class="kicker">Блог ВебВерфи</span>
<h1>Цифровые инструменты для грузового сервиса и малого дела</h1>
<p>Как приёмка, заказ-наряды, боты и мини-приложения экономят часы мастерам и руководителям — на живых примерах верфи.</p>
</header>
{feed}"""
    return page("Блог ВебВерфи — боты и приложения для грузового сервиса и малого дела",
                "Статьи ВебВерфи: приёмка грузовиков, заказ-наряды, боты в MAX и Telegram, мини-приложения для малого бизнеса.",
                f"{BLOG}/", body)


def render_rss(posts: list[Post]) -> str:
    items = []
    for p in posts[:30]:
        dt = datetime(p.day.year, p.day.month, p.day.day, 7, 0, tzinfo=MSK)
        items.append(f"""<item><title>{escape(p.title)}</title><link>{escape(SITE + p.url)}</link>
<guid isPermaLink="true">{escape(SITE + p.url)}</guid><pubDate>{format_datetime(dt)}</pubDate>
<description>{escape(p.description)}</description></item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Блог ВебВерфи</title><link>{SITE}{BLOG}/</link>
<description>Боты и приложения для грузового сервиса и малого дела</description><language>ru</language>
{chr(10).join(items)}
</channel></rss>
"""


def render_sitemap(posts: list[Post]) -> str:
    urls = [f"<url><loc>{SITE}{BLOG}/</loc></url>"]
    urls += [f"<url><loc>{escape(SITE + p.url)}</loc><lastmod>{p.day.isoformat()}</lastmod></url>" for p in posts]
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(urls) + "\n</urlset>\n")


# ── сборка ──────────────────────────────────────────────────────────

def build(content: Path, out: Path, today: date) -> dict:
    """Собрать блог в out (каталог создаётся заново). Вернуть итог для лога."""
    posts, errors = load_posts(content)
    live = [p for p in posts if not p.draft and p.day <= today]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for i, p in enumerate(live):
        others = live[:i] + live[i + 1:]
        (out / f"{p.slug}.html").write_text(render_post(p, others), encoding="utf-8")
    (out / "index.html").write_text(render_index(live), encoding="utf-8")
    (out / "rss.xml").write_text(render_rss(live), encoding="utf-8")
    (out / "sitemap.xml").write_text(render_sitemap(live), encoding="utf-8")
    media_src = content / "media"
    copied = 0
    if media_src.is_dir():
        for f in media_src.rglob("*"):
            if f.is_file() and not f.is_symlink() and f.suffix.lower() in MEDIA_EXT:
                dst = out / "media" / f.relative_to(media_src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(f, dst)
                copied += 1
    feed = [{"slug": p.slug, "title": p.title, "description": p.description, "date": p.day.isoformat(),
             "url": SITE + p.url, "cover": p.cover, "youtube": p.youtube} for p in live]
    (out / "posts.json").write_text(json.dumps(feed, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"published": len(live), "drafts": sum(p.draft for p in posts),
            "scheduled": sum(1 for p in posts if not p.draft and p.day > today), "media": copied, "errors": errors}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--content", type=Path, default=ROOT / "content")
    ap.add_argument("--out", type=Path, default=ROOT / "_site" / "blog")
    ap.add_argument("--today", type=date.fromisoformat, default=datetime.now(MSK).date())
    ap.add_argument("--check", action="store_true", help="только проверить статьи")
    a = ap.parse_args(argv)
    if a.check:
        posts, errors = load_posts(a.content)
        for e in errors:
            print("✗", e)
        print(f"статей {len(posts)}, ошибок {len(errors)}")
        return 1 if errors else 0
    res = build(a.content, a.out, a.today)
    for e in res["errors"]:
        print("✗", e, file=sys.stderr)
    print(f"опубликовано {res['published']}, черновиков {res['drafts']}, отложено {res['scheduled']}, "
          f"картинок {res['media']}, ошибок {len(res['errors'])} → {a.out}")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
