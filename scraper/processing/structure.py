# processing/structure.py
"""結構還原：把「已排好閱讀順序的 行 / 表格 / 插圖」串流還原成 大題 → 題目 → 選項 的 Markdown 與題庫清單。

移植自 word.py 的 ExamStructureParser 與 Markdown 渲染，改動：
- 插圖已在擷取階段裁切存檔，這裡只負責「綁定到題目」與輸出 ![..](R2 路徑)。
- 選項排版三種：list=逐行並排（每個選項一行）、inline=單行並排（選項同一行）、auto=短選項單行 / 長選項逐行。
非考卷文件也能用：沒有題號時會退化成「標題 / 段落 / 表格 / 插圖」。
"""
import re
import unicodedata
from collections import Counter

from processing.textutil import (clean_inline, join_text, restore_marks, md_escape, lead_escape, strip_marks)

OPTION_STYLES = ("list", "inline", "auto")


class ExamStructureParser:
    SEC_RE = re.compile(r"^[（(]?\s*([一二三四五六七八九十百壹貳貮參叁肆伍陸柒捌玖拾]{1,3})\s*[)）]?\s*[、．.：:]\s*(.+)$")
    PART_RE = re.compile(r"^(第\s*[一二三四五六七八九十\d]+\s*[部大]\s*[分份]|Part\s*[A-Z0-9]+)", re.I)
    SEC_STRONG = re.compile(r"選擇|是非|填充|填空|問答|簡答|閱讀|測驗|計算|配合|應用|題組|作文|國字|注音|改錯|"
                            r"翻譯|聽力|綜合|單選|複選|多選|非選|造句|判斷|選填|解釋|申論|實作")
    SEC_WEAK = re.compile(r"^[\u4e00-\u9fff]{1,6}題\s*([（(].*[)）])?$")
    NOT_SEC = re.compile(r"本試卷|本卷|試卷共|請|須知|注意|共有\d")
    ANSWER_SEC = re.compile(r"答案|解答|詳解|解析|解說")
    ANS_PREFIX = re.compile(r"^(?:參考|正確)?(?:答案|解答|Ans(?:wer)?s?)\s*[：:]?\s*", re.I)
    END_RE = re.compile(r"^[—\-－~～\s]*(試題結束|以下空白|本試卷結束|試卷完|—完—|The End)", re.I)
    META_KEYS = ["班級", "姓名", "座號", "學號", "分數", "得分", "科目", "年級", "日期", "時間", "滿分", "命題", "審題", "範圍"]
    GROUP_RE = re.compile(r"第\s*\d{1,3}\s*[～~\-－至到—]\s*第?\s*\d{1,3}\s*題")

    Q1 = re.compile(r"^[（(]\s*(?P<a>[A-Ea-eＡ-Ｅ√×Ｏ○Ｘ✓✗OXox])?\s*[)）]\s*(?P<n>\d{1,3})\s*[.、．)）:：]\s*(?!\d)(?P<r>.*)$")
    Q2 = re.compile(r"^第\s*(?P<n>\d{1,3})\s*題\s*[:：.、．]?\s*(?P<r>.*)$")
    Q4 = re.compile(r"^[【\[]\s*(?P<n>\d{1,3})\s*[】\]]\s*(?P<r>.*)$")
    Q3 = re.compile(r"^(?P<n>\d{1,3})\s*(?:[.、．)）]\s*(?!\d)|\s+(?=[（(]\s*[)）]))(?P<r>.*)$")

    OPT_RE = re.compile(
        r"(?P<paren>[（(]\s*(?P<pl>[A-Ea-eＡ-Ｅ])\s*[)）])"
        r"|(?P<circ>[①②③④⑤⑥⑦⑧⑨⑩])"
        r"|(?P<pnum>[（(]\s*(?P<pn>\d{1,2})\s*[)）])"
        r"|(?P<dot>(?<![A-Za-z0-9])(?P<dl>[A-E])[.、．](?=\s|[\u3400-\u9fff]))"
    )
    CIRC = "①②③④⑤⑥⑦⑧⑨⑩"
    ANS_PAIR = re.compile(r"(?<!\d)(\d{1,3})\s*[.、．:：\-－)）]?\s*([A-Ea-eＡ-Ｅ①-⑩○×√✓✗OXox甲乙丙丁戊]{1,5})(?![A-Za-z\d])")
    ANS_RANGE = re.compile(r"(\d{1,3})\s*[-－~～至到]\s*(\d{1,3})\s*[:：.、]?\s*([A-Ea-eＡ-Ｅ]{2,})")

    def __init__(self, body_size=10.5):
        self.body = body_size or 10.5

    # ------------------------------------------------------------ 入口
    def parse(self, stream):
        self.nodes, self.cur_q, self.cur_para, self.prev_it = [], None, None, None
        self.sec_title, self.sec_idx, self.answer_mode = "", 0, False
        self.last_num, self.in_group, self.seen_q, self.h1_done = 0, False, False, False
        self.pending_figs, self.answer_pairs = [], []
        for it in stream:
            t = it["type"]
            if t == "table":
                self.cur_para = self.prev_it = None
                if self.cur_q is not None:
                    self.cur_q["blocks"].append(["tbl", it["md"]])
                else:
                    self.nodes.append({"k": "table", "md": it["md"]})
            elif t == "figure":
                fig = it["fig"]
                fig["page"] = it.get("page", 1)
                if not fig.get("name"):
                    continue
                if it.get("bind_next"):
                    self.pending_figs.append(fig)
                else:
                    self._attach_fig(fig)
            else:
                self._handle_line(it)
        return self.nodes

    # ------------------------------------------------------------ 題號 / 選項
    @classmethod
    def match_question(cls, text):
        """回傳 (答案, 題號, 題幹剩餘, 是否強特徵, 是否帶括號, 是否帶字母答案)。"""
        m = cls.Q1.match(text)
        if m:
            ans = m.group("a") or ""
            ans = unicodedata.normalize("NFKC", ans).upper() if ans else ""
            return ans, int(m.group("n")), m.group("r").strip(), True, True, bool(ans)
        m = cls.Q2.match(text)
        if m:
            return "", int(m.group("n")), m.group("r").strip(), True, False, False
        m = cls.Q4.match(text)
        if m:
            return "", int(m.group("n")), m.group("r").strip(), True, False, False
        m = cls.Q3.match(text)
        if m:
            rest, paren = m.group("r").strip(), False
            m2 = re.match(r"^[（(]\s*[)）]\s*", rest)
            if m2:
                paren, rest = True, rest[m2.end():]
            return "", int(m.group("n")), rest, paren, paren, False
        return None

    def _accept_num(self, num, strong, has_letter):
        last = self.last_num
        if has_letter:
            return num == last + 1 or last == 0
        if strong:
            return num == last + 1 or last == 0 or num == 1 or last < num <= last + 5
        return num == last + 1 or (last == 0 and num <= 2)

    def _label_of(self, m):
        if m.group("pl"):
            L = unicodedata.normalize("NFKC", m.group("pl")).upper()
            return "a", ord(L) - 64, f"({L})"
        if m.group("circ"):
            ch = m.group("circ")
            return "c", self.CIRC.index(ch) + 1, ch
        if m.group("pn"):
            n = int(m.group("pn"))
            return "d", n, f"({n})"
        L = m.group("dl")
        return "a", ord(L) - 64, f"({L})"

    def _option_chain(self, text, expected):
        ms = list(self.OPT_RE.finditer(text))
        if not ms:
            return None
        chain = []
        for m in ms:
            fam, idx, disp = self._label_of(m)
            if not chain:
                ok = (expected is not None and (fam, idx) == expected) or idx == 1
                if expected is not None and m.start() == 0 and idx == 1:
                    ok = True
                if ok:
                    chain.append((fam, idx, disp, m))
            else:
                lf, li = chain[-1][0], chain[-1][1]
                if fam == lf and idx == li + 1:
                    chain.append((fam, idx, disp, m))
        if not chain:
            return None
        start = chain[0][3].start()
        if start > 0 and len(chain) < 3:
            return None
        prefix = text[:start].strip()
        opts = []
        for i, (fam, idx, disp, m) in enumerate(chain):
            end = chain[i + 1][3].start() if i + 1 < len(chain) else len(text)
            opts.append({"fam": fam, "idx": idx, "label": disp, "text": clean_inline(text[m.end():end])})
        return prefix, opts

    # ------------------------------------------------------------ 答案行
    def _parse_answers(self, text):
        t = self.ANS_PREFIX.sub("", text).strip()
        pairs = []
        for m in self.ANS_RANGE.finditer(t):
            a, b = int(m.group(1)), int(m.group(2))
            letters = re.sub(r"[\s,、，]", "", m.group(3))
            if b >= a and len(letters) == b - a + 1:
                for i, ch in enumerate(letters):
                    pairs.append((a + i, unicodedata.normalize("NFKC", ch).upper()))
        t2 = self.ANS_RANGE.sub(" ", t)
        found = self.ANS_PAIR.findall(t2)
        leftover = re.sub(r"[\s,，、;；/]", "", self.ANS_PAIR.sub(" ", t2))
        if leftover:
            return []
        for n, a in found:
            pairs.append((int(n), unicodedata.normalize("NFKC", a).upper()))
        return pairs

    # ------------------------------------------------------------ 分類
    def _is_meta(self, text):
        keys = sum(1 for k in self.META_KEYS if k in text)
        if keys >= 2:
            return True
        return keys == 1 and len(text) <= 40 and bool(re.search(r"[：:＿_]", text))

    def _match_section(self, text, it):
        if self.NOT_SEC.search(text) or len(text) > 70:
            return None
        bold = it.get("bold") or (it.get("size") or self.body) >= self.body * 1.1
        m = self.SEC_RE.match(text)
        if m:
            title = m.group(2).strip()
            if self.SEC_STRONG.search(title) or (len(text) <= 24 and bold and not text.endswith("。")):
                return f"{m.group(1)}、{title}"
            return None
        if self.PART_RE.match(text) and len(text) <= 40:
            return text
        if (self.SEC_STRONG.search(text) and len(text) <= 36 and not re.search(r"[。，,；;]", text.split("（")[0])
                and (bold or re.search(r"每題|共\s*\d+\s*分|配分", text))):
            return text
        if self.SEC_WEAK.match(text) and bold:
            return text
        return None

    def _para_break(self, it):
        if it.get("para"):
            return True
        p = self.prev_it
        if p is None or it.get("indent"):
            return True
        if p.get("short") and re.search(r"[。！？!?：:」』）)；;]$", p.get("text", "")):
            return True
        return bool(p.get("short") and len(p.get("text", "")) <= 12)

    # ------------------------------------------------------------ 節點操作
    def _close_q(self):
        self.cur_q = None
        self.cur_para = None

    def _attach_fig(self, fig):
        if self.cur_q is not None:
            self.cur_q["blocks"].append(["fig", fig])
        else:
            self.nodes.append({"k": "fig", "fig": fig})
            self.cur_para = None
        self.prev_it = None

    def _add_para(self, text, it):
        quote = self.in_group
        if self.cur_para is not None and not self._para_break(it) and self.cur_para["quote"] == quote:
            self.cur_para["text"] = join_text(self.cur_para["text"], text)
        else:
            node = {"k": "para", "text": text, "quote": quote}
            self.nodes.append(node)
            self.cur_para = node
        self.prev_it = it

    def _append_q_text(self, text, it, first=False):
        q = self.cur_q
        if q["opts"]:
            q["opts"][-1]["text"] = join_text(q["opts"][-1]["text"], text)
            self.prev_it = it
            return
        blocks = q["blocks"]
        if blocks and blocks[-1][0] == "p" and not first and not self._para_break(it):
            blocks[-1][1] = join_text(blocks[-1][1], text)
        else:
            blocks.append(["p", text])
        self.prev_it = it

    def _feed_q(self, text, it, first=False):
        q, text = self.cur_q, text.strip()
        if not text:
            return
        expected = None
        if q["opts"]:
            last = q["opts"][-1]
            expected = (last["fam"], last["idx"] + 1)
        chain = self._option_chain(text, expected)
        if chain:
            prefix, opts = chain
            if prefix:
                self._append_q_text(prefix, it, first)
            q["opts"].extend(opts)
            self.prev_it = it
            return
        self._append_q_text(text, it, first)

    def _start_question(self, ans, num, rest, paren, it):
        self._close_q()
        q = {"k": "q", "num": num, "ans": ans, "paren": paren, "section": self.sec_title,
             "page": it.get("page", 1), "blocks": [], "opts": [], "in_answer": self.answer_mode}
        self.nodes.append(q)
        self.cur_q, self.seen_q, self.last_num, self.in_group = q, True, num, False
        for fig in self.pending_figs:        # 先於題號行出現的並排插圖，綁定給這一題（渲染時排在題幹之後）
            q["blocks"].append(["fig", fig, True])
        self.pending_figs, self.prev_it = [], None
        if rest:
            self._feed_q(clean_inline(rest), it, first=True)

    # ------------------------------------------------------------ 逐行
    def _handle_line(self, it):
        text = clean_inline(it.get("text", ""))
        if not text:
            return
        size = it.get("size") or self.body

        if it.get("quote"):
            self._close_q()
            self.nodes.append({"k": "para", "text": text, "quote": True})
            self.cur_para = None
            return
        if it.get("vertical"):
            self._close_q()
            if not self.h1_done:
                self.nodes.append({"k": "h", "level": 1, "text": text})
                self.h1_done = True
            else:
                self.nodes.append({"k": "para", "text": text, "quote": True})
            return
        if self.END_RE.match(text):
            self._close_q()
            self.nodes.append({"k": "para", "text": text, "quote": False})
            return

        if self.answer_mode or self.ANS_PREFIX.match(text):
            pairs = self._parse_answers(text)
            if pairs and (self.answer_mode or len(pairs) >= 2):
                self._close_q()
                self.answer_pairs.extend(pairs)
                if self.nodes and self.nodes[-1]["k"] == "answers":
                    self.nodes[-1]["pairs"].extend(pairs)
                else:
                    self.nodes.append({"k": "answers", "pairs": list(pairs)})
                return

        heading = it.get("heading", 0)
        sec = self._match_section(text, it)
        if sec:
            self._close_q()
            self.sec_idx += 1
            self.sec_title = sec
            self.answer_mode = bool(self.ANSWER_SEC.search(sec))
            self.last_num = 0
            self.in_group = bool(re.search(r"閱讀|題組|文章", sec))
            self.nodes.append({"k": "sec", "text": sec, "idx": self.sec_idx})
            self.prev_it = None
            return

        qm = self.match_question(text)
        if qm:
            ans, num, rest, strong, paren, has_letter = qm
            if self._accept_num(num, strong, has_letter):
                self._start_question(ans, num, rest, paren, it)
                return

        if self.GROUP_RE.search(text) and len(text) <= 120:
            self._close_q()
            self.nodes.append({"k": "para", "text": "📎 " + text, "quote": True})
            self.in_group = True
            self.cur_para = None
            return

        if not self.seen_q and (heading or (size >= self.body * 1.28 and len(text) <= 60)):
            self._close_q()
            level = 1 if not self.h1_done else 2
            if heading:
                level = min(max(heading, 1), 3)
            self.h1_done = True
            self.nodes.append({"k": "h", "level": level, "text": text})
            self.prev_it = None
            return
        if heading and self.seen_q:
            self._close_q()
            self.nodes.append({"k": "sec", "text": text, "idx": self.sec_idx})
            return

        if not self.seen_q and self._is_meta(text):
            if self.nodes and self.nodes[-1]["k"] == "meta":
                self.nodes[-1]["text"] += "　　" + text
            else:
                self.nodes.append({"k": "meta", "text": text})
            self.cur_para = None
            return

        if self.cur_q is not None:
            self._feed_q(text, it)
        else:
            self._add_para(text, it)

    # ------------------------------------------------------------ 題庫
    def export_questions(self, nodes, img_base=""):
        explanations = {}
        for nd in nodes:
            if nd["k"] == "q" and nd.get("in_answer"):
                explanations.setdefault(nd["num"], []).append(" ".join(b[1] for b in nd["blocks"] if b[0] == "p"))
        ansmap = {}
        for n, a in self.answer_pairs:
            ansmap.setdefault(n, a)
        result = []
        for nd in nodes:
            if nd["k"] != "q" or nd.get("in_answer"):
                continue
            ans = nd["ans"]
            if not ans and nd["opts"] and nd["num"] in ansmap:
                ans = ansmap.pop(nd["num"])
            result.append({
                "number": nd["num"], "section": nd["section"], "page": nd["page"], "answer": ans,
                "stem": restore_marks("\n".join(b[1] for b in nd["blocks"] if b[0] == "p")),
                "options": [{"label": o["label"], "text": restore_marks(o["text"])} for o in nd["opts"]],
                "images": [img_base + b[1]["key"] for b in nd["blocks"] if b[0] == "fig" and b[1].get("key")],
                "tables": [b[1] for b in nd["blocks"] if b[0] == "tbl"],
                "explanation": "\n".join(explanations.get(nd["num"], [])),
            })
        return result


# ====================================================================== 插圖綁定 / 字號
def bind_figures(stream):
    """圖在題幹旁邊（左 / 右並排）時，圖的位置可能先於題號行；此時把圖綁定給「下一題」。"""
    for i, it in enumerate(stream):
        if it["type"] != "figure":
            continue
        for j in range(i + 1, min(i + 4, len(stream))):
            nx = stream[j]
            if nx["type"] != "line":
                continue
            if nx.get("page") == it.get("page"):
                qm = ExamStructureParser.match_question(clean_inline(nx["text"]))
                cy = (nx["bbox"][1] + nx["bbox"][3]) / 2
                beside = nx["bbox"][0] >= it["bbox"][2] - 4 or nx["bbox"][2] <= it["bbox"][0] + 4
                if qm and beside and it["bbox"][1] - 2 <= cy <= it["bbox"][3]:
                    it["bind_next"] = True
            break


def body_size(stream):
    cnt = Counter()
    for it in stream:
        if it["type"] == "line" and it.get("size"):
            cnt[round(it["size"] * 2) / 2] += len(it["text"])
    return cnt.most_common(1)[0][0] if cnt else 10.5


# ====================================================================== Markdown 渲染
def _answers_table(pairs):
    uniq = list(dict(pairs).items())
    lines = []
    for i in range(0, len(uniq), 10):
        chunk = uniq[i:i + 10]
        lines.append("| 題號 | " + " | ".join(str(n) for n, _ in chunk) + " |")
        lines.append("| --- |" + " --- |" * len(chunk))
        lines.append("| 答案 | " + " | ".join(a for _, a in chunk) + " |")
        lines.append("")
    return "\n".join(lines).strip()


def options_inline(opts, style):
    """決定這一題的選項要『單行並排』還是『逐行並排』。"""
    if style == "inline":
        return True
    if style == "auto":
        texts = [strip_marks(o["text"]) for o in opts]
        return len(opts) <= 4 and all(len(t) <= 12 for t in texts) and sum(len(t) for t in texts) <= 40
    return False


def render_options(opts, style):
    if not opts:
        return ""
    if options_inline(opts, style):
        return "　　".join(f"{o['label']} {md_escape(o['text'])}" for o in opts)
    return "\n".join(f"- {o['label']} {md_escape(o['text'])}" for o in opts)


def _img_md(fig, alt, img_base):
    return f"![{alt}]({img_base}{fig['key']})"


def _render_question(nd, style, img_base):
    lead = f"**{nd['num']}.**"
    paren = f"（{nd['ans']}）" if nd["ans"] else ("（　）" if nd["paren"] else "")
    # 題號 + 括號答案；括號與題幹之間不加空格（中文排版），題號粗體後需要空格否則 CJK 會讓 ** 失效
    head = f"{lead} {paren}" if paren else lead
    parts, cur, fig_k = [], head, 0
    side, stem_done = [], False        # 與題幹並排的插圖：先暫存，題幹輸出後再放

    def emit_fig(fig):
        nonlocal fig_k, cur
        if cur is not None:
            parts.append(cur)
            cur = None
        fig_k += 1
        parts.append(_img_md(fig, f"第{nd['num']}題插圖{fig_k}", img_base))

    for blk in nd["blocks"]:
        kind, val = blk[0], blk[1]
        if kind == "p":
            if cur is not None:
                parts.append(cur + ("" if paren and cur == head else " ") + md_escape(val))
                cur = None
            else:
                parts.append(md_escape(val))
            if not stem_done:
                stem_done = True
                for f in side:
                    emit_fig(f)
                side = []
        elif kind == "tbl":
            if cur is not None:
                parts.append(cur)
                cur = None
            parts.append(val.strip())
        elif len(blk) > 2 and blk[2] and not stem_done:
            side.append(val)
        else:
            emit_fig(val)
    for f in side:
        emit_fig(f)
    if cur is not None:
        parts.append(cur)
    if nd["opts"]:
        parts.append(render_options(nd["opts"], style))
    return "\n\n".join(parts)


def render_nodes(nodes, option_style="list", img_base=""):
    out, fig_n = [], 0
    for nd in nodes:
        k = nd["k"]
        if k == "h":
            out.append("#" * nd["level"] + " " + md_escape(nd["text"]))
        elif k == "meta":
            out.append("> " + md_escape(nd["text"]))
        elif k == "sec":
            out.append("### " + md_escape(nd["text"]))
        elif k == "para":
            t = md_escape(nd["text"])
            out.append("> " + t if nd["quote"] else lead_escape(t))
        elif k == "table":
            out.append(nd["md"].strip())
        elif k == "fig":
            fig_n += 1
            out.append(_img_md(nd["fig"], f"插圖{fig_n}", img_base))
        elif k == "answers":
            out.append(_answers_table(nd["pairs"]))
        elif k == "q":
            out.append(_render_question(nd, option_style, img_base))
    return "\n\n".join(x for x in out if x and x.strip())


def build_markdown(stream, option_style="list", img_base=""):
    """已排序的條目串流 → (markdown, 題庫清單)。img_base 為圖片路徑前綴（如 R2 公開域名）。"""
    if option_style not in OPTION_STYLES:
        option_style = "list"
    if not stream:
        return "", []
    bind_figures(stream)
    parser = ExamStructureParser(body_size(stream))
    nodes = parser.parse(stream)
    return render_nodes(nodes, option_style, img_base), parser.export_questions(nodes, img_base)
