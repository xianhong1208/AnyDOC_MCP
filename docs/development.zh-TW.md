# 開發

[English](development.md) · [README](../README.zh-TW.md)

## 準備環境

```bash
git clone https://github.com/xianhong1208/morphyard.git
cd morphyard
uv sync                      # 包含 dev 群組：pytest、ruff
```

轉換引擎是系統套件，見[安裝](../README.zh-TW.md#安裝)。沒有引擎時單元測試仍會通過（引擎都是 mock），但 `sweep_routes.py` 會跳過缺少的引擎負責的路線。

## 測試

```bash
uv run pytest                              # logic, < 1 s, engines are mocked
uv run ruff check .                        # lint
uv run python scripts/sweep_routes.py      # every advertised path against real engines, ~70 s
uv run python scripts/mcp_smoke.py         # protocol check with a real MCP client (server on :5056)
```

協定檢查需要一個在 5056 埠、關閉認證的伺服器。先在另一個終端機啟動：

```bash
AUTH_ENABLED=false SERVER_PORT=5056 MORPHYARD_REVOCATION_ENABLED=false uv run python main.py
```

三層各自回答不同的問題 —— 邏輯對不對、引擎是否真的產出有效檔案、client 是否真的能使用這些工具 —— 而每一層都抓到過其他層抓不到的 bug，詳見 [docs/architecture.md](architecture.md#testing)。

CI（`.github/workflows/ci.yml`）在每次 push 與對 `dev`、`main` 的 pull request 時跑這三層：先在 runner 上跑 ruff 與 pytest，再 build Docker image，並在容器內跑 `sweep_routes.py --strict`（被跳過的路線也算失敗）與 `mcp_smoke.py`，所以檢查到的引擎與字型就是實際出貨的那一套。

## 貢獻

歡迎回報問題與提交 pull request。從 `dev` 開分支，pull request 也開回 `dev`；`main` 只在發布時前進，兩個分支都只接受 pull request。註解與 docstring 請使用英文，開 PR 前執行 `uv run ruff check .` 與 `uv run pytest`，修改 registry 或升級引擎後請執行 `scripts/sweep_routes.py`。行為上的變更要附測試、在 [CHANGELOG.md](../CHANGELOG.md) 的 `[Unreleased]` 加一筆，並同步更新兩份 README。
