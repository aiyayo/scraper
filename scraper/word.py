import datetime
import json
import os
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import fitz  # PyMuPDF
import pythoncom
import win32com.client


class OfficeToImageApp:
    """
    Windows Office 优先、WPS 替补的极速直出图片版工具 (带文件创建时间提取)
    依赖库: pip install pywin32 pymupdf
    """

    def __init__(self, root):
        self.root = root
        self.root.title("Office / WPS 极速直出图片工具 (含创建时间提取与JSON生成)")
        self.root.geometry("720x600")
        self.root.resizable(False, False)

        self.file_list = []
        self.output_dir = tk.StringVar()
        self.dpi_var = tk.IntVar(value=150)  # 默认 150 DPI 追求极致速度
        self.fmt_var = tk.StringVar(value="PNG")

        self._build_ui()
        self.root.after(500, self._diagnose_environment)

    def _build_ui(self):
        # 1. 文件选择区域
        frame_files = tk.LabelFrame(
            self.root, text="1. 选择要转换的文件 (支持 PDF / Word / PPT)", padx=10, pady=8
        )
        frame_files.pack(fill="x", padx=15, pady=8)

        btn_add = tk.Button(frame_files, text="添加文件", width=12, command=self.select_files)
        btn_add.pack(side="left", padx=5)

        btn_add_dir = tk.Button(frame_files, text="添加文件夹", width=12, command=self.select_folder_files)
        btn_add_dir.pack(side="left", padx=5)

        btn_clear = tk.Button(frame_files, text="清空列表", width=12, command=self.clear_files)
        btn_clear.pack(side="left", padx=5)

        self.lbl_count = tk.Label(frame_files, text="已选 0 个文件", fg="gray")
        self.lbl_count.pack(side="right", padx=10)

        # 2. 参数与输出目录设置
        frame_settings = tk.LabelFrame(self.root, text="2. 导出设置", padx=10, pady=8)
        frame_settings.pack(fill="x", padx=15, pady=5)

        tk.Label(frame_settings, text="输出目录:").grid(row=0, column=0, sticky="w", pady=5)
        tk.Entry(frame_settings, textvariable=self.output_dir, width=54).grid(
            row=0, column=1, padx=5, pady=5
        )
        tk.Button(frame_settings, text="浏览...", width=10, command=self.select_output_dir).grid(
            row=0, column=2, padx=5
        )

        tk.Label(frame_settings, text="图片格式:").grid(row=1, column=0, sticky="w", pady=5)
        fmt_combo = ttk.Combobox(
            frame_settings,
            textvariable=self.fmt_var,
            values=["PNG", "JPG"],
            state="readonly",
            width=10,
        )
        fmt_combo.grid(row=1, column=1, sticky="w", padx=5)

        tk.Label(frame_settings, text="清晰度 (DPI):").grid(
            row=1, column=1, sticky="e", padx=95
        )
        dpi_combo = ttk.Combobox(
            frame_settings,
            textvariable=self.dpi_var,
            values=[100, 150, 200, 300],
            state="readonly",
            width=8,
        )
        dpi_combo.grid(row=1, column=2, sticky="w", padx=5)

        # 3. 执行与进度
        frame_action = tk.Frame(self.root, padx=15, pady=2)
        frame_action.pack(fill="x")

        self.btn_start = tk.Button(
            frame_action,
            text="开始极速转换并提取创建时间",
            bg="#0078D7",
            fg="white",
            font=("微软雅黑", 10, "bold"),
            height=2,
            command=self.start_conversion,
        )
        self.btn_start.pack(fill="x", pady=5)

        self.progress = ttk.Progressbar(frame_action, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", pady=2)

        # 4. 日志输出区域
        frame_log = tk.LabelFrame(self.root, text="运行与状态日志", padx=10, pady=5)
        frame_log.pack(fill="both", expand=True, padx=15, pady=5)

        self.log_area = scrolledtext.ScrolledText(
            frame_log, height=10, state="disabled", font=("Consolas", 9)
        )
        self.log_area.pack(fill="both", expand=True)

    def log(self, message):
        def _append():
            self.log_area.config(state="normal")
            self.log_area.insert(tk.END, message + "\n")
            self.log_area.see(tk.END)
            self.log_area.config(state="disabled")
        self.root.after(0, _append)

    def kill_office_processes(self):
        try:
            for proc in ["POWERPNT.EXE", "WINWORD.EXE", "wps.exe", "wpp.exe", "et.exe"]:
                subprocess.run(["taskkill", "/f", "/im", proc], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _diagnose_environment(self):
        self.log("========== 正在检测系统可用组件 ==========")
        pythoncom.CoInitialize()
        
        has_office_ppt = False
        try:
            app = win32com.client.DispatchEx("PowerPoint.Application")
            app.Quit()
            has_office_ppt = True
        except Exception:
            pass

        has_wps_ppt = False
        for prog in ["Kwpp.Application", "KET.Application"]:
            try:
                app = win32com.client.DispatchEx(prog)
                app.Quit()
                has_wps_ppt = True
                break
            except Exception:
                pass

        pythoncom.CoUninitialize()
        self.log(f"-> 演示组件: 微软 Office [{ '可用' if has_office_ppt else '不可用' }] | WPS [{ '可用' if has_wps_ppt else '不可用' }]")
        self.log("==========================================")

    def _get_file_creation_time(self, file_path):
        """获取文件的第一次生成时间（创建时间）并格式化为字符串"""
        try:
            timestamp = os.path.getctime(file_path)
            dt = datetime.datetime.fromtimestamp(timestamp)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知时间"

    def select_files(self):
        files = filedialog.askopenfilenames(
            title="选择文件",
            filetypes=[
                ("支持的文档", "*.pdf;*.docx;*.doc;*.pptx;*.ppt"),
                ("PDF 文件", "*.pdf"),
                ("Word 文档", "*.docx;*.doc"),
                ("PowerPoint 演示文稿", "*.pptx;*.ppt"),
            ],
        )
        if files:
            added = 0
            for f in files:
                norm_f = os.path.normpath(f)
                if norm_f not in self.file_list:
                    self.file_list.append(norm_f)
                    added += 1
            self.lbl_count.config(text=f"已选 {len(self.file_list)} 个文件")
            if not self.output_dir.get() and self.file_list:
                self.output_dir.set(os.path.dirname(self.file_list[0]))
            self.log(f"已添加 {added} 个文件，当前共 {len(self.file_list)} 个待处理文件。")

    def select_folder_files(self):
        folder = filedialog.askdirectory(title="选择包含文档的文件夹")
        if folder:
            supported_exts = {".pdf", ".docx", ".doc", ".pptx", ".ppt"}
            added = 0
            for root_dir, _, files in os.walk(folder):
                for file in files:
                    if file.startswith("~$"):
                        continue
                    ext = os.path.splitext(file)[1].lower()
                    if ext in supported_exts:
                        full_path = os.path.normpath(os.path.join(root_dir, file))
                        if full_path not in self.file_list:
                            self.file_list.append(full_path)
                            added += 1
            self.lbl_count.config(text=f"已选 {len(self.file_list)} 个文件")
            if not self.output_dir.get() and self.file_list:
                self.output_dir.set(os.path.normpath(folder))
            self.log(f"从文件夹扫描并添加 {added} 个文件，当前共 {len(self.file_list)} 个待处理文件。")

    def clear_files(self):
        self.file_list.clear()
        self.lbl_count.config(text="已选 0 个文件")
        self.log("文件列表已清空。")

    def select_output_dir(self):
        folder = filedialog.askdirectory(title="选择图片保存目录")
        if folder:
            self.output_dir.set(os.path.normpath(folder))

    def start_conversion(self):
        if not self.file_list:
            messagebox.showwarning("提示", "请先添加需要转换的文件！")
            return
        if not self.output_dir.get():
            messagebox.showwarning("提示", "请选择输出目录！")
            return

        self.btn_start.config(state="disabled", text="正在极速转换中...")
        self.progress["value"] = 0
        self.progress["maximum"] = len(self.file_list)

        t = threading.Thread(target=self._run_worker, daemon=True)
        t.start()

    def _run_worker(self):
        pythoncom.CoInitialize()
        out_base = os.path.normpath(os.path.abspath(self.output_dir.get()))
        dpi = self.dpi_var.get()
        img_fmt = self.fmt_var.get().lower()

        self.kill_office_processes()
        success_count = 0

        try:
            for idx, file_path in enumerate(self.file_list, 1):
                file_path = os.path.normpath(os.path.abspath(file_path))
                file_name = os.path.splitext(os.path.basename(file_path))[0]
                ext = os.path.splitext(file_path)[1].lower()

                # 获取文件第一次生成时间（创建时间）
                create_time = self._get_file_creation_time(file_path)

                target_folder = os.path.normpath(os.path.join(out_base, f"{file_name}_图片"))
                os.makedirs(target_folder, exist_ok=True)

                self.log(f"[{idx}/{len(self.file_list)}] 处理: {os.path.basename(file_path)}")
                self.log(f"  -> 创建时间: {create_time}")

                try:
                    if ext == ".pdf":
                        page_cnt = self._pdf_to_images(file_path, target_folder, dpi, img_fmt)
                    elif ext in [".docx", ".doc"]:
                        temp_pdf = os.path.normpath(os.path.join(tempfile.gettempdir(), f"fast_word_{os.getpid()}.pdf"))
                        self._word_to_pdf_with_fallback(file_path, temp_pdf)
                        page_cnt = self._pdf_to_images(temp_pdf, target_folder, dpi, img_fmt, prefix="page")
                        if os.path.exists(temp_pdf):
                            os.remove(temp_pdf)
                    elif ext in [".pptx", ".ppt"]:
                        page_cnt = self._ppt_to_images_direct(file_path, target_folder, dpi, img_fmt)

                    # 自动生成 info.json 配置文件
                    image_files = [f for f in os.listdir(target_folder) if f.lower().endswith(f".{img_fmt}")]
                    image_files.sort()
                    
                    html_content = ""
                    for img in image_files:
                        html_content += f'<p><img src="{img}" alt="{file_name}"></p>\n'

                    meta_data = {
                        "title": file_name,
                        "summary": f"由工具自动转换，原文件创建于 {create_time}",
                        "create_time": create_time,
                        "total_pages": page_cnt,
                        "images": image_files,
                        "content": html_content
                    }
                    
                    json_path = os.path.join(target_folder, "info.json")
                    with open(json_path, "w", encoding="utf-8") as jf:
                        json.dump(meta_data, jf, ensure_ascii=False, indent=4)

                    success_count += 1
                    self.log(f"  -> 成功！(共 {page_cnt} 页) 已保存至: {target_folder}")
                except Exception as e:
                    self.log(f"  -> [失败] {os.path.basename(file_path)}: {str(e)}")
                    self.kill_office_processes()

                self.root.after(0, lambda v=idx: self.progress.config(value=v))

            self.log(f"========== 转换完成：成功 {success_count}/{len(self.file_list)} 个文件 ==========")
            self.root.after(0, lambda: messagebox.showinfo("完成", f"转换处理完毕！\n成功: {success_count} 个\n总数: {len(self.file_list)} 个"))

        finally:
            self.kill_office_processes()
            pythoncom.CoUninitialize()
            self.root.after(0, lambda: self.btn_start.config(state="normal", text="开始极速转换并提取创建时间"))

    def _pdf_to_images(self, pdf_path, target_folder, dpi, img_fmt, prefix="page"):
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)

        for page_num in range(total_pages):
            page = doc.load_page(page_num)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out_file = os.path.join(target_folder, f"{prefix}_{page_num + 1:03d}.{img_fmt}")
            pix.save(out_file)
        doc.close()
        return total_pages

    def _word_to_pdf_with_fallback(self, file_path, temp_pdf):
        temp_in = os.path.normpath(os.path.join(tempfile.gettempdir(), f"in_word_{os.getpid()}.docx"))
        shutil.copy2(file_path, temp_in)
        try:
            os.remove(temp_in + ":Zone.Identifier")
        except Exception:
            pass

        success = False
        last_err = None
        for prog in ["Word.Application", "KWPS.Application"]:
            try:
                app = win32com.client.DispatchEx(prog)
                app.Visible = False
                app.DisplayAlerts = 0
                doc = app.Documents.Open(temp_in, ReadOnly=True)
                doc.SaveAs(temp_pdf, FileFormat=17)
                doc.Close(SaveChanges=0)
                app.Quit()
                success = True
                break
            except Exception as e:
                last_err = e
                try:
                    app.Quit()
                except Exception:
                    pass
                continue
        if os.path.exists(temp_in):
            try:
                os.remove(temp_in)
            except Exception:
                pass
        if not success:
            raise RuntimeError(f"Word 转临时 PDF 失败: {str(last_err)}")

    def _ppt_to_images_direct(self, ppt_path, target_folder, dpi, img_fmt):
        temp_in = os.path.normpath(os.path.join(tempfile.gettempdir(), f"in_ppt_{os.getpid()}.pptx"))
        shutil.copy2(ppt_path, temp_in)
        try:
            os.remove(temp_in + ":Zone.Identifier")
        except Exception:
            pass

        success = False
        last_err = None
        slide_count = 0

        progs = ["PowerPoint.Application", "Kwpp.Application", "KET.Application"]
        for prog in progs:
            presentation = None
            try:
                app = win32com.client.DispatchEx(prog)
                app.DisplayAlerts = 1
                try:
                    presentation = app.Presentations.Open(
                        FileName=temp_in, ReadOnly=True, Untitled=False, WithWindow=False
                    )
                except Exception:
                    if presentation:
                        try:
                            presentation.Close()
                        except Exception:
                            pass
                    presentation = app.Presentations.Open(
                        temp_in, True, False, True
                    )

                scale = dpi / 72.0
                width = int(presentation.PageSetup.SlideWidth * scale)
                height = int(presentation.PageSetup.SlideHeight * scale)
                export_fmt = "JPG" if img_fmt.lower() in ["jpg", "jpeg"] else "PNG"

                for i, slide in enumerate(presentation.Slides, 1):
                    out_file = os.path.join(target_folder, f"slide_{i:03d}.{img_fmt}")
                    slide.Export(out_file, export_fmt, width, height)
                    slide_count += 1

                if presentation:
                    presentation.Close()
                app.Quit()
                success = True
                break
            except Exception as e:
                last_err = e
                if presentation:
                    try:
                        presentation.Close()
                    except Exception:
                        pass
                try:
                    app.Quit()
                except Exception:
                    pass
                continue

        if os.path.exists(temp_in):
            try:
                os.remove(temp_in)
            except Exception:
                pass

        if not success:
            raise RuntimeError(f"PPT 直接导出图片失败: {str(last_err)}")
        return slide_count


if __name__ == "__main__":
    root = tk.Tk()
    app = OfficeToImageApp(root)
    root.mainloop()