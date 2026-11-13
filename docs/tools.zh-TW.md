# 工具與格式

[English](tools.md) · [README](../README.zh-TW.md)

## 工具

| 工具 | 用途 | 回傳 |
|---|---|---|
| `convert_document` | 轉換成目標格式 | 檔案 |
| `extract_text` | 抽取內容為 Markdown 供模型閱讀 | 文字 |
| `inspect_document` | 格式、大小、頁數與可到達的目標格式 | 文字 |
| `list_supported_conversions` | 能力矩陣（只含已安裝的引擎） | 文字 |
| `pdf_extract_pages` | 抽頁或重排頁序 | 檔案 |
| `pdf_split` | 切分成多個檔案 | 多個檔案 |
| `pdf_rotate` | 旋轉指定頁面 | 檔案 |
| `pdf_protect` | AES-256 加密 | 檔案 |

**支援的格式**

| 類別 | 格式 |
|---|---|
| 文件 | pdf, docx, doc, odt, rtf, epub |
| 試算表 | xlsx, xls, ods, csv, tsv |
| 簡報 | pptx, ppt, odp |
| 文字 | md, html, txt, rst, tex, json, xml |
| 圖片 | png, jpg, webp, gif, bmp, tiff |

不支援：音訊、視訊、壓縮檔、CAD、可執行檔。工具描述與 [`config/instructions.md`](../config/instructions.md) 會告訴模型直接說明不支援，而不是反覆重試。

## 運作方式

```
client ──(bearer token)──▶ /mcp ──▶ tools ──▶ service.py ──▶ registry.plan_conversion() ──▶ engines
                             │
                             └── JWTVerifier(jwks_uri = MCP_GATE_URL/.well-known/jwks.json)
```

1. **輸入。** 工具接收 `file_content`（base64）、`file_name` 與 `mime_type`。管理上傳檔案的 host 通常會自動注入 base64；參數描述的措辭刻意引導模型只把上傳參照放進 `file_content`，不放在其他地方。
2. **偵測。** `detect_format()` 依序嘗試副檔名、MIME、容器內部結構、檔頭魔數 —— 因為實務上檔名經常缺席。
3. **規劃。** `plan_conversion()` 找出直接引擎或最多三跳的路徑，依 fidelity、跳數與中繼格式偏好排序。直接路徑永遠不會被拒絕。低於 `structural` fidelity 的多跳路徑會照轉並附上警告；`allow_quality_loss=false` 時則改為拒絕。
4. **執行。** 引擎以子程序在暫存工作區中執行並設有逾時；掃描版 PDF 會自動從文字抽取 fallback 到 OCR。
5. **輸出。** 工具回傳 `[摘要文字, File(...)]`；檔案以 MCP `EmbeddedResource` 形式送達。

| 術語 | 意義 |
|---|---|
| **Fidelity** | `high`（保留版面）、`structural`（保留標題／清單／表格）、`lossy`（只有文字）。驅動路徑規劃。 |
| **Hop** | 多步驟路徑中的一次引擎呼叫。 |
| **Audience** | token 中的 resource URI；必須與 MCP Gate 為本伺服器註冊的值相同。 |

引擎表、規劃器的規則與格式偵測的細節在 [docs/architecture.md](architecture.md)。

## 已知限制

- 一次呼叫只處理一個檔案（尚無 `pdf_merge` 工具；合併邏輯已在 `pdfops` 中實作）。
- 單檔 50 MB，每次 OCR 最多 30 頁。
- 同時執行的轉換數有上限（預設為 CPU 數，最多 8）；其餘排隊等待，超過佇列長度或等待時間時，工具會回答「伺服器忙碌」，稍後再呼叫即可。見[同時轉換](configuration.zh-TW.md#同時轉換)。
- PDF → 可編輯格式一定有損；PDF 內的表格以啟發式方式還原。
- 圖片會保留，但附加在抽取出的 Markdown 末尾，而非原本的位置。
- 不支援 `.xlsb`。
- LibreOffice 轉檔同樣不會讀取文件所連結的檔案或網址：以路徑或網址連結的圖片（HTML 的 `<img>`、DOCX/XLSX/PPTX 中標為 external 的 relationship、ODF 的 `xlink:href`、RTF 的 `INCLUDEPICTURE` 欄位）不會出現在輸出中，樣式表也不會被下載。每次轉檔都使用一個會封鎖不受信任文件連結、並把所有 HTTP/HTTPS/FTP 流量導向不存在之 proxy 的 LibreOffice 設定檔；ODF、OOXML 與 RTF 文件還會先移除指向外部的參照（依內容判斷，所以改名成 `.pptx` 的 ODP 仍當作 ODP 處理）。文件內嵌的圖片會保留。無法完整讀取以進行檢查的 zip 類文件會被拒絕，而不是照樣轉檔。
- Pandoc 轉檔不會讀取文件所指向的檔案或網址：LaTeX 的 `\input` / `\lstinputlisting` / `\includegraphics`，以及用路徑或 `http(s)` 網址引用的圖片都會被略過（圖片保留 alt 文字）；文件內嵌或以 `data:` URI 提供的圖片會保留。
- 撤銷不是即時的：啟用 [revocation feed](configuration.zh-TW.md#token-撤銷) 時，被撤銷的 token 會在一個輪詢間隔內失效（預設 30 秒）；未啟用時則要等到過期。
