"""
① Parsing — تبدیل فایل‌های MDX مستندات لیارا به متن ساختاریافته.

این فایل‌ها مارک‌داون نیستند؛ صفحات React هستند. پس پارسر مارک‌داون کار نمی‌کند
و باید JSX را دست بگیریم. خوشبختانه ساختار مستندات کاملاً یکدست است و همیشه
همان چند کامپوننت ثابت استفاده می‌شود.

آنچه از هر فایل بیرون می‌آید:
    ParsedDoc
      ├── page_title      از  # H1
      ├── description     از  <meta og:description>
      └── segments[]      هر کدام یک بخش قابل بازیابی
            ├── anchor    از  <Section id="...">      → لینک مستقیم
            ├── title     از  <Section title="...">
            ├── method    از  <Tabs>                   → Console/CLI/Github
            └── body      متن تمیز + بلوک‌های کد

تست:
    python -m ingest.parse                    آمار کل ۱۱۴۲ فایل
    python -m ingest.parse <path/to/file.mdx> خروجی کامل یک فایل
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ingest.config import (
    DOCS_PAGES,
    MAX_CODE_BLOCK_CHARS,
    path_to_breadcrumb,
    path_to_service,
    path_to_url,
)

# جانگهدار بلوک کد. NUL در متن مستندات وجود ندارد، پس برخوردی پیش نمی‌آید.
CODE_OPEN, CODE_CLOSE = "\x00C", "\x00"

ALERT_LABELS = {
    "success": "نکته",
    "info": "توجه",
    "warning": "هشدار",
    "error": "اخطار",
}


# --------------------------------------------------------------- مدل

@dataclass
class Segment:
    """یک بخش قابل بازیابی از صفحه."""
    title: str = ""
    anchor: str | None = None
    variant: str | None = None   # برچسب <Tabs> — فریم‌ورک، زبان، روش، سیستم‌عامل، …
    body: str = ""

    @property
    def has_code(self) -> bool:
        return "```" in self.body


@dataclass
class ParsedDoc:
    source_path: str
    page_title: str = ""
    description: str = ""
    service: str = ""
    breadcrumb: str = ""
    url: str = ""
    segments: list[Segment] = field(default_factory=list)


# --------------------------------------------------------------- ابزار

def _match_bracket(text: str, i: int, opener: str, closer: str) -> int:
    """
    i روی opener است. اندیس بعد از closer متناظر را برمی‌گرداند، یا -1.
    آگاه به رشته نیست؛ چون بلوک‌های کد از قبل بیرون کشیده شده‌اند، مشکلی نمی‌سازد.
    """
    depth = 0
    while i < len(text):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _split_fragments(s: str) -> list[str]:
    """بدنه‌ی یک آرایه JSX را به فرگمنت‌های سطح‌بالای <>...</> می‌شکند."""
    frags: list[str] = []
    i = 0
    while True:
        start = s.find("<>", i)
        if start == -1:
            return frags
        depth, k, found = 0, start, False
        while k < len(s):
            if s.startswith("</>", k):
                depth -= 1
                k += 3
                if depth == 0:
                    frags.append(s[start + 2:k - 3])
                    found = True
                    break
            elif s.startswith("<>", k):
                depth += 1
                k += 2
            else:
                k += 1
        if not found:
            return frags
        i = k


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'{name}\s*=\s*["\']([^"\']*)["\']', tag)
    return m.group(1) if m else None


# --------------------------------------------------------------- مراحل

def _strip_imports(text: str) -> str:
    """
    حذف خطوط import.

    ⚠️ importهای چندخطی هم باید بروند:
        import { GoContainer,
                 GoDatabase, ... } from "react-icons/go";
    اگر فقط تک‌خطی‌ها را برداریم، دنباله‌ی اسم آیکون‌ها به‌عنوان «محتوا» در
    ایندکس می‌نشیند و صفحه را با متنی بی‌ربط به‌عنوان منبع معرفی می‌کند.
    """
    text = re.sub(
        r'^\s*import\s+\{[^}]*\}\s*from\s*["\'][^"\']*["\'];?\s*$',
        "",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return re.sub(r"^\s*import\s+.*$", "", text, flags=re.MULTILINE)


def _strip_comments(text: str) -> str:
    """
    حذف {/* ... */}.

    ⚠️ حیاتی: بعضی صفحات بلوک‌های کامنت‌شده‌ی کپی‌پیست‌مانده از صفحات دیگر
    دارند (مثلاً ai/quick-start.mdx بخش بکاپ دیتابیس را دارد). اگر اینها بمانند،
    ربات در پاسخِ یک موضوع، مطالب موضوع دیگری را تحویل می‌دهد.
    """
    return re.sub(r"\{\s*/\*.*?\*/\s*\}", "", text, flags=re.DOTALL)


def _extract_head(text: str) -> tuple[str, str]:
    """عنوان و توضیح را از <Head> بیرون می‌کشد و بلوک را حذف می‌کند."""
    title = ""
    desc = ""
    m = re.search(r"<Head>(.*?)</Head>", text, flags=re.DOTALL)
    if m:
        head = m.group(1)
        t = re.search(r"<title>(.*?)</title>", head, flags=re.DOTALL)
        if t:
            title = t.group(1).strip()
        d = re.search(
            r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']*)["\']',
            head,
        )
        if d:
            desc = d.group(1).strip()
    return title, desc


def _strip_base64(text: str) -> str:
    """
    کوتاه کردن رشته‌های Base64.

    مستندات AI SDK نمونه‌ی data URL دارند که یک PNG کامل به‌صورت Base64 است —
    یک بخش ۲۷۰ کیلوبایتی تولید می‌کرد. این‌ها هیچ سؤالی را جواب نمی‌دهند، فقط
    هزینه‌ی امبدینگ می‌سازند و در صورت بازیابی، کل context مدل را می‌بلعند.
    """
    text = re.sub(
        r"(data:[\w/+.-]+;base64,)[A-Za-z0-9+/=\s]{80,}",
        r"\1…",
        text,
    )
    return re.sub(r"[A-Za-z0-9+/]{300,}={0,2}", "…", text)


def _cap_code(code: str) -> str:
    """بریدن بلوک‌های کد بیش از حد بلند، با حفظ ابتدای مفیدشان."""
    if len(code) <= MAX_CODE_BLOCK_CHARS:
        return code
    cut = code[:MAX_CODE_BLOCK_CHARS]
    cut = cut[:cut.rfind("\n")] if "\n" in cut else cut
    return cut + "\n// … ادامه‌ی نمونه کد در مستندات"


def _extract_code(text: str) -> tuple[str, list[str]]:
    """
    <Highlight className="json">{`...`}</Highlight> → بلوک کد فنس‌دار.

    بلوک‌ها با جانگهدار جایگزین می‌شوند تا بقیه‌ی قواعد پاک‌سازی رویشان اجرا نشود.
    """
    blocks: list[str] = []

    def repl(m: re.Match) -> str:
        lang = _attr(m.group(1), "className") or ""
        code = _cap_code(_strip_base64(m.group(2).strip("\n")))
        blocks.append(f"```{lang}\n{code}\n```")
        return f"{CODE_OPEN}{len(blocks) - 1}{CODE_CLOSE}"

    text = re.sub(
        r"<Highlight\b([^>]*)>\s*\{`(.*?)`\}\s*</Highlight>",
        repl,
        text,
        flags=re.DOTALL,
    )
    return text, blocks


# کلیدهایی که در آرایه‌های .map محتوا حمل می‌کنند (نه چیدمان)
_CONTENT_KEYS = re.compile(
    r"\b(?:platform|title|text|label|name|question|heading)\s*:\s*['\"]([^'\"]+)['\"]"
)


def _flatten_map_expression(block: str) -> str:
    """
    داده‌ی معنادار را از یک گرید {[...].map(...)} بیرون می‌کشد.

    ⚠️ اول این بلوک‌ها را کامل حذف می‌کردم با این استدلال که «لیست همچنین
    بخوانید» هستند. غلط بود: فهرست کامل ۱۶ ارائه‌دهنده‌ی هوش مصنوعی لیارا
    داخل همین ساختار است. نتیجه‌اش این شد که ربات OpenAI/GPT را جا انداخت
    و بعد در پاسخ به «چت جی‌پی‌تی داره؟» گفت در مستندات نیست.

    حالا مقادیر متنی نگه داشته می‌شوند و فقط قالب JSX دور ریخته می‌شود.
    """
    values = _CONTENT_KEYS.findall(block)
    if not values:
        return ""
    # یکتا با حفظ ترتیب
    seen: list[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return "\n" + "\n".join(f"- {v}" for v in seen) + "\n"


def _enclosing_brace(text: str, pos: int) -> tuple[int, int]:
    """
    بیرونی‌ترین { که موقعیت pos داخلش است.

    ⚠️ اینجا اول rfind("{") زدم که غلط بود: نزدیک‌ترین { قبل از .map همان
    آبجکت آخرِ داخل آرایه است، نه {[ بیرونی. نتیجه‌اش این شد که از فهرست
    ۱۶تایی مدل‌ها فقط چند مورد آخر بیرون می‌آمد.

    درستش این است که به عقب برویم تا براکتی که واقعاً pos را در بر بگیرد.
    """
    i = pos
    while i >= 0:
        i = text.rfind("{", 0, i)
        if i == -1:
            return -1, -1
        end = _match_bracket(text, i, "{", "}")
        if end > pos:
            return i, end
    return -1, -1


def _remove_map_expressions(text: str) -> str:
    """گریدهای {[...].map(...)} را به فهرست ساده‌ی متنی تبدیل می‌کند."""
    guard = 0
    while guard < 50:
        guard += 1
        m = re.search(r"\.map\s*\(", text)
        if not m:
            return text

        start, end = _enclosing_brace(text, m.start())
        if start == -1:
            # براکت دربرگیرنده پیدا نشد؛ فقط خود .map را حذف کن تا حلقه
            # بی‌پایان نشود
            return text[:m.start()] + text[m.end():]

        text = text[:start] + _flatten_map_expression(text[start:end]) + text[end:]
    return text


def _expand_steps(text: str) -> str:
    """<Step steps={[{step:"۱", content:(<>...</>)}, ...]}/> → لیست شماره‌دار."""
    while True:
        m = re.search(r"<Step\b[^>]*?steps\s*=\s*", text, flags=re.DOTALL)
        if not m:
            return text
        arr_start = text.find("{", m.end() - 1)
        if arr_start == -1:
            return text
        arr_end = _match_bracket(text, arr_start, "{", "}")
        if arr_end == -1:
            return text
        body = text[arr_start:arr_end]

        # پایان تگ <Step ... /> بعد از آرایه
        tag_end = text.find(">", arr_end)
        tag_end = tag_end + 1 if tag_end != -1 else arr_end

        steps = re.findall(r'step\s*:\s*["\']([^"\']*)["\']', body)
        frags = _split_fragments(body)

        out = []
        for i, frag in enumerate(frags):
            label = steps[i] if i < len(steps) else str(i + 1)
            out.append(f"\nمرحله {label}:\n{frag}\n")

        text = text[:m.start()] + "\n".join(out) + text[tag_end:]


def _tab_labels(s: str) -> list[str]:
    """
    برچسب تب‌ها را بیرون می‌کشد. دو شکل در مستندات دیده شده:
        tabs={["Liara Console", "Liara CLI"]}
        tabs={[{ label: "PHP", icon: <Icon/> }, ...]}
    """
    labels = re.findall(r'label\s*:\s*["\']([^"\']+)["\']', s)
    return labels or re.findall(r'["\']([^"\']+)["\']', s)


def _split_by_tabs(text: str) -> list[tuple[str | None, str]]:
    """
    <Tabs tabs={[...]} content={[<>A</>, <>B</>, <>C</>]}/>
    → [(None, قبل), ("Liara Console", A), ("Liara CLI", B), ("Github", C), (None, بعد)]

    چرا جدا نگه داشته می‌شوند: محتوای تب‌ها تقریباً یکسان است. اگر ادغام شوند،
    هر پنج جای top-k با تکرار یک مطلب پر می‌شود. جدا بودنشان هم بازیابی را تمیز
    می‌کند و هم پایه‌ی سؤال تکمیلی «با کدام روش/زبان/فریم‌ورک؟» را می‌دهد.

    تب‌ها تودرتو می‌شوند (مثلاً OpenAI SDK ← Python)، پس بازگشتی است و
    برچسب‌ها با › به هم می‌چسبند.
    """
    m = re.search(r"<Tabs\b", text)
    if not m:
        return [(None, text)]

    labels_m = re.search(r"tabs\s*=\s*\{", text[m.start():])
    content_m = re.search(r"content\s*=\s*\{", text[m.start():])
    if not labels_m or not content_m:
        return [(None, text)]

    lb = m.start() + labels_m.end() - 1
    le = _match_bracket(text, lb, "{", "}")
    cb = m.start() + content_m.end() - 1
    ce = _match_bracket(text, cb, "{", "}")
    if le == -1 or ce == -1:
        return [(None, text)]

    labels = _tab_labels(text[lb:le])
    frags = _split_fragments(text[cb:ce])

    tag_end = text.find(">", max(le, ce))
    tag_end = tag_end + 1 if tag_end != -1 else ce

    out: list[tuple[str | None, str]] = []
    before = text[:m.start()].strip()
    if before:
        out.append((None, before))

    for i, frag in enumerate(frags):
        label = labels[i] if i < len(labels) else None
        # بازگشت به داخل فرگمنت، چون ممکن است خودش Tabs داشته باشد
        for sub_label, sub_text in _split_by_tabs(frag):
            combined = " › ".join(x for x in (label, sub_label) if x)
            out.append((combined or None, sub_text))

    after = text[tag_end:].strip()
    if after:
        out.extend(_split_by_tabs(after))
    return out


def _clean(text: str) -> str:
    """تبدیل JSX باقی‌مانده به متن خوانا."""
    text = _strip_base64(text)   # اگر خارج از بلوک کد هم آمده باشد
    # <Alert variant="warning">...</Alert>  →  "هشدار: ..."
    def alert(m: re.Match) -> str:
        variant = _attr(m.group(1), "variant") or "info"
        return f"\n{ALERT_LABELS.get(variant, 'توجه')}: {m.group(2)}\n"

    text = re.sub(r"<Alert\b([^>]*)>(.*?)</Alert>", alert, text, flags=re.DOTALL)

    # <Important> برای نام فایل و دستور استفاده می‌شود → کد درون‌خطی
    text = re.sub(r"<Important>(.*?)</Important>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(
        r"<h(\d)>(.*?)</h\1>",
        lambda m: f"\n### {' '.join(m.group(2).split())}\n",
        text,
        flags=re.DOTALL,
    )

    # عناصر بی‌محتوا
    text = re.sub(r"<video\b.*?</video>", "", text, flags=re.DOTALL)
    text = re.sub(r"<(img|br|hr)\b[^>]*/?>", "\n", text)
    text = re.sub(r"<div\b[^>]*/>", "", text)

    # هر تگ باقی‌مانده حذف، متن داخلش می‌ماند
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)

    # عبارت‌های JSX ساده که ممکن است مانده باشند
    text = re.sub(r"\{\s*[\"'](.*?)[\"']\s*\}", r"\1", text)

    # فاصله‌ها
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _restore_code(text: str, blocks: list[str]) -> str:
    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        return "\n" + blocks[idx] + "\n" if idx < len(blocks) else ""

    return re.sub(rf"{CODE_OPEN}(\d+){CODE_CLOSE}", repl, text)


def _split_sections(text: str) -> list[tuple[str | None, str, str]]:
    """
    برش روی <Section id="x" title="y" />.
    → [(anchor, title, body), ...]  اولین تکه anchor ندارد (مقدمه صفحه).
    """
    tags = list(re.finditer(r"<Section\b[^>]*/>", text))
    if not tags:
        return [(None, "", text)]

    out: list[tuple[str | None, str, str]] = []
    intro = text[:tags[0].start()].strip()
    if intro:
        out.append((None, "", intro))

    for i, tag in enumerate(tags):
        end = tags[i + 1].start() if i + 1 < len(tags) else len(text)
        raw = tag.group(0)
        out.append((_attr(raw, "id"), _attr(raw, "title") or "", text[tag.end():end].strip()))
    return out


# --------------------------------------------------------------- ورودی اصلی

def parse_file(path: Path) -> ParsedDoc:
    raw = path.read_text(encoding="utf-8", errors="replace")

    raw = _strip_imports(raw)
    raw = _strip_comments(raw)          # قبل از هر چیز دیگر
    meta_title, description = _extract_head(raw)
    raw = re.sub(r"<Head>.*?</Head>", "", raw, flags=re.DOTALL)

    h1 = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
    page_title = h1.group(1).strip() if h1 else (meta_title or path.stem)
    if h1:
        raw = raw[:h1.start()] + raw[h1.end():]

    raw, code_blocks = _extract_code(raw)

    doc = ParsedDoc(
        source_path=str(path).replace("\\", "/"),
        page_title=page_title,
        description=description,
        service=path_to_service(path),
        breadcrumb=path_to_breadcrumb(path),
        url=path_to_url(path),
    )

    for variant, chunk_text in _split_by_tabs(raw):
        chunk_text = _remove_map_expressions(chunk_text)
        chunk_text = _expand_steps(chunk_text)

        for anchor, title, body in _split_sections(chunk_text):
            body = _restore_code(_clean(body), code_blocks)
            if len(body.strip()) < 40:      # بخش‌های تهی رد می‌شوند
                continue
            doc.segments.append(
                Segment(title=title, anchor=anchor, variant=variant, body=body)
            )

    return doc


def iter_docs():
    for p in sorted(DOCS_PAGES.rglob("*.mdx")):
        yield parse_file(p)


# --------------------------------------------------------------- دیباگ

def _dump(doc: ParsedDoc) -> None:
    print("=" * 70)
    print(f"فایل      : {doc.source_path}")
    print(f"عنوان     : {doc.page_title}")
    print(f"توضیح     : {doc.description}")
    print(f"سرویس     : {doc.service}")
    print(f"مسیر      : {doc.breadcrumb}")
    print(f"URL       : {doc.url}")
    print(f"بخش‌ها    : {len(doc.segments)}")
    for s in doc.segments:
        print("-" * 70)
        print(f"[واریانت: {s.variant or '—'}] [anchor: {s.anchor or '—'}] {s.title}")
        print(f"({len(s.body)} کاراکتر، کد: {'بله' if s.has_code else 'خیر'})")
        print(s.body[:700] + ("…" if len(s.body) > 700 else ""))


def _stats() -> None:
    files = 0
    segs = 0
    empty: list[str] = []
    chars = 0
    with_code = 0
    variants: dict[str, int] = {}
    services: dict[str, int] = {}
    sizes: list[int] = []

    for doc in iter_docs():
        files += 1
        if not doc.segments:
            empty.append(doc.source_path)
        for s in doc.segments:
            segs += 1
            chars += len(s.body)
            sizes.append(len(s.body))
            with_code += s.has_code
            variants[s.variant or "—"] = variants.get(s.variant or "—", 0) + 1
        services[doc.service] = services.get(doc.service, 0) + 1

    sizes.sort()
    def pct(p: int) -> int:
        return sizes[min(len(sizes) - 1, p * len(sizes) // 100)] if sizes else 0

    print(f"فایل‌ها          : {files}")
    print(f"بخش‌ها           : {segs}")
    print(f"میانگین طول بخش  : {chars // max(segs, 1)} کاراکتر")
    print(f"بخش‌های دارای کد : {with_code}  ({with_code * 100 // max(segs, 1)}٪)")
    print(f"\nتوزیع طول بخش‌ها:")
    print(f"  کمینه {sizes[0] if sizes else 0} | ۲۵٪ {pct(25)} | میانه {pct(50)} "
          f"| ۷۵٪ {pct(75)} | ۹۵٪ {pct(95)} | بیشینه {sizes[-1] if sizes else 0}")
    print(f"  زیر ۲۵۰ کاراکتر : {sum(1 for s in sizes if s < 250)}")
    print(f"  بالای ۳۵۰۰      : {sum(1 for s in sizes if s > 3500)}")
    print(f"\nتفکیک واریانت (Tabs):")
    for k, v in sorted(variants.items(), key=lambda x: -x[1])[:12]:
        print(f"  {k:<20} {v}")
    print(f"  … مجموعاً {len(variants)} واریانت مختلف")
    print(f"\nتفکیک سرویس:")
    for k, v in sorted(services.items(), key=lambda x: -x[1]):
        print(f"  {k:<24} {v}")
    print(f"\n⚠️ فایل‌های بدون هیچ بخش: {len(empty)}")
    for p in empty[:15]:
        print(f"  {p}")


def _top(n: int = 15) -> None:
    """بزرگ‌ترین بخش‌ها — برای شکار باگ پارسر."""
    rows = []
    for doc in iter_docs():
        for s in doc.segments:
            rows.append((len(s.body), doc.source_path, s.variant, s.title, s.body))
    rows.sort(reverse=True, key=lambda r: r[0])

    for size, path, variant, title, body in rows[:n]:
        print(f"{size:>8}  [{variant or '—'}] {title or '(بدون عنوان)'}")
        print(f"          {path}")
    if rows:
        print("\n" + "=" * 70)
        print("نمونه از ابتدای بزرگ‌ترین بخش:\n")
        print(rows[0][4][:1500])


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--top":
        _top()
    elif arg:
        _dump(parse_file(Path(arg)))
    else:
        _stats()
