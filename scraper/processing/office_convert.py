# processing/office_convert.py
"""Windows Office（優先）／WPS（備援）轉檔，轉檔邏輯取自 word.py。

- Word（doc/docx）：Word.Application → KWPS.Application，先另存暫存 PDF（Word 無原生多圖匯出）。
- Excel（xls/xlsx）：Excel.Application → Ket.Application，ExportAsFixedFormat 匯出 PDF。
- PowerPoint（ppt/pptx）：PowerPoint.Application → Kwpp/KET，Slide.Export 直接匯出 PNG（免 PDF 中轉）。

注意：
- 只支援 Windows；背景執行緒使用前該執行緒需先 com_init()（內含執行緒層級一次性保護）。
- 原始檔先複製到暫存目錄並去除 Zone.Identifier，避免從網路下載的檔案被 Office 受保護視圖擋住。
"""
import os
import shutil
import subprocess
import tempfile
import threading

_tls = threading.local()


def com_init():
    """COM 在每個執行緒只需初始化一次（ProcManager / GUI 背景執行緒呼叫）。"""
    if not getattr(_tls, "ok", False):
        import pythoncom
        pythoncom.CoInitialize()
        _tls.ok = True


def kill_office():
    """轉檔失敗時清掉殘留的 Office / WPS 行程，釋放檔案鎖（與 word.py 一致）。"""
    for proc in ("POWERPNT.EXE", "WINWORD.EXE", "EXCEL.EXE", "wps.exe", "wpp.exe", "et.exe"):
        try:
            subprocess.run(["taskkill", "/f", "/im", proc],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _safe_quit(app):
    try:
        if app is not None:
            app.Quit()
    except Exception:
        pass


def _stage(src, ext, tmp):
    """複製到純英文暫存路徑並移除下載標記，避免受保護視圖／中文路徑導致 COM 開檔失敗。"""
    dst = os.path.join(tmp, f"in.{ext}")
    shutil.copy2(src, dst)
    try:
        os.remove(dst + ":Zone.Identifier")
    except OSError:
        pass
    return dst


# ---------------------------------------------------------------------- Word / Excel → PDF
def word_to_pdf(src, out_pdf):
    """Word 轉 PDF：微軟 Word 優先、WPS 文字（KWPS）備援。成功寫入 out_pdf，全失敗則拋錯。"""
    import win32com.client
    last_err = None
    for prog in ("Word.Application", "KWPS.Application"):
        app = doc = None
        try:
            app = win32com.client.DispatchEx(prog)
            app.Visible = False
            try:
                app.DisplayAlerts = 0
            except Exception:
                pass
            doc = app.Documents.Open(src, ReadOnly=True)
            doc.SaveAs(out_pdf, FileFormat=17)  # wdFormatPDF
            doc.Close(SaveChanges=0)
            _safe_quit(app)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            _safe_quit(app)
            kill_office()
    raise RuntimeError(f"Word/WPS 轉暫存 PDF 失敗: {last_err}")


def excel_to_pdf(src, out_pdf):
    """Excel 轉 PDF：微軟 Excel 優先、WPS 表格（Ket）備援。"""
    import win32com.client
    last_err = None
    for prog in ("Excel.Application", "Ket.Application"):
        app = wb = None
        try:
            app = win32com.client.DispatchEx(prog)
            app.Visible = False
            try:
                app.DisplayAlerts = False
            except Exception:
                pass
            wb = app.Workbooks.Open(src, ReadOnly=True)
            wb.ExportAsFixedFormat(0, os.path.abspath(out_pdf))  # xlTypePDF=0
            wb.Close(SaveChanges=False)
            _safe_quit(app)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            _safe_quit(app)
            kill_office()
    raise RuntimeError(f"Excel/WPS 轉暫存 PDF 失敗: {last_err}")


def office_to_pdf(src, ext, out_pdf):
    """Word/Excel 類文件轉 PDF（含暫存複製與環境清理）。"""
    com_init()
    tmp = tempfile.mkdtemp(prefix="oc_pdf_")
    try:
        staged = _stage(src, ext, tmp)
        if ext in ("docx", "doc"):
            word_to_pdf(staged, out_pdf)
        elif ext in ("xlsx", "xls"):
            excel_to_pdf(staged, out_pdf)
        else:
            raise RuntimeError(f"office_to_pdf 不支援 .{ext}")
        if not os.path.exists(out_pdf):
            raise RuntimeError("COM 轉檔後未產生 PDF")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------- PowerPoint → PNG
def _shape_texts(shape, out):
    """遞迴取出圖形內文字（含群組、表格、文字框），給 .ppt 等無 XML 可讀的簡報用。"""
    try:
        stype = getattr(shape, "Type", 0)
        if stype == 6:  # msoGroup
            for sub in shape.GroupItems:
                _shape_texts(sub, out)
            return
        if getattr(shape, "HasTable", 0) == -1:
            tbl = shape.Table
            for r in range(1, tbl.Rows.Count + 1):
                for c in range(1, tbl.Columns.Count + 1):
                    try:
                        t = tbl.Cell(r, c).Shape.TextFrame.TextRange.Text
                        if t:
                            out.append(t.strip())
                    except Exception:
                        pass
            return
        if getattr(shape, "HasTextFrame", 0) == -1:
            tf = shape.TextFrame
            if getattr(tf, "HasText", 0) == -1:
                t = tf.TextRange.Text
                if t:
                    out.append(t.strip())
    except Exception:
        pass


def _slide_text(slide):
    out = []
    try:
        for shape in slide.Shapes:
            _shape_texts(shape, out)
    except Exception:
        pass
    return "\n".join(t for t in out if t)


def ppt_export_slides(src, ext, img_dir, zoom=2.0, width_cap=0, max_pages=0):
    """PowerPoint 逐頁 Slide.Export 直出 PNG。

    回傳 (頁數, 每頁文字清單)。寬度：以 slide 點尺寸 × zoom 為基準，再受 width_cap（px）限制。
    """
    import win32com.client
    com_init()
    os.makedirs(img_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="oc_ppt_")
    staged = _stage(src, ext, tmp)
    last_err = None
    try:
        for prog in ("PowerPoint.Application", "Kwpp.Application", "KET.Application"):
            app = pres = None
            try:
                app = win32com.client.DispatchEx(prog)
                try:
                    app.DisplayAlerts = 1
                except Exception:
                    pass
                try:
                    pres = app.Presentations.Open(FileName=staged, ReadOnly=True,
                                                  Untitled=False, WithWindow=False)
                except Exception:
                    pres = app.Presentations.Open(staged, True, False, True)

                sw, sh = float(pres.PageSetup.SlideWidth), float(pres.PageSetup.SlideHeight)
                width = int(sw * zoom)
                if width_cap and width > width_cap:
                    width = int(width_cap)
                height = max(1, int(round(sh * width / sw)))

                count = min(pres.Slides.Count, max_pages) if max_pages else pres.Slides.Count
                texts = []
                for i in range(1, count + 1):
                    slide = pres.Slides(i)
                    texts.append(_slide_text(slide))
                    slide.Export(os.path.join(img_dir, f"slide_{i:03d}.png"), "PNG", width, height)
                try:
                    pres.Close()
                except Exception:
                    pass
                _safe_quit(app)
                return count, texts
            except Exception as e:  # noqa: BLE001
                last_err = e
                if pres is not None:
                    try:
                        pres.Close()
                    except Exception:
                        pass
                _safe_quit(app)
                kill_office()
        raise RuntimeError(f"PowerPoint/WPS 直接匯出圖片失敗: {last_err}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
