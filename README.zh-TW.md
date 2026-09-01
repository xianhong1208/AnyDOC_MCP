# AnyDoc

**文件格式轉換 MCP 服務。** 上傳一個檔案，拿回你需要的格式：Office ↔ PDF、Markdown ↔ Word、試算表、簡報、圖片、掃描頁面的 OCR，以及 PDF 的分割／抽頁／旋轉／加密 —— 全部以 MCP 工具提供，任何支援 MCP 的 client（Claude Code、Claude Desktop、Cursor、FastMCP client）都能呼叫。

認證委託給 [MCP Center](https://github.com/xianhong1208/MCP_Center)：AnyDoc 是一個 OAuth 2.1 resource server，透過 MCP Center 的 JWKS 離線驗證其 RS256 token。它本身不儲存使用者，也不簽發 token。

[English](README.md) · [快速開始](#快速開始) · [工具](#工具) · [運作方式](#運作方式) · [設定](#設定) · [架構](docs/architecture.md)

## 主要特色

- **八個工具，一套契約。** `convert_document`、`extract_text`、`inspect_document`、`list_supported_conversions`、`pdf_extract_pages`、`pdf_split`、`pdf_rotate`、`pdf_protect`。每個工具都接收同一組 `file_content` / `file_name` / `mime_type` 參數，因此會把上傳檔案以 base64 注入的 host 對所有工具都適用。
- **六個引擎，依 fidelity 挑選。** LibreOffice 負責版面保真的 Office ↔ PDF，Pandoc 負責保留結構的文字格式，[firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) 負責快速的文件 → Markdown 抽取，Tesseract 負責 OCR，Pillow 負責圖片，另有一個輕量文字引擎處理 JSON / XML / TSV。缺少的引擎會在啟動時停用對應路徑，而不是在請求時才失敗。
- **多跳規劃，並說明代價。** 沒有引擎能直接把 A 轉成 B 時，registry 會搜尋最多三跳的路徑並依 fidelity 排序。有損的跳預設允許，但會在結果中標記，讓模型轉告使用者；傳 `allow_quality_loss=false` 則拒絕任何會失去版面的路徑。
- **沒有檔名也能偵測格式。** 副檔名 → MIME → 容器內部檢查（ZIP 目錄 / OLE2 stream）→ 檔頭魔數。十六種格式在完全沒有檔名與 MIME type 的情況下仍能正確辨識。
- **標準化認證。** Bearer token 依 MCP Center 的 JWKS 驗證（issuer、audience、可選的 scope）；伺服器發布 `/.well-known/oauth-protected-resource/mcp`，讓支援 OAuth 的 client 找到登入入口。
- **到處都能跑。** 一個內建所有引擎的 Docker image，或直接 `uv run`。Windows 桌面版（Tkinter GUI，不需伺服器）在 [`desktop/`](desktop/README.md)。

## 安裝

**前置需求：** Python 3.13+ 與 [uv](https://docs.astral.sh/uv/)。Python 引擎（`firecrawl-anydoc`、`pypdf`、`pillow`）由 `uv sync` 安裝；其餘為系統套件：

```bash
# Debian / Ubuntu
sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress \
                        pandoc tesseract-ocr tesseract-ocr-chi-tra tesseract-ocr-chi-sim \
                        tesseract-ocr-eng poppler-utils fonts-noto-cjk fonts-dejavu-core
```

`fonts-noto-cjk` 不可省略：少了它，LibreOffice 產出的 PDF 中 CJK 文字會變成 `□□□`，而且回報成功。Tesseract 語言包同樣不可省略 —— 缺少語言包會讓 Tesseract 直接無法啟動，而不是降級處理。

```bash
git clone https://github.com/xianhong1208/AnyDOC_MCP.git
cd AnyDOC_MCP
uv sync
```

或者完全略過系統套件，改用內建所有引擎的容器：

```bash
docker build -t anydoc .
docker run -p 5055:5055 -e MCP_CENTER_URL=http://mcp-center:4568 anydoc
```

## 快速開始

### 1. 啟動 MCP Center 並註冊 AnyDoc

執行 [MCP Center](https://github.com/xianhong1208/MCP_Center)（預設 `http://localhost:4568`），註冊一個指向本伺服器的服務：host `127.0.0.1`、port `5055`、path `/mcp`。MCP Center 會從這筆註冊推導 token 的 audience（`http://127.0.0.1:5055/mcp`）；AnyDoc 則從 `ANYDOC_BASE_URL` 推導出同一個值，所以兩邊只要 host 與 port 一致即可。

### 2. 啟動 AnyDoc

```bash
cp .env.example .env          # defaults already point at http://localhost:4568
uv run python main.py
```

啟動日誌會列出可用的引擎。開啟 `http://localhost:5055/` 查看首頁，`http://localhost:5055/docs` 查看 REST API。

### 3. 連接 client

從 MCP Center 的服務頁面複製現成的設定片段，或手動設定：

```bash
# Claude Code (OAuth: completes sign-in through MCP Center on first use)
claude mcp add --transport http anydoc http://localhost:5055/mcp

# Any client with a personal access token issued by MCP Center
claude mcp add --transport http anydoc http://localhost:5055/mcp \
  --header "Authorization: Bearer <token>"
```

### 4. 驗證

```bash
curl -s http://localhost:5055/.well-known/oauth-protected-resource/mcp   # points at MCP Center
curl -s http://localhost:5055/.well-known/oauth-authorization-server      # 轉發 MCP Center 的 metadata,給較舊的 client 用
curl -i -X POST http://localhost:5055/mcp                                  # 401 without a token
```

若要在沒有 MCP Center 的情況下本機實驗，改用 `config/config.test.yaml`（關閉認證）並執行協定 smoke test：

```bash
SERVER_PORT=5056 uv run python main.py --config config/config.test.yaml
uv run python scripts/mcp_smoke.py
```

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

不支援：音訊、視訊、壓縮檔、CAD、可執行檔。工具描述與 [`config/instructions.md`](config/instructions.md) 會告訴模型直接說明不支援，而不是反覆重試。

## 運作方式

```
client ──(bearer token)──▶ /mcp ──▶ tools ──▶ service.py ──▶ registry.plan_conversion() ──▶ engines
                             │
                             └── JWTVerifier(jwks_uri = MCP_CENTER_URL/.well-known/jwks.json)
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
| **Audience** | token 中的 resource URI；必須與 MCP Center 為本伺服器註冊的值相同。 |

引擎表、規劃器的規則與格式偵測的細節在 [docs/architecture.md](docs/architecture.md)。

## 設定

所有設定都在 [`config/config.yaml`](config/config.yaml)，以 `${VAR:-default}` 讀取環境變數。[`.env.example`](.env.example) 列出了這些變數：

| 變數 | 預設值 | 說明 |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5055` | 綁定位址。 |
| `MCP_CENTER_URL` | `http://localhost:4568` | 簽發 token 的 MCP Center（`auth.issuer`）。 |
| `ANYDOC_BASE_URL` | `http://127.0.0.1:5055` | 本伺服器對外的位址。token 的 audience 為 `<ANYDOC_BASE_URL>/mcp`，必須等於在 MCP Center 註冊的服務；也會出現在 protected-resource metadata 中。 |
| `AUTH_ENABLED` | `true` | 只有本機實驗時才設為 `false`。 |
| `MAX_INPUT_MB` | `50` | 單檔上限；同時提高 MCP 傳輸層的 body 上限（×1.5）。 |
| `ENGINE_TIMEOUT` | `180` | 每次引擎呼叫的秒數上限。 |
| `LOG_LEVEL` | `INFO` | |

YAML 中的 `auth.required_scopes` 可要求每個 token 都必須帶有某個 scope（例如 `mcp:tools:invoke`）；`auth.audience` 則可在註冊的 resource URI 不是 `<base_url>/mcp` 的少數情況下覆寫推導值。

## 測試

```bash
uv run pytest                              # logic, < 1 s, engines are mocked
uv run python scripts/sweep_routes.py      # every advertised path against real engines, ~70 s
uv run python scripts/mcp_smoke.py         # protocol check with a real MCP client (server on :5056)
```

三層各自回答不同的問題 —— 邏輯對不對、引擎是否真的產出有效檔案、client 是否真的能使用這些工具 —— 而每一層都抓到過其他層抓不到的 bug。

## 已知限制

- 一次呼叫只處理一個檔案（尚無 `pdf_merge` 工具；合併邏輯已在 `pdfops` 中實作）。
- 單檔 50 MB，每次 OCR 最多 30 頁。
- PDF → 可編輯格式一定有損；PDF 內的表格以啟發式方式還原。
- 圖片會保留，但附加在抽取出的 Markdown 末尾，而非原本的位置。
- 不支援 `.xlsb`。

## 貢獻

歡迎回報問題與提交 pull request。註解與 docstring 請使用英文，開 PR 前執行 `uv run ruff check .` 與 `uv run pytest`，修改 registry 或升級引擎後請執行 `scripts/sweep_routes.py`。

## 授權

[MIT](LICENSE)
