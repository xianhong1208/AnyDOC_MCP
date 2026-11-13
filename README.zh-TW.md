# Morphyard

[![License: MIT](https://img.shields.io/github/license/xianhong1208/morphyard)](LICENSE)
[![CI](https://github.com/xianhong1208/morphyard/actions/workflows/ci.yml/badge.svg?branch=dev)](https://github.com/xianhong1208/morphyard/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/xianhong1208/morphyard)](https://github.com/xianhong1208/morphyard/releases/latest)

**文件格式轉換 MCP 服務：交給它一個檔案，拿回你需要的格式 —— Office ↔ PDF、Markdown ↔ Word、試算表、簡報、圖片、OCR 與 PDF 頁面工具 —— 任何 MCP client 都能用。**

前身為 AnyDoc；從舊版升級請見[改名的變數](docs/configuration.zh-TW.md#改名的變數)。

[English](README.md) · [工具與格式](docs/tools.zh-TW.md) · [設定](docs/configuration.zh-TW.md) · [架構](docs/architecture.md) · [發展路線圖](ROADMAP.zh-TW.md) · [Changelog](CHANGELOG.md)

Morphyard 把各種轉換以 MCP 工具提供，任何支援 MCP 的 client（Claude Code、Claude Desktop、Cursor、FastMCP client）都能呼叫。認證委託給 [MCP Gate](https://github.com/xianhong1208/MCP_Gate)：Morphyard 是一個 OAuth 2.1 resource server，透過 MCP Gate 的 JWKS 離線驗證其 RS256 token。它本身不儲存使用者，也不簽發 token。

## 一次呼叫長什麼樣子

Word 檔以 base64 傳入；PDF 以 MCP embedded resource 回傳，並附上一段可以直接轉述給使用者的摘要。以下取自對本機伺服器的實際執行（FastMCP client）：

```python
await client.call_tool("convert_document", {
    "file_content": "<base64 of report.docx>",
    "file_name": "report.docx",
    "target_format": "pdf",
    "output_name": "report",
})
```

```text
TEXT  Conversion complete: report.pdf
      Path: docx → pdf (engines: libreoffice)
      Size: 23.4 KB
FILE  file:///report.pdf  application/pdf  23963 bytes
```

對同一個檔案呼叫 `extract_text`，則會回傳給模型閱讀的 Markdown：

```markdown
# Quarterly report

| Region | Sales |
|--------|-------|
| North  | 120   |
| South  | 95    |

- North leads this quarter
```

## 最新消息

**1.2.1**（2026-11-09）：LibreOffice 轉檔不再能讀取文件連結的本機檔案或網址，也絕不執行巨集 —— 與 1.2.0 給 Pandoc 轉檔的保證相同；1.2.0 也加入了在 Docker image 裡跑過每一條公開轉換路線的 CI。詳見 [CHANGELOG](CHANGELOG.md)，接下來的計畫見[發展路線圖](ROADMAP.zh-TW.md)。

## 主要特色

- **八個工具，一套契約。** `convert_document`、`extract_text`、`inspect_document`、`list_supported_conversions`、`pdf_extract_pages`、`pdf_split`、`pdf_rotate`、`pdf_protect`。每個工具都接收同一組 `file_content` / `file_name` / `mime_type` 參數，因此會把上傳檔案以 base64 注入的 host 對所有工具都適用。
- **六個引擎，依 fidelity 挑選。** LibreOffice 負責版面保真的 Office ↔ PDF，Pandoc 負責保留結構的文字格式，[firecrawl-anydoc](https://pypi.org/project/firecrawl-anydoc/) 負責快速的文件 → Markdown 抽取，Tesseract 負責 OCR，Pillow 負責圖片，另有一個輕量文字引擎處理 JSON / XML / TSV。缺少的引擎會在啟動時停用對應路徑，而不是在請求時才失敗。
- **多跳規劃，並說明代價。** 沒有引擎能直接把 A 轉成 B 時，registry 會搜尋最多三跳的路徑並依 fidelity 排序。有損的跳預設允許，但會在結果中標記，讓模型轉告使用者；傳 `allow_quality_loss=false` 則拒絕任何會失去版面的路徑。
- **沒有檔名也能偵測格式。** 副檔名 → MIME → 容器內部檢查（ZIP 目錄 / OLE2 stream）→ 檔頭魔數。十六種格式在完全沒有檔名與 MIME type 的情況下仍能正確辨識。
- **文件無法往外伸手。** 轉檔絕不讀取文件指向的本機檔案或網址，LibreOffice 也絕不執行巨集。
- **標準化認證。** Bearer token 依 MCP Gate 的 JWKS 驗證（issuer、audience、可選的 scope）；伺服器發布 `/.well-known/oauth-protected-resource/mcp`，讓支援 OAuth 的 client 找到登入入口。開啟選用的 revocation feed 後，在 MCP Gate 撤銷的 token 會在一個輪詢間隔內（預設 30 秒）於此失效。
- **到處都能跑。** 一個內建所有引擎的 Docker image，或直接 `uv run`。

支援：pdf、docx、doc、odt、rtf、epub；xlsx、xls、ods、csv、tsv；pptx、ppt、odp；md、html、txt、rst、tex、json、xml；png、jpg、webp、gif、bmp、tiff。完整的工具與格式表在 [docs/tools.zh-TW.md](docs/tools.zh-TW.md)。

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
git clone https://github.com/xianhong1208/morphyard.git
cd morphyard
uv sync
```

## 快速開始

### 1. 啟動 MCP Gate 並註冊 Morphyard

執行 [MCP Gate](https://github.com/xianhong1208/MCP_Gate)（預設 `http://localhost:4568`），註冊一個指向本伺服器的服務：host `127.0.0.1`、port `5055`、path `/mcp`。MCP Gate 會從這筆註冊推導 token 的 audience（`http://127.0.0.1:5055/mcp`）；Morphyard 則從 `MORPHYARD_BASE_URL` 推導出同一個值，所以兩邊只要 host 與 port 一致即可。

### 2. 啟動 Morphyard

```bash
cp .env.example .env          # defaults already point at http://localhost:4568
uv run python main.py
```

啟動日誌會列出可用的引擎。開啟 `http://localhost:5055/` 查看首頁，`http://localhost:5055/docs` 查看 REST API。

### 3. 連接 client

從 MCP Gate 的服務頁面複製現成的設定片段，或手動設定：

```bash
# Claude Code (OAuth: completes sign-in through MCP Gate on first use)
claude mcp add --transport http morphyard http://localhost:5055/mcp

# Any client with a personal access token issued by MCP Gate
claude mcp add --transport http morphyard http://localhost:5055/mcp \
  --header "Authorization: Bearer <token>"
```

接著請你的助理轉一個檔案，例如「把這個 DOCX 轉成 PDF」。

### 4. 驗證

```bash
curl -s http://localhost:5055/.well-known/oauth-protected-resource/mcp   # points at MCP Gate
curl -s http://localhost:5055/.well-known/oauth-authorization-server      # 轉發 MCP Gate 的 metadata，給較舊的 client 用
curl -i -X POST http://localhost:5055/mcp                                  # 401 without a token
```

若要在沒有 MCP Gate 的情況下本機實驗，關閉認證啟動並執行協定 smoke test：

```bash
AUTH_ENABLED=false SERVER_PORT=5056 MORPHYARD_REVOCATION_ENABLED=false uv run python main.py
uv run python scripts/mcp_smoke.py
```

## 部署方式

- **Docker** —— 不必安裝系統套件，image 內建所有引擎與字型：

  ```bash
  docker build -t morphyard .
  docker run -p 5055:5055 -e MCP_GATE_URL=http://mcp-gate:4568 \
    -e MORPHYARD_BASE_URL=http://127.0.0.1:5055 morphyard
  ```

- **直接在主機上**以 `uv run python main.py` 執行，如快速開始所示。

不論哪一種，`MORPHYARD_BASE_URL` 都必須是 client 實際使用的位址，因為 token 的 audience 由它推導。所有設定，以及如何開啟 revocation feed，見 [docs/configuration.zh-TW.md](docs/configuration.zh-TW.md)。

## 文件

| 文件 | 內容 |
|---|---|
| [工具與格式](docs/tools.zh-TW.md) | 八個工具、支援的格式、一次轉換如何執行、已知限制 |
| [設定](docs/configuration.zh-TW.md) | 環境變數與 MCP Gate revocation feed |
| [架構](docs/architecture.md)（英文） | 模組配置、引擎與 fidelity、格式偵測、轉換規劃、測試分層 |
| [開發](docs/development.zh-TW.md) | 準備環境、三層測試、CI、如何貢獻 |
| [發展路線圖](ROADMAP.zh-TW.md) | 已發布與接下來的內容 |
| [Changelog](CHANGELOG.md) | 每個版本的變更 |

## 貢獻

歡迎回報問題與提交 pull request。pull request 請開到 `dev`；開之前先執行 `uv run ruff check .` 與 `uv run pytest`。詳見 [docs/development.zh-TW.md](docs/development.zh-TW.md)。

## 授權

[MIT](LICENSE)
