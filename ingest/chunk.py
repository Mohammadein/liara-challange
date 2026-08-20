"""
② Chunking — تبدیل بخش‌های پارس‌شده به تکه‌های نهایی قابل بازیابی.

کار سنگینی نیست، چون <Section> مرزهای معنایی را از قبل داده. اینجا فقط:
  ۱. تکه‌های بزرگ‌تر از سقف روی مرز پاراگراف/بلوک کد شکسته می‌شوند
  ۲. سرآیند زمینه به متنِ امبدینگ اضافه می‌شود
  ۳. شناسه و URL نهایی ساخته می‌شود

تست:
    python -m ingest.chunk           آمار
    python -m ingest.chunk --sample  نمونه تکه‌های واقعی
"""

from __future__ import annotations

import re
import sys

from ingest.config import MAX_CHUNK_CHARS, TARGET_CHUNK_CHARS, _relative_slug
from ingest.parse import ParsedDoc, Segment, iter_docs
from ingest.schema import Chunk

_FENCE = re.compile(r"(```.*?```)", re.DOTALL)


# --------------------------------------------------------------- شکستن

def _atoms(text: str) -> list[str]:
    """
    متن را به واحدهای اتمی می‌شکند: پاراگراف‌ها و بلوک‌های کد.
    یک بلوک کد هرگز از وسط شکسته نمی‌شود — نمونه کد نصفه بدتر از نبودنش است.
    """
    out: list[str] = []
    for part in _FENCE.split(text):
        if not part.strip():
            continue
        if part.lstrip().startswith("```"):
            out.append(part.strip())
        else:
            for para in re.split(r"\n{2,}", part):
                if para.strip():
                    out.append(para.strip())
    return out


def _hard_split(s: str, limit: int) -> list[str]:
    """آخرین چاره برای واحدی که خودش از سقف بزرگ‌تر است: برش روی خط."""
    if len(s) <= limit:
        return [s]
    parts, buf = [], ""
    for line in s.split("\n"):
        if buf and len(buf) + len(line) + 1 > limit:
            parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts


def _pack(text: str) -> list[str]:
    """واحدها را حریصانه تا رسیدن به اندازه هدف کنار هم می‌چیند."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    parts: list[str] = []
    buf = ""
    for atom in _atoms(text):
        for piece in _hard_split(atom, MAX_CHUNK_CHARS):
            if buf and len(buf) + len(piece) + 2 > TARGET_CHUNK_CHARS:
                parts.append(buf)
                buf = piece
            else:
                buf = f"{buf}\n\n{piece}" if buf else piece
    if buf.strip():
        parts.append(buf)
    return parts


# --------------------------------------------------------------- ساخت تکه

def _embed_text(doc: ParsedDoc, seg: Segment, body: str) -> str:
    """
    متنی که وکتور می‌شود — با سرآیند زمینه.

    بدون این، تکه‌ای مثل «سپس مقدار را روی true بگذارید» هیچ سؤالی را جواب
    نمی‌دهد چون وکتورش به هیچ چیز نزدیک نیست. با سرآیند، همان تکه با
    «تنظیمات جنگو در لیارا» فاصله‌ی کمی پیدا می‌کند.
    """
    head = [doc.breadcrumb, doc.page_title]
    if seg.title:
        head.append(seg.title)
    if seg.variant:
        head.append(f"روش: {seg.variant}")
    return "\n".join(x for x in head if x) + "\n---\n" + body


def _variant_key(variant: str | None) -> str:
    """برچسب واریانت را به قطعه‌ای امن برای شناسه تبدیل می‌کند."""
    if not variant:
        return "-"
    return re.sub(r"[^\w.-]+", "_", variant.replace(" › ", "_")).strip("_") or "-"


def chunk_doc(doc: ParsedDoc) -> list[Chunk]:
    slug = _relative_slug(doc.source_path)
    out: list[Chunk] = []

    for i, seg in enumerate(doc.segments):
        # شماره بخش همیشه در شناسه می‌آید: یک anchor ممکن است در چند تب تکرار
        # شود و بدون آن، تکه‌ها روی هم می‌افتند.
        anchor_key = f"{seg.anchor}-{i}" if seg.anchor else f"seg{i}"
        url = f"{doc.url}#{seg.anchor}" if seg.anchor else doc.url

        for part, body in enumerate(_pack(seg.body)):
            # تکه‌های عملاً خالی وارد ایندکس نمی‌شوند
            if len(body.strip()) < 60:
                continue
            out.append(Chunk(
                id=f"{slug}#{anchor_key}#{_variant_key(seg.variant)}#{part}",
                text=body,
                embed_text=_embed_text(doc, seg, body),
                url=url,
                page_title=doc.page_title,
                section_title=seg.title,
                breadcrumb=doc.breadcrumb,
                service=doc.service,
                variant=seg.variant,
                has_code="```" in body,
                source_path=doc.source_path,
            ))
    return out


def build_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in iter_docs():
        chunks.extend(chunk_doc(doc))
    return chunks


# --------------------------------------------------------------- دیباگ

def _stats() -> None:
    chunks = build_chunks()
    sizes = sorted(len(c.text) for c in chunks)
    embed_sizes = sorted(len(c.embed_text) for c in chunks)

    def pct(v: list[int], p: int) -> int:
        return v[min(len(v) - 1, p * len(v) // 100)] if v else 0

    ids = [c.id for c in chunks]
    total_embed_chars = sum(embed_sizes)

    print(f"تکه‌ها             : {len(chunks)}")
    print(f"شناسه‌های تکراری   : {len(ids) - len(set(ids))}")
    print(f"دارای کد           : {sum(c.has_code for c in chunks)}")
    print(f"دارای anchor       : {sum('#' in c.url for c in chunks)}")
    print(f"دارای واریانت      : {sum(c.variant is not None for c in chunks)}")
    print(f"\nطول متن تکه‌ها:")
    print(f"  کمینه {sizes[0]} | میانه {pct(sizes, 50)} | ۹۵٪ {pct(sizes, 95)} "
          f"| بیشینه {sizes[-1]}")
    print(f"  بالای سقف {MAX_CHUNK_CHARS}: {sum(1 for s in sizes if s > MAX_CHUNK_CHARS)}")
    print(f"\nهزینه امبدینگ (تخمینی):")
    print(f"  کاراکتر  : {total_embed_chars:,}")
    print(f"  توکن ≈   : {total_embed_chars // 3:,}")
    print(f"\nحجم فایل‌های خروجی (تخمینی):")
    print(f"  chunks.json ≈ {sum(sizes) // 1024 // 1024 + 2} MB")
    print(f"  vectors.npy ≈ {len(chunks) * 1024 * 4 // 1024 // 1024} MB  (۱۰۲۴ بعد)")


def _sample() -> None:
    chunks = build_chunks()
    picks = [
        next((c for c in chunks if c.variant and c.has_code and c.section_title), None),
        next((c for c in chunks if len(c.text) < 300), None),
        max(chunks, key=lambda c: len(c.text)),
    ]
    for c in picks:
        if not c:
            continue
        print("=" * 70)
        print(f"id       : {c.id}")
        print(f"url      : {c.url}")
        print(f"صفحه     : {c.page_title}")
        print(f"بخش      : {c.section_title or '—'}")
        print(f"واریانت  : {c.variant or '—'}")
        print(f"طول      : {len(c.text)}")
        print("-" * 70)
        print("متنِ امبدینگ (اول ۴۰۰ کاراکتر):")
        print(c.embed_text[:400])
        print("-" * 70)
        print("متنِ ارسالی به مدل (اول ۴۰۰ کاراکتر):")
        print(c.text[:400])
        print()


if __name__ == "__main__":
    if "--sample" in sys.argv:
        _sample()
    else:
        _stats()
