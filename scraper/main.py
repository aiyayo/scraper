# main.py —— 程式入口：python main.py（GUI）或 python main.py --cli ...（無介面）
import os
import sys

# 與 v4 相同：忽略系統代理環境變數（需要代理請在介面『HTTP 代理』欄位填寫）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ[_k] = ""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def run_gui():
    import tkinter as tk
    from tkinter import ttk
    from gui.app import App

    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        sys.argv.remove("--cli")
        from cli import main as cli_main
        sys.exit(cli_main())
    run_gui()
