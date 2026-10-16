# processing/textutil.py
"""文字清洗與 Markdown 表格工具（移植 / 精簡自 word.py：clean_inline、join_text、_rows_to_markdown_table 等）。"""
import re

# 上下標佔位符（私有區字元，不會與正文衝突），最後才還原成 <sup>/<sub>
SUP_O, SUP_C, SUB_O, SUB_C = "\ue001", "\ue002", "\ue003", "\ue004"

_CJK_CH = "\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef"
_RE_CJK_GAP = re.compile(rf"(?<=[{_CJK_CH}])[ \t]+(?=[{_CJK_CH}])")
_RE_BLANK_PAREN = re.compile(r"[（(][ \u3000\t]*[)）]")
_RE_UNDERSCORE = re.compile(r"_{2,}")
CN_DIGITS = "一二三四五六七八九"
# 全形英數 → 半形（Ａ１ → A1；全形標點保留不動）
_FW = {i: i - 0xFEE0 for i in list(range(0xFF10, 0xFF1A)) + list(range(0xFF21, 0xFF3B)) + list(range(0xFF41, 0xFF5B))}
_SENTENCE_END = re.compile(r"[。！？!?.；;：:）)」』”]$")


def cn_num(n):
    if 1 <= n <= 9:
        return CN_DIGITS[n - 1]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + CN_DIGITS[n - 11]
    if n < 100:
        return CN_DIGITS[n // 10 - 1] + "十" + (CN_DIGITS[n % 10 - 1] if n % 10 else "")
    return str(n)


def join_text(a, b):
    """拼接折行：中文直接相連，英文 / 數字之間補空格。"""
    if not a:
        return b
    if not b:
        return a
    if re.search(r"[A-Za-z0-9,.;:!?)]$", a) and re.match(r"[A-Za-z0-9(]", b):
        return a + " " + b
    return a + b


def ends_sentence(text):
    return bool(_SENTENCE_END.search(text.strip()))


def clean_inline(text):
    """清洗單行：去除 CJK 之間多餘空格、全形英數轉半形（標點不動）、規範填空括號與底線。"""
    text = text.replace("\u00a0", " ").replace("\t", " ").translate(_FW)
    text = _RE_CJK_GAP.sub("", text)
    text = re.sub(r" {2,}", " ", text).strip()
    text = _RE_BLANK_PAREN.sub("（　）", text)
    return _RE_UNDERSCORE.sub(lambda m: "＿" * min(max(len(m.group(0)), 3), 12), text)


def restore_marks(t):
    return (t.replace(SUP_O, "<sup>").replace(SUP_C, "</sup>")
             .replace(SUB_O, "<sub>").replace(SUB_C, "</sub>"))


def strip_marks(t):
    for m in (SUP_O, SUP_C, SUB_O, SUB_C):
        t = t.replace(m, "")
    return t


def rows_to_markdown_table(rows, norm=None):
    """二維列表 → Markdown 表格（空列略過、欄數補齊、| 轉全形避免破壞表格；norm 為儲存格清洗函式）。"""
    cleaned, max_cols = [], 0
    for row in rows or []:
        r = [str(c).replace("\n", " ").replace("|", "｜").strip() if c is not None else "" for c in row]
        if norm:
            r = [norm(c) for c in r]
        if any(r):
            cleaned.append(r)
            max_cols = max(max_cols, len(r))
    if not cleaned or max_cols == 0:
        return ""
    for r in cleaned:
        r.extend([""] * (max_cols - len(r)))
    lines = ["| " + " | ".join(cleaned[0]) + " |", "| " + " | ".join(["---"] * max_cols) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in cleaned[1:]]
    return "\n".join(lines)


def clean_doc_text(text):
    """通用清洗（不改字元寬度）：移除控制字元、壓縮多餘空白與空行。"""
    if not text:
        return ""
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def md_escape(text):
    """轉義會被 Markdown 誤解析的字元，並還原上下標標籤。"""
    t = text.replace("*", "\\*").replace("`", "\\`").replace("<", "&lt;")
    t = re.sub(r"(?<![A-Za-z0-9\\])_|_(?![A-Za-z0-9])", r"\\_", t)
    return restore_marks(t)


def lead_escape(t):
    """行首的 # > - + 會被當成 Markdown 語法，加反斜線轉義。"""
    return re.sub(r"^([#>\-+])", r"\\\1", t)
