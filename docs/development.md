# Development

[繁體中文](development.zh-TW.md) · [README](../README.md)

## Set up

```bash
git clone https://github.com/xianhong1208/morphyard.git
cd morphyard
uv sync                      # includes the dev group: pytest, ruff
```

The conversion engines are system packages; see [Installation](../README.md#installation). Without them the unit tests still pass (engines are mocked), but `sweep_routes.py` skips the routes of every missing engine.

## Testing

```bash
uv run pytest                              # logic, < 1 s, engines are mocked
uv run ruff check .                        # lint
uv run python scripts/sweep_routes.py      # every advertised path against real engines, ~70 s
uv run python scripts/mcp_smoke.py         # protocol check with a real MCP client (server on :5056)
```

The protocol check needs a server without authentication on port 5056. Start it in another terminal first:

```bash
AUTH_ENABLED=false SERVER_PORT=5056 MORPHYARD_REVOCATION_ENABLED=false uv run python main.py
```

The three layers answer different questions — is the logic right, do the engines actually produce valid files, can a client really use the tools — and each has caught bugs the others cannot. [docs/architecture.md](architecture.md#testing) says which.

CI (`.github/workflows/ci.yml`) runs all three on every push and pull request to `dev` and `main`: ruff and pytest on the runner, then it builds the Docker image and runs `sweep_routes.py --strict` (a skipped route fails too) and `mcp_smoke.py` inside the container, so the engines and fonts checked are the ones that ship.

## Contributing

Bug reports and pull requests are welcome. Branch off `dev` and open the pull request into `dev`; `main` only moves at release time, and both branches accept changes through pull requests only. Keep comments and docstrings in English, run `uv run ruff check .` and `uv run pytest` before opening a PR, and run `scripts/sweep_routes.py` after touching the registry or upgrading an engine. A change in behaviour comes with tests, an entry under `[Unreleased]` in [CHANGELOG.md](../CHANGELOG.md), and updates to both READMEs.
