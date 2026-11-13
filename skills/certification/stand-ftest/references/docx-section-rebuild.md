# Пересборка раздела .docx-отчёта: код и порядок

Проверено на отчёте с пользовательскими стилями (`!Заг. 1/2/3`, `Н-4`,
`Caption`). Все функции работают через `python-docx` + `lxml` из него.

**Собирать итог заново из оригинала, а не патчить предыдущую копию.** Подмены
рисунков и пересборка разделов укладываются в один проход «оригинал → итог»;
чинить накопленные правки XML дороже, а лишние media-части всё равно остаются
в пакете.

## 1. Поиск границ блока

Стиль ищется по id, а не по имени: в `w:pStyle/@w:val` лежит id, имя живёт в
`doc.styles` (и содержит пробел: `!Заг. 3`).

```python
from docx.oxml.ns import qn

def ptext(el):
    return "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()

def para_style_id(el):
    ppr = el.find(qn("w:pPr"))
    st = ppr.find(qn("w:pStyle")) if ppr is not None else None
    return st.get(qn("w:val")) if st is not None else None

def find_heading(doc, needle, style_name):
    """Заголовок раздела: текст + стиль (в оглавлении тот же текст, но стиль toc*)."""
    ids = {s.style_id for s in doc.styles if (s.name or "") == style_name}
    for el in doc.element.body.iterchildren():
        if el.tag == qn("w:p") and needle in ptext(el) and para_style_id(el) in ids:
            return el
    return None
```

## 2. Удаление тела раздела

```python
def drop_between(doc, start_el, stop_el):
    els = list(doc.element.body.iterchildren())
    i, j = els.index(start_el), els.index(stop_el)
    for el in els[i:j]:
        doc.element.body.remove(el)
    return j - i
```

Границы: `start` — первый абзац «Предварительные настройки» после заголовка
раздела; `stop` — следующий заголовок (`!Заг. 3`) или «Свидетельство анализа»
(`!Заг. 1`). Якорь для вставки — элемент перед `start` (таблица требований).

## 3. Вставка абзацев и рисунков — клонированием

Новые абзацы **не конструируются** через `add_paragraph`, а клонируются из
существующих: это единственный способ гарантировать валидный `w:pPr`, который
примет Word. Шаблоны берутся ДО удаления диапазона.

```python
import copy
from pathlib import Path
from docx.oxml.ns import qn
from docx.parts.image import ImagePart
from docx.image.image import Image as DocxImage
from docx.opc.constants import RELATIONSHIP_TYPE as RT

EMU_PER_MM = 36000

def strip_clone_junk(el):
    """Закладки клона дублируют w:id по всему документу — Word это отвергает."""
    for tag in ("w:bookmarkStart", "w:bookmarkEnd", "w:commentRangeStart",
                "w:commentRangeEnd", "w:proofErr"):
        for node in el.findall(".//" + qn(tag)):
            node.getparent().remove(node)

def set_text(el, text):
    runs = el.findall(qn("w:r"))
    for r in runs[1:]:
        el.remove(r)
    first = runs[0]
    for child in list(first):
        if child.tag != qn("w:rPr"):
            first.remove(child)
    t = OxmlElement("w:t"); t.set(qn("xml:space"), "preserve"); t.text = text
    first.append(t)

def disable_numbering(el):
    """numId=0 гасит нумерацию, унаследованную от стиля Н-4."""
    ppr = el.find(qn("w:pPr"))
    if ppr is None:
        return
    numpr = ppr.get_or_add_numPr()          # вставляет в правильном месте схемы
    numpr.get_or_add_ilvl().set(qn("w:val"), "0")
    numpr.get_or_add_numId().set(qn("w:val"), "0")

def put_image(el, path, width_mm, doc, docpr_id):
    img = DocxImage.from_file(str(path))
    part = ImagePart.from_image(img, doc.part.package.next_partname("/word/media/image%d.png"))
    rid = doc.part.relate_to(part, RT.IMAGE)
    el.findall(".//" + qn("a:blip"))[0].set(qn("r:embed"), rid)
    cx = int(width_mm * EMU_PER_MM); cy = int(cx * img.px_height / img.px_width)
    for tag in ("wp:extent", "a:ext"):
        for ext in el.findall(".//" + qn(tag)):
            ext.set("cx", str(cx)); ext.set("cy", str(cy))
    for tag in ("wp:docPr", "pic:cNvPr"):
        for node in el.findall(".//" + qn(tag)):
            node.set("id", str(docpr_id)); node.set("name", f"Рисунок {docpr_id}")
```

Вставка — «следующим за предыдущим» (`addnext`), подпись сразу после кадра:

```python
def insert(doc, anchor, tmpl_body, tmpl_cap, tmpl_img, ops):
    cur, docpr = anchor, 9000
    for op in ops:
        if op[0] == "p":
            el = copy.deepcopy(tmpl_body); strip_clone_junk(el)
            set_text(el, op[1]); disable_numbering(el)
        else:                                     # ("img", path, width_mm, caption)
            el = copy.deepcopy(tmpl_img); strip_clone_junk(el)
            docpr += 1; put_image(el, op[1], op[2], doc, docpr)
        cur.addnext(el); cur = el
        if op[0] == "img":
            cap = copy.deepcopy(tmpl_cap); strip_clone_junk(cap)
            set_text(cap, "Рисунок 0 – " + op[3])
            cur.addnext(cap); cur = cap
```

Шаблоны: тело — любой абзац `Н-4` с текстом и без рисунка; подпись — `Caption`
с «Рисунок …»; кадр — любой абзац с `a:blip`. Отступ первой строки у вставленных
абзацев копировать из абзаца-образца того же раздела (`w:ind`), иначе половина
абзацев встанет к левому полю.

## 4. Перенумерация подписей

```python
import re
from docx.text.paragraph import Paragraph

def renumber_captions(doc):
    n = 0
    for el in doc.element.body.iterchildren():
        if el.tag != qn("w:p"):
            continue
        t = ptext(el)
        m = re.match(r"^Рисунок\s+(\d+)\s*[–\-—]?\s*(.*)$", t)
        if not m:
            continue
        n += 1
        if int(m.group(1)) != n:
            para = Paragraph(el, doc)
            for r in para.runs[1:]:
                r._element.getparent().remove(r._element)
            para.runs[0].text = f"Рисунок {n} – {m.group(2)}".strip()
    return n
```

## 5. Подмена media-частей (рисунки с готовым кадром)

```python
def replace_media(src, dst, mapping):          # {номер рисунка: (path, width_mm|None)}
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == part_of_figure(num):
                data = Path(path).read_bytes()
            zout.writestr(item, data)
```

Перезапись идёт по байтам пакета, `wp:extent` правится вторым проходом через
`python-docx`. Сопоставление «Рисунок N → part» — по подписи, идущей ПОСЛЕ
изображения (логотип на титуле своей подписи не имеет). Ширину сохранять из
исходного `wp:extent`, если новый кадр не задан явно.

## 6. Порядок работы

1. Копия оригинала → подмена media-частей у рисунков с готовым кадром.
2. Пересборка разделов, где изменилось число шагов (клонирование абзацев).
3. Перенумерация подписей, замена заглушек `https://<IP>` на адрес стенда.
4. `python3 scripts/docx_word_hygiene_check.py file.docx` (этого же скилла) →
   `pandoc -f docx -t plain file.docx -o /dev/null` → `soffice --convert-to pdf`
   → `pdftoppm -png` → глазами страницы с новыми рисунками.

## Грабли

- **`numPr` перед `pStyle` — файл не открывается в Word.** `ppr.insert(0, numPr)`
  нарушает порядок детей `w:pPr`; LibreOffice и `docx_validate` молчат, Word
  выдаёт «нечитаемое содержимое». Только `ppr.get_or_add_numPr()`.
- **`numPr` в стиле.** Без `w:numId val="0"` вставленные абзацы получают
  сквозную нумерацию `1.1.1.x` через весь документ; удаление `numPr` из абзаца
  не помогает, если нумерация объявлена в стиле.
- **Клон тащит закладки.** `w:bookmarkStart/@w:id` обязан быть уникальным:
  двадцать клонов одного абзаца дают двадцать закладок с одним id.
- **Заголовки в оглавлении.** Тот же текст со стилем `toc*` — поиск «по
  вхождению» вернёт строку оглавления, и `drop_between` снесёт не тот участок.
- **Вставка в конец.** `add_paragraph`/`add_picture` дописывают в конец тела;
  без немедленного `addnext` порядок блоков перемешается.
- **Подпись и кадр — соседние абзацы.** При удалении лишних шагов удалять оба,
  иначе подпись остаётся без картинки (или наоборот).
- **Старые media-части остаются в пакете** после замены/удаления рисунков; файл
  растёт, но ссылки целы. Пересборка из оригинала убирает этот мусор.
