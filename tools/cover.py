#!/usr/bin/env python3
"""Обложка статьи блога в фирменном стиле — для страницы, соцсетей и анонсов в каналы.

Зачем (Денис, 15.11.2026): у статей не было картинок, анонсы в Telegram и MAX уходили
голым текстом и в ленте терялись. Обложка рисуется локально: тёмный фон бренда, логотип,
надзаголовок, крупный заголовок и адрес сайта. Никакой генерации картинок моделью — только
шрифты и вёрстка, поэтому результат предсказуемый и бесплатный.

    python3 tools/cover.py --all              # всем статьям без обложки
    python3 tools/cover.py --slug <slug>      # одной
    python3 tools/cover.py --slug <slug> --force   # перерисовать поверх

Пишет PNG в content/media/cover-<slug>.png и прописывает в статью cover и cover_alt.
Нужен Pillow; на Mac и в GitHub Actions ставится одной строкой.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTS = ROOT / "content" / "posts"
MEDIA = ROOT / "content" / "media"
FONTS = ROOT / "assets" / "fonts"
LOGO = ROOT / "assets" / "logo.png"

W, H = 1200, 630
NAVY = (34, 50, 64)
INK = (255, 255, 255)
MUTED = (150, 166, 181)
ORANGE = (214, 136, 52)
PAD = 72


def font(name: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONTS / name), size)


def wrap(draw, text: str, fnt, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        probe = (cur + " " + w).strip()
        if draw.textlength(probe, font=fnt) <= width or not cur:
            cur = probe
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_title(draw, title: str, width: int, max_lines: int, max_height: int):
    """Кегль подбираем так, чтобы заголовок вошёл ЦЕЛИКОМ: без обрезки и многоточия."""
    for size in range(62, 23, -2):
        f = font("Unbounded-Bold.ttf", size)
        lines = wrap(draw, title, f, width)
        line_h = int(size * 1.26)
        if len(lines) <= max_lines and line_h * len(lines) <= max_height:
            return f, lines, line_h
    f = font("Unbounded-Bold.ttf", 24)
    return f, wrap(draw, title, f, width), int(24 * 1.26)


def letter_spaced(draw, xy, text: str, fnt, fill, spacing: int = 4) -> None:
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw.textlength(ch, font=fnt) + spacing


def render(title: str, tema: str, day: str):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (W, H), NAVY)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 8], fill=ORANGE)                    # фирменная полоса сверху

    logo = Image.open(LOGO).convert("RGBA").resize((104, 104), Image.LANCZOS)
    im.paste(logo, (PAD - 6, PAD - 10), logo)                 # круг совпадает с фоном — виден только знак

    kicker = font("Manrope-Bold.ttf", 21)
    letter_spaced(d, (PAD + 118, PAD + 18), "БЛОГ ВЕБВЕРФИ", kicker, ORANGE, spacing=5)
    if tema:
        d.text((PAD + 118, PAD + 50), tema.upper()[:48], font=font("Manrope-Regular.ttf", 18), fill=MUTED)

    top = PAD + 150                                            # ниже логотипа и надзаголовка
    bottom = H - PAD - 54                                      # выше подписи сайта
    fnt, lines, line_h = fit_title(d, title, W - PAD * 2, max_lines=4, max_height=bottom - top)
    y = top
    for line in lines:
        d.text((PAD, y), line, font=fnt, fill=INK)
        y += line_h

    d.text((PAD, H - PAD + 4), "вебверфь.рф", font=font("Manrope-Regular.ttf", 23), fill=MUTED)
    if day:
        f = font("Manrope-Regular.ttf", 19)
        d.text((W - PAD - d.textlength(day, font=f), H - PAD + 8), day, font=f, fill=MUTED)
    return im


def read_meta(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    head = text.split("\n---", 1)[0]
    meta = {}
    for key in ("title", "slug", "date", "tema", "cover"):
        m = re.search(rf'^{key}:\s*"?(.*?)"?\s*$', head, re.M)
        if m:
            meta[key] = m.group(1).strip()
    return meta


def set_cover(path: Path, cover: str, title: str) -> None:
    text = path.read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n")
    cover_line = f'cover: "{cover}"'
    alt_line = f'cover_alt: "{title.replace(chr(34), chr(39))[:200]}"'
    if re.search(r"^cover:.*$", head, re.M):
        head = re.sub(r"^cover:.*$", cover_line, head, count=1, flags=re.M)
    else:
        head = head.replace("\n---", f"\n---", 1) if False else head
        head += "\n" + cover_line
    if re.search(r"^cover_alt:.*$", head, re.M):
        head = re.sub(r"^cover_alt:.*$", alt_line, head, count=1, flags=re.M)
    path.write_text(head + sep + body, encoding="utf-8")


def build_one(path: Path, force: bool = False) -> str | None:
    meta = read_meta(path)
    title = meta.get("title", "").strip()
    slug = meta.get("slug", "").strip() or path.stem
    if not title:
        return None
    out = MEDIA / f"cover-{slug}.png"
    if out.exists() and not force:
        return None
    MEDIA.mkdir(parents=True, exist_ok=True)
    day = meta.get("date", "")[-10:]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        day = f"{day[8:10]}.{day[5:7]}.{day[0:4]}"
    im = render(title.strip('"'), meta.get("tema", "").strip('"'), day)
    im.save(out, "PNG", optimize=True)
    set_cover(path, f"/blog/media/{out.name}", title.strip('"'))
    return out.relative_to(ROOT).as_posix()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    made = 0
    for p in sorted(POSTS.glob("*.md")):
        meta = read_meta(p)
        if args.slug and meta.get("slug") != args.slug:
            continue
        out = build_one(p, force=args.force)
        if out:
            made += 1
            print("обложка:", out)
    if not made:
        print("обложек не сделано — либо нет статей, либо у всех уже есть (--force перерисует)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
