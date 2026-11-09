# AnyDoc

**文件格式轉換 MCP 服務。** 上傳一個檔案，拿回你需要的格式：Office ↔ PDF、Markdown ↔ Word、試算表、簡報、圖片、掃描頁面的 OCR，以及 PDF 的分割／抽頁／旋轉／加密 —— 全部以 MCP 工具提供，任何支援 MCP 的 client（Claude Code、Claude Desktop、Cursor、FastMCP client）都能呼叫。

認證委託給 [MCP Gate](https://github.com/xianhong1208/MCP_Gate)：AnyDoc 是一個 OAuth 2.1 resource server，透過 MCP Gate 的 JWKS 離線驗證其 RS256 token。它本身不儲存使用者，也不簽發 token。

[English](README.md) · [快速開始](#快速開始) · [工具](#工具) · [運作方式](#運作方式) · [設定](#設定) · [架構](docs/architecture.md)

## 主要特色

- **八個工具，一套契約。** `convert_document`、`extract_text`、`inspect_document`、`list_supported_conversions`、`pdf_extract_pages`、`pdf_split`、`pdf_rotate`、`pdf_protect`。每個工具都接收同一組 `file_content` / `file_name` / `mime_type` 參數，因此會把上傳檔案以 base64 注入的 host 對所有工具都適用。
- **六個引擎，依 fidelity 挑選。** LibreOffice 負責版面保真的 Office ↔ PDF，Pandoc 負責保留結構的文字格式，[firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) 負責快速的文件 → Markdown 抽取，Tesseract 負責 OCR，Pillow 負責圖片，另有一個輕量文字引擎處理 JSON / XML / TSV。缺少的引擎會在啟動時停用對應路徑，而不是在請求時才失敗。
- **多跳規劃，並說明代價。** 沒有引擎能直接把 A 轉成 B 時，registry 會搜尋最多三跳的路徑並依 fidelity 排序。有損的跳預設允許，但會在結果中標記，讓模型轉告使用者；傳 `allow_quality_loss=false` 則拒絕任何會失去版面的路徑。
- **沒有檔名也能偵測格式。** 副檔名 → MIME → 容器內部檢查（ZIP 目錄 / OLE2 stream）→ 檔頭魔數。十六種格式在完全沒有檔名與 MIME type 的情況下仍能正確辨識。
- **標準化認證。** Bearer token 依 MCP Gate 的 JWKS 驗證（issuer、audience、可選的 scope）；伺服器發布 `/.well-known/oauth-protected-resource/mcp`，讓支援 OAuth 的 client 找到登入入口。
- **到處都能跑。** 一個內建所有引擎的 Docker image，或直接 `uv run`。

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
docker run -p 5055:5055 -e MCP_GATE_URL=http://mcp-gate:4568 anydoc
```

## 快速開始

### 1. 啟動 MCP Gate 並註冊 AnyDoc

執行 [MCP Gate](https://github.com/xianhong1208/MCP_Gate)（預設 `http://localhost:4568`），註冊一個指向本伺服器的服務：host `127.0.0.1`、port `5055`、path `/mcp`。MCP Gate 會從這筆註冊推導 token 的 audience（`http://127.0.0.1:5055/mcp`）；AnyDoc 則從 `ANYDOC_BASE_URL` 推導出同一個值，所以兩邊只要 host 與 port 一致即可。

### 2. 啟動 AnyDoc

```bash
cp .env.example .env          # defaults already point at http://localhost:4568
uv run python main.py
```

啟動日誌會列出可用的引擎。開啟 `http://localhost:5055/` 查看首頁，`http://localhost:5055/docs` 查看 REST API。

### 3. 連接 client

從 MCP Gate 的服務頁面複製現成的設定片段，或手動設定：

```bash
# Claude Code (OAuth: completes sign-in through MCP Gate on first use)
claude mcp add --transport http anydoc http://localhost:5055/mcp

# Any client with a personal access token issued by MCP Gate
claude mcp add --transport http anydoc http://localhost:5055/mcp \
  --header "Authorization: Bearer <token>"
```

### 4. 驗證

```bash
curl -s http://localhost:5055/.well-known/oauth-protected-resource/mcp   # points at MCP Gate
curl -s http://localhost:5055/.well-known/oauth-authorization-server      # 轉發 MCP Gate 的 metadata,給較舊的 client 用
curl -i -X POST http://localhost:5055/mcp                                  # 401 without a token
```

若要在沒有 MCP Gate 的情況下本機實驗，改用 `config/config.test.yaml`（關閉認證）並執行協定 smoke test：

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

引擎表、規劃器的規則與格式偵測的細節在 [docs/architecture.md](docs/architecture.md)。

## 設定

所有設定都在 [`config/config.yaml`](config/config.yaml)，以 `${VAR:-default}` 讀取環境變數。[`.env.example`](.env.example) 列出了這些變數：

| 變數 | 預設值 | 說明 |
|---|---|---|
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5055` | 綁定位址。 |
| `MCP_GATE_URL` | `http://localhost:4568` | 簽發 token 的 MCP Gate（`auth.issuer`）。未設定時仍會讀取改名前的 `MCP_CENTER_URL`。 |
| `ANYDOC_BASE_URL` | `http://127.0.0.1:5055` | 本伺服器對外的位址。token 的 audience 為 `<ANYDOC_BASE_URL>/mcp`，必須等於在 MCP Gate 註冊的服務；也會出現在 protected-resource metadata 中。 |
| `AUTH_ENABLED` | `true` | 只有本機實驗時才設為 `false`。 |
| `MAX_INPUT_MB` | `50` | 單檔上限；同時提高 MCP 傳輸層的 body 上限（×1.5）。 |
| `ENGINE_TIMEOUT` | 各引擎預設 | 每次引擎呼叫的秒數上限，套用到所有引擎。未設定時各引擎維持自己的上限：LibreOffice 180、Pandoc 120、OCR 300。 |
| `LOG_LEVEL` | `INFO` | |

YAML 中的 `auth.required_scopes` 可要求每個 token 都必須帶有某個 scope（例如 `mcp:tools:invoke`）；`auth.audience` 則可在註冊的 resource URI 不是 `<base_url>/mcp` 的少數情況下覆寫推導值。

### Token 撤銷

AnyDoc 以離線方式驗證 MCP Gate 的 token，因此預設情況下，在 MCP Gate 撤銷的 token 在這裡會一直有效到過期為止——而 personal access token 最長可活一年。開啟 **revocation feed** 後，AnyDoc 會在背景輪詢 MCP Gate 的 `POST /oauth/revoked`（每 `poll_interval_sec` 秒，帶 jitter 與錯誤退避），在記憶體保存「已撤銷但尚未過期」的 `jti`，並對 `/mcp` 上的這些 token 回應 401 `invalid_token`。撤銷約在一個輪詢間隔內生效。

| 變數 / 設定 | 預設 | 說明 |
|---|---|---|
| `ANYDOC_REVOCATION_ENABLED`（`auth.revocation.enabled`） | `false` | 開啟 feed。關閉 = 不輪詢。 |
| `ANYDOC_REVOCATION_CLIENT_ID` | — | 在 MCP Gate 註冊的 resource-server client（見下方）。 |
| `ANYDOC_REVOCATION_CLIENT_SECRET` | — | 它的 secret。在 YAML 中**必須**維持 `${ENV_VAR}` 參照；寫成字面值會拒絕啟動。 |
| `ANYDOC_REVOCATION_POLL_INTERVAL`（`poll_interval_sec`） | `30` | 輪詢間隔秒數（5–3600）。 |
| `ANYDOC_REVOCATION_FAIL_MODE`（`fail_mode`） | `open` | MCP Gate 連不上時：`open` 繼續接受 token 並大聲記錄；`closed` 在連續 `stale_after_intervals` × `poll_interval_sec`（YAML，預設 4）沒有成功輪詢後拒絕所有 token，啟動後第一次輪詢成功前也一樣。 |

第一次輪詢抓完整清單；之後只問上次以來的撤銷（重疊 30 秒），每小時再完整抓一次。token 本來就會過期之後，項目即被移除。啟動時會記錄 feed 是否啟用，`GET /health` 的 `revocation` 會顯示 `last_success`、`revoked_count`、`consecutive_failures`、`last_error`、`stale`；feed 過期時 `status` 會變成 `degraded`。

**設定步驟**（每個部署做一次）：

1. **在 MCP Gate 註冊 confidential client。** 主控台 → *OAuth Clients* → *Register trusted client*。名稱例如 `AnyDoc revocation feed`；*Redirect URIs* 留空；*Grant types* 只選 `client_credentials`；*Token endpoint authentication*：**Client secret (POST body)**（HTTP Basic 也可以，MCP Gate 兩者都接受）；*Resource server for*：**AnyDoc**。註冊後複製 `client_id`（`mcpc_…`）與 client secret——secret 只顯示一次。
2. **綁定到 AnyDoc**——若步驟 1 已選 *Resource server for* 就已完成。否則在該 client 那一列點伺服器圖示（*Resource server…*），選 AnyDoc 服務後 *Save*；或以主控台管理員呼叫 `PUT /api/oauth/clients/<client_id>/resource-server`，body 為 `{"service_id": "<AnyDoc service id>"}`。該列會顯示 *Resource server for AnyDoc*。feed 只列出 audience 等於該服務 audience 的 token，所以它必須等於 `<ANYDOC_BASE_URL>/mcp`。
3. **把憑證交給 AnyDoc**（環境變數或 `.env`），然後重啟：
   ```bash
   ANYDOC_REVOCATION_ENABLED=true
   ANYDOC_REVOCATION_CLIENT_ID=mcpc_...
   ANYDOC_REVOCATION_CLIENT_SECRET=...
   ```
4. **確認。** 啟動 log 會出現 `MCP Gate revocation feed: ENABLED …`；`curl -s localhost:5055/health` 會看到最近的 `revocation.last_success`。在 MCP Gate 撤銷一個測試 token，`poll_interval_sec` 內用它呼叫 `/mcp` 會得到 401。`last_error` 出現 `HTTP 401: invalid_client` 表示憑證錯誤；`HTTP 403: unauthorized_client` 表示 client 尚未綁定伺服器。

## 測試

```bash
uv run pytest                              # logic, < 1 s, engines are mocked
uv run python scripts/sweep_routes.py      # every advertised path against real engines, ~70 s
uv run python scripts/mcp_smoke.py         # protocol check with a real MCP client (server on :5056)
```

三層各自回答不同的問題 —— 邏輯對不對、引擎是否真的產出有效檔案、client 是否真的能使用這些工具 —— 而每一層都抓到過其他層抓不到的 bug。

CI(`.github/workflows/ci.yml`)在每次 push 與對 `dev`、`main` 的 pull request 時跑這三層:先在 runner 上跑 ruff 與 pytest,再 build Docker image,並在容器內跑 `sweep_routes.py --strict`(被跳過的路線也算失敗)與 `mcp_smoke.py`,所以檢查到的引擎與字型就是實際出貨的那一套。

## 已知限制

- 一次呼叫只處理一個檔案（尚無 `pdf_merge` 工具；合併邏輯已在 `pdfops` 中實作）。
- 單檔 50 MB，每次 OCR 最多 30 頁。
- PDF → 可編輯格式一定有損；PDF 內的表格以啟發式方式還原。
- 圖片會保留，但附加在抽取出的 Markdown 末尾，而非原本的位置。
- 不支援 `.xlsb`。
- 撤銷不是即時的：啟用 [revocation feed](#token-撤銷) 時，被撤銷的 token 會在一個輪詢間隔內失效（預設 30 秒）；未啟用時則要等到過期。

## 貢獻

歡迎回報問題與提交 pull request。註解與 docstring 請使用英文，開 PR 前執行 `uv run ruff check .` 與 `uv run pytest`，修改 registry 或升級引擎後請執行 `scripts/sweep_routes.py`。

## 授權

[MIT](LICENSE)
