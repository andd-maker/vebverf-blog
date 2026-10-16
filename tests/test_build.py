"""Сборщик блога: безопасность разметки, черновики и отложенные статьи, frontmatter от Pages CMS."""
import io
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deploy"))

import blog_sync  # noqa: E402
import build  # noqa: E402

POST = """---
title: "Приёмка \\"без бумаг\\""
slug: priemka-bez-bumag
date: 2026-10-10
draft: false
description: >-
  Длинный анонс, который js-yaml
  перенёс на две строки.
cover: /blog/media/a.jpg
---

Вступление с **жирным**, *курсивом* и [ссылкой](https://example.com).

## Раздел первый

- пункт один
- пункт два

## Раздел второй

| Что | Сколько |
|---|---|
| Фото | 8 |

## Раздел третий

> цитата

![схема](/blog/media/a.jpg "Подпись")
"""


class Markdown(unittest.TestCase):
    def test_html_is_escaped(self):
        html, _ = build.markdown('Текст <script>alert(1)</script> <img src=x onerror=alert(1)>')
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_dangerous_links_dropped(self):
        html, _ = build.markdown("[клик](javascript:alert(1)) и [data](data:text/html,x)")
        self.assertNotIn("href", html)
        self.assertIn("клик", html)

    def test_attribute_breakout(self):
        html, _ = build.markdown('[a](https://x.ru/"onmouseover="alert(1))')
        self.assertNotIn('"onmouseover', html)

    def test_images_only_from_media(self):
        html, _ = build.markdown("![ok](/blog/media/a.jpg)\n\n![bad](https://evil.ru/a.jpg)\n\n![up](/blog/media/../../x)")
        self.assertIn('src="/blog/media/a.jpg"', html)
        self.assertNotIn("evil", html)
        self.assertNotIn("../", html)

    def test_structure(self):
        meta, body = build.split_frontmatter(POST)
        html, toc = build.markdown(body)
        self.assertEqual(meta["title"], 'Приёмка "без бумаг"')
        self.assertEqual(meta["description"], "Длинный анонс, который js-yaml перенёс на две строки.")
        self.assertIs(meta["draft"], False)
        self.assertEqual(len(toc), 3)
        for tag in ("<strong>жирным</strong>", "<em>курсивом</em>", "<ul><li>пункт один</li>", "<table>",
                    "<blockquote>", "<figcaption>Подпись</figcaption>", 'target="_blank"'):
            self.assertIn(tag, html)

    def test_list_after_paragraph(self):
        html, _ = build.markdown("Список:\n- раз\n- два")
        self.assertIn("<ul>", html)

    def test_youtube_button(self):
        html, _ = build.markdown("Текст.\n\n[Тягач по трём цифрам](https://youtube.com/shorts/O5bGaUMIKrM?si=x)\n\nhttps://youtu.be/Qi2DueKz9Z4")
        self.assertIn('class="yt" href="https://www.youtube.com/shorts/O5bGaUMIKrM"', html)
        self.assertIn("Тягач по трём цифрам", html)
        self.assertIn("watch?v=Qi2DueKz9Z4", html)
        self.assertNotIn("<iframe", html)
        inline, _ = build.markdown("Смотрите https://youtube.com/shorts/O5bGaUMIKrM в тексте")
        self.assertNotIn('class="yt"', inline)          # ссылка внутри абзаца — не кнопка
        self.assertIsNone(build.youtube_url("https://evil.ru/shorts/O5bGaUMIKrM"))

    def test_slugify(self):
        self.assertEqual(build.slugify("Приёмка грузовика: фото!"), "priemka-gruzovika-foto")


class Yaml(unittest.TestCase):
    def test_temy_list(self):
        data = build.parse_yaml((ROOT / "content" / "temy.yml").read_text(encoding="utf-8"))
        self.assertGreater(len(data["temy"]), 5)
        self.assertIn("tema", data["temy"][0])
        self.assertEqual(data["temy"][0]["sdelano"], "")

    def test_literal_block(self):
        self.assertEqual(build.parse_yaml("a: |\n  x\n  y\nb: 1")["a"], "x\ny")


class Build(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "content" / "posts").mkdir(parents=True)
        (self.tmp / "content" / "media").mkdir()
        (self.tmp / "content" / "media" / "a.jpg").write_bytes(b"jpg")
        (self.tmp / "content" / "media" / "evil.html").write_text("<script>")

    def add(self, name, text):
        (self.tmp / "content" / "posts" / name).write_text(text, encoding="utf-8")

    def run_build(self, today=date(2026, 10, 16)):
        return build.build(self.tmp / "content", self.tmp / "out", today)

    def test_publish_rules(self):
        self.add("2026-10-10-a.md", POST)
        self.add("2026-10-11-draft.md", POST.replace("draft: false", "draft: true").replace("priemka-bez-bumag", "draft"))
        self.add("2026-10-20-future.md", POST.replace("2026-10-10", "2026-10-20").replace("priemka-bez-bumag", "future"))
        self.add("2026-10-12-broken.md", "---\nslug: x\n---\nбез заголовка")
        res = self.run_build()
        self.assertEqual((res["published"], res["drafts"], res["scheduled"]), (1, 1, 1))
        self.assertEqual(len(res["errors"]), 1)
        out = self.tmp / "out"
        self.assertTrue((out / "priemka-bez-bumag.html").exists())
        self.assertFalse((out / "draft.html").exists())
        self.assertFalse((out / "future.html").exists())
        self.assertTrue((out / "media" / "a.jpg").exists())
        self.assertFalse((out / "media" / "evil.html").exists())
        feed = json.loads((out / "posts.json").read_text())
        self.assertEqual([p["slug"] for p in feed], ["priemka-bez-bumag"])
        page = (out / "priemka-bez-bumag.html").read_text()
        self.assertIn("application/ld+json", page)
        self.assertIn("vv_consent", page)
        self.assertNotIn("fonts.googleapis", page)
        self.assertIn("priemka-bez-bumag.html", (out / "sitemap.xml").read_text())
        # отложенная выходит в свой день
        self.assertEqual(self.run_build(today=date(2026, 10, 20))["published"], 2)

    def test_duplicate_slug(self):
        self.add("2026-10-10-a.md", POST)
        self.add("2026-10-11-b.md", POST)
        res = self.run_build()
        self.assertEqual(res["published"], 1)
        self.assertIn("уже занят", res["errors"][0])

    def test_empty_blog(self):
        res = self.run_build()
        self.assertEqual(res["published"], 0)
        self.assertIn("скоро появятся", (self.tmp / "out" / "index.html").read_text())


class Sync(unittest.TestCase):
    def tar(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data, kind in members:
                ti = tarfile.TarInfo(name)
                if kind == "link":
                    ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                else:
                    ti.size = len(data)
                t.addfile(ti, io.BytesIO(data) if kind == "file" else None)
        return buf.getvalue()

    def test_extract_only_content(self):
        arc = self.tar([
            ("repo-abc/content/posts/a.md", b"x", "file"),
            ("repo-abc/content/media/l.jpg", b"", "link"),
            ("repo-abc/build.py", b"evil", "file"),
            ("repo-abc/content/../../etc/x", b"x", "file"),
        ])
        dest = Path(tempfile.mkdtemp())
        n = blog_sync.extract_content(arc, dest)
        self.assertEqual(n, 1)
        self.assertTrue((dest / "content" / "posts" / "a.md").exists())
        self.assertFalse((dest / "build.py").exists())
        self.assertFalse((dest / "content" / "media" / "l.jpg").exists())

    def test_swap(self):
        root = Path(tempfile.mkdtemp()) / "www" / "verf"
        live = root / "blog"
        live.mkdir(parents=True)
        (live / "old.txt").write_text("old")
        new = blog_sync.side(live, "new")
        new.mkdir()
        (new / "new.txt").write_text("new")
        self.assertNotIn(str(root), str(new))       # временный каталог вне корня сайта
        blog_sync.swap(new, live)
        self.assertTrue((live / "new.txt").exists())
        self.assertFalse((live / "old.txt").exists())
        self.assertFalse(new.exists())


if __name__ == "__main__":
    unittest.main()
