# 統一文檔採集與智能處理系統 v5

由 v4 的「生成器脚本 + 1500 行 main.py」拆成真正的多模块项目。

```
scraper_v5/
├─ main.py              入口：python main.py（GUI）｜python main.py --cli ...（无界面）
├─ cli.py               无界面模式
├─ runner.py            任务调度（搜索→下载→后处理 / 扫描 / 导出 / 校验），GUI 与 CLI 共用
├─ core/
│  ├─ constants.py      选择器、UA、扩展名、R2 路径前缀
│  ├─ config.py         设置读写（原子保存、环境变量、自动迁移 v4 设置）
│  ├─ utils.py          通用工具（md5、分层路径、文件日志…）
│  └─ db.py             SQLite
├─ crawler/
│  ├─ search.py         Yahoo 搜索（Selenium）
│  └─ downloader.py     下载器
├─ processing/
│  ├─ extractor.py      PDF/Office → WebP + 每页文字（调度）
│  ├─ layout.py         PDF 版面还原（参考 word.py：多栏顺序/表格/去页眉页脚/段落合并/插图检测）
│  ├─ structure.py      结构还原：大题→题号→选项→答案，渲染 Markdown（单行并排/逐行并排）、题库
│  ├─ ocr.py            PaddleOCR（2.x/3.x）+ Tesseract 备援
│  ├─ docx_reader.py    docx 结构化读取（参考 word.py：标题/自动编号/表格/上下标/分页）
│  ├─ textutil.py       文字清洗与 Markdown 表格
│  ├─ ai_client.py      阿里云百炼（智能体）
│  ├─ r2_client.py      Cloudflare R2
│  ├─ pipeline.py       单文档流水线 + JSON 构建（工作台共用）
│  ├─ publish_client.py 步驟⑤帝國CMS 發布（標籤映射/form-gbk/縮略圖/歸檔，唯一 POST 出口）
│  └─ proc_manager.py   后处理队列与工作线程
├─ gui/                 app.py + 6 个选项卡（含独立的「发布接口」页）+ widgets.py
└─ tests/                test_core.py、test_layout.py、test_structure.py（无需 PyMuPDF / PaddleOCR，用模拟对象）
```

## 运行
```
pip install -r requirements.txt
python main.py
python main.py --cli --mode pipeline -k "四年級數學教案" --pages 3 --headless
```
首次启动会自动读取旧的 `user_settings_v4.json`。密钥也可用环境变量
`DASHSCOPE_API_KEY` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` 提供。

## 文档转换（参考 word.py）
每个文档会得到：**每页文字**（`pages[].content`）、**整份结构化 Markdown**（`markdown`），程序内部仍会解析题库
（`questions`，工作台重排/查看用），插图另存为 WebP 并绑定位置（`figures`）。设置在 ☁️ 选项卡里，均可逐项关闭。
**注意：导出的 JSON 已精简**——不再写入 `questions`、`local_file_path`，每页与插图也不再写本机路径 `local_image`（只保留 R2 URL）。

**版面还原（PDF）**：按行提取并保留字号/粗体/上下标（`<sup>`/`<sub>`）；通栏标题分带 + 带内双栏/多栏阅读顺序；
原生表格转 Markdown（过滤单行/单列误检）；跨页重复的页眉页脚和页码自动去除；斜向水印丢弃、叠印重影行去重；
独立成块的题号 `( )` `1.` 吸附到正文；折行合并成段落；全形英数转半形、去除中文间多余空格、规范填空括号/下划线。
结果为空或比纯文字短太多（疑似误删）时，自动退回 PyMuPDF 纯文字。

**智能 Markdown 还原**：标题（字号）、抬头信息（班级/姓名…）、大题（一、选择题…）、题号（`1.` `(　)1.` `第1題` `【1】`）、
选项链（`(A)(B)…` `①②…` `(1)(2)…`，含同一行多个选项）、题组说明、答案行/答案表、题内表格，一起还原成
`# 标题` / `### 大题` / `**1.** 题干` / 选项。没有题号的普通文档会退化成标题 + 段落 + 表格 + 插图。

**选项排版**（设置里选，工作台里可随时切换并重新生成，不必重跑 OCR）：
- **逐行并排**：每个选项一行 `- (A) …`（默认）
- **单行并排**：选项同一行 `(A) 4　　(B) 6　　(C) 7　　(D) 9`
- **智能**：短选项（≤4 个、每个≤12 字、合计≤40 字）单行，长选项逐行

**插图绑定**：检测嵌入图 + 向量图（相邻合并），连同坐标轴/角标等短标签一起裁切成 `{二次md5}_p{页}_f{序}.webp`（3 倍渲染）；
图在题干左右并排时绑定到**下一题**，放在题干之后、选项之前（`![第2題插圖1](…)`）；页面文字里也按位置内嵌。
插图随原档、页面图一起上传 R2，Markdown 里的图片路径在设置了公开域名时自动加前缀。

**PaddleOCR**（`pip install paddlepaddle paddleocr`，同时支持 2.x 和 3.x）：只处理「没有文字层的扫描页」和「文字层乱码的页」，
有文字层的页不会跑 OCR；OCR 结果同样走版面还原 → 结构还原 → Markdown。语言：`ch`（简繁中英）/ `chinese_cht`（繁体）/ `en`；
低于信心度（默认 0.5）的文字丢弃；模型只初始化一次，推论加锁；没装 PaddleOCR 时只警告一次，不影响其他流程。
文档工作台有「🔍 OCR 当前页」按钮。Tesseract 可选作备援。

**Office / WPS 转图（仅 Windows，逻辑取自 word.py）**：
- Word（doc/docx）、Excel（xls/xlsx）：优先调用微软 Office COM（`Word.Application` / `Excel.Application`），
  没装时自动改用 WPS（`KWPS.Application` / `Ket.Application`）转暂存 PDF，再走 PDF 渲染流程（真实分页 + 真实版面图）。
- PowerPoint（ppt/pptx）：`PowerPoint.Application` → `Kwpp.Application` 备援，逐页 `Slide.Export` 直出 PNG（免 PDF 中转）；
  pptx 文字由 XML 提取，.ppt 文字由 COM 提取。
- 原档先复制到纯英文暂存路径并去除下载标记，避免受保护视图拦截；转换失败时 docx/pptx/xlsx 退回结构化纯文字 + 中文示意图，
  doc/ppt/xls 则提示需安装 Office 或 WPS。需 `pip install pywin32`。
- **最大图片宽度**：设置「最大寬度(px,0=不限)」（`webp_max_width`），渲染倍率超过该宽度时等比缩小，PPT 直出页同样受限。

**docx 纯文字备援**：未走 COM（或转换失败）时按文档顺序读取段落和表格，
还原标题级别、Word 自动编号（1. / 一、 / (A) / ①）、文本框、页眉、上下标，并按分页符/字数切页。

JSON 字段：`title`、`summary`、`keywords`、`file_size`、`file_type`、`total_pages`、`page_count`、`md5`、
`orig_name`、`file_path`（R2 键）、`file_url`（原檔完整公網 URL）、`json_url`（JSON 完整公網 URL）、
`pages`（page/image/width/height/content/figures）、`images`、`markdown`、
`figures`（image/name/page，不含本机路径）、`processed_at`。题库 `questions` 与 `local_image` 不再写入 JSON。

## 發布接口（獨立選項卡：帝國 CMS，即流水線步驟⑤）
『📤 6. 發布接口（帝國CMS·步驟⑤）』頁是流水線**唯一的資料回傳出口**（R2 上傳後執行；原 R2 頁的純 JSON POST 已移除）：
- **接口地址 / 提交格式 / 編碼**：form(`application/x-www-form-urlencoded`) 或 json；頁面編碼 utf-8 / gbk（自動轉碼與 `%XX` 編碼）。
- **動態字段映射表**：Treeview 選中即可改/刪，「添加/更新此字段」新增；左列 POST 字段名（title、classid、smalltext、titlepic、newstext…），
  右列寫動態標籤或固定值（如 classid 填 `1`）。
- **可用標籤**：[标题]、[内容]、[摘要]、[关键词]、[账号名]、[文章ID]、[发布时间]、[发布时间戳]、[缩略图路径]、
  [嵌入代码]、[嵌入iframe]、[iframe地址]、[原文网址]；以及 JSON 欄位標籤
  [md5]、[orig_name]、[file_path]、[file_url]、[json_url]、[file_size]、[file_type]、[total_pages]、[page_count]、[images]。
- **[内容]** 會把 Markdown 深度渲染成 HTML：標題/段落/粗體/列表/表格，圖片自動補上 payload 裡的 width/height 屬性；
  Markdown 正文未包含的逐頁掃描圖會自動按頁序補到文末，帝國文章內即可看完整份掃描件。
- **縮略圖**：可配置尺寸（預設 312×208），用首頁 WebP 中心裁切出 JPG，相對路徑 `/d/file/pic/月日/{md5}_thumb.jpg` 對應 [缩略图路径]。
- **成功標記**：回應本文命中任一關鍵字（發送成功/編輯成功/增加信息成功/SUCCESS，逗號分隔可改）才視為成功。
- **歸檔備份**：發布成功後把縮略圖與配圖複製到「歸檔根目錄/d/file/pic/月日/」相同結構，可勾選歸檔後刪除本地原始文件。
- 頁內「🔌 用範例資料測試發布」可即時驗證接口（背景執行緒，彈窗顯示 HTTP 狀態、回應內容與是否命中成功標記）。

## 五步流水線
採集 ① 下載 → ② 處理（WebP + 文字/Markdown/插圖）→ ③ 阿里雲清洗（標題/簡介/關鍵詞）→ ④ Cloudflare R2 上傳 → ⑤ **POST 發布帝國 CMS**。
- 步驟⑤的接口、欄位映射、編碼、成功標記、歸檔全部在『📤 6. 發布接口（帝國CMS·步驟⑤）』頁設定；勾選「步驟 ⑤：POST 發布至帝國 CMS」後隨流水線自動執行，成功後才歸檔配圖。
- 未填接口地址則該文檔標記為「跳過」；發布失敗（含回應未命中成功標記）只記 `pub_status=fail`，不影響文檔生成狀態。
- CLI 用 `--no-publish` 關閉步驟⑤、`--no-process` 關閉步驟②~⑤。
- 工作台『📄 文檔處理與預覽』有「📤 5. 發布到帝國CMS」按鈕，可對單文檔手動重送。
- **四種狀態入庫**：下載（`status`）、生成（`proc_status/proc_at/proc_msg`）、Cloudflare（`r2_status/r2_at`）、
  帝國發布（`pub_status/pub_at/pub_msg`），監控中心可見 Cloudflare / 帝國CMS發布 兩欄；重跑後處理會一併清空 R2/發布舊結果。
- 舊版的 `post_url/post_fields/step5_post_data` 設定會在載入時自動清除；舊開關 `step6_publish_cms` 自動沿用為步驟⑤。

## 修复的问题（相对 v4）
- Yahoo 搜索网址与 Referer 被写成 Markdown 链接 `[https://..](https://..)`，导致搜索/翻页异常。
- 翻页：改用页面「下一页」链接真实 href，退回 `b=` 位移；偏移按实际结果数推进。
- 「包含历史失败任务」会无限重试已达上限的链接。
- https/http 回退的连接错误盖掉真实错误（如 404），同网站失败 3 次被误判「死站」整批跳过。
- 下载完整性：Content-Length 不符判为截断重试（空文件/残缺文件）。
- 并发下同一 MD5 可能重复入库。
- Office 示意图用默认字体画不出中文；R2 原档 Content-Type 一律是 application/pdf。
- 后台线程直接读取 Tk 变量；R2 浏览器操作卡住界面。
- `Wayback 备援` 选项原本没有实现。

## 新增 / 增强
- **单一存储根目录**：设置里只需选择一个根目录（`storage_root`），自动在其下建立 `down/`、`json/`、`pic/`、`db/`；
  **默认就是软件运行目录下的 `data/`**（相对路径按程序目录解析），下载与处理数据不再散落到其他盘。
  采集数据库 `db/downloads.db` 独立成目录，不与下载文件、JSON、图片混放；首次启动若发现旧库 `<根目录>/down/downloads.db` 会自动复制迁移（旧文件保留）。
- **排除域名多行输入**：一行一个域名（粘贴旧的逗号/顿号/分号分隔内容也能识别），自动去 `www.` 前缀并去重。
- **步驟⑤ 發布帝國 CMS**：R2 上傳後依「發布接口」頁的標籤映射把文檔 POST 到帝國 CMS（詳見上節）；
  舊版 R2 頁內建的純 JSON POST（post_client）已移除，資料回傳統一走此出口。
- 下载文件与 JSON 按 `md5前2位/第3位/md5.ext` 分层（如 `down/02/3/023….pdf`，设置里可关）。
- **图片二次 MD5**：pic 的分层与文件名都对原 MD5 再做一次 MD5（`pic/<二次md5前2位>/<第3位>/<二次md5>_p1.webp`），
  拿到图片地址也无法反推原档与 JSON 的位置。
- Windows Office（优先）/ WPS（备援）COM 把 docx/pptx/xlsx/doc/ppt/xls 转 PDF 或直出 PNG，得到真实版面；doc/ppt/xls 不再「不支持」。
- 可设置图片最大宽度（`webp_max_width`，0=不限）。
- 已生成的 WebP 可重用、可限制最大渲染页数、加密 PDF 先试空密码。
- AI：代表页缩图后再送、可配置重试、JSON 解析更宽容。
- R2：已存在同大小则跳过（可重复执行）、多图并行上传、批量删除、列表分页、先传图后传 JSON。
- 流水线结束后自动补处理「已下载但未处理」的文件。
- 采集选项自动保存/还原；每日滚动日志写入 `下载目录/logs/`。
- 监控中心右键菜单（重新下载/删除记录/打开网址/重置某网站失败项）、失败原因分布、
  「校验本地文件」（缺失/空文件改回待下载）、报表导出 4 份 CSV。
- `--cli` 无界面模式。

## 说明
- 本地目录统一为 `<根目录>/down`、`<根目录>/json`、`<根目录>/pic`、`<根目录>/db`（采集库独立）；
  R2 云端键沿用原规格 `d/file/down`、`d/flie/pic`、`d/flie/json`（pic/json 含历史 flie 拼写），统一放在 `core/constants.py`，要改只改一处。
  注意云端 pic 的**文件名**也已改用二次 MD5，但键前缀不变。
