"""Landing page and Markdown converter tests."""

from src.api.router.index import markdown_to_html


class TestMarkdownToHtml:
    """Offline Markdown converter."""

    def test_heading(self):
        """Headings become <h1>/<h2> of the matching level; non-ASCII text passes through."""
        assert "<h1>標題</h1>" in markdown_to_html("# 標題")
        assert "<h2>Subtitle</h2>" in markdown_to_html("## Subtitle")

    def test_paragraph_and_inline(self):
        """Bold and inline code inside a paragraph are converted."""
        out = markdown_to_html("this is **bold** and `code`")
        assert "<strong>bold</strong>" in out
        assert "<code>code</code>" in out

    def test_list(self):
        """List items become <ul><li>."""
        out = markdown_to_html("- one\n- two")
        assert out.count("<li>") == 2
        assert "<ul>" in out

    def test_code_fence_is_escaped(self):
        """Code block content is HTML-escaped and never treated as real HTML."""
        out = markdown_to_html("```\n<script>x</script>\n```")
        assert "<pre><code>" in out
        assert "&lt;script&gt;" in out

    def test_link(self):
        """Link syntax becomes <a>."""
        out = markdown_to_html("[docs](/docs)")
        assert '<a href="/docs">docs</a>' in out


class TestLandingPage:
    """Landing page route (needs create_app + TestClient)."""

    def _client(self):
        """Build a minimal test client with auth disabled and no modules.

        Returns:
            A Starlette TestClient bound to the assembled app.
        """
        from starlette.testclient import TestClient

        from app import create_app
        from src.config.model import (
            AppConfig,
            AuthConfig,
            ConfigModel,
            LoggingConfig,
            ModulesConfig,
            ServerConfig,
        )

        cfg = ConfigModel(
            server=ServerConfig(host="127.0.0.1", port=5050, transport="http"),
            auth=AuthConfig(enabled=False),
            logging=LoggingConfig(level="WARNING"),
            app=AppConfig(
                name="TestMCP", version="9.9.9", title="Test MCP Service", description="desc"
            ),
            modules=ModulesConfig(enabled=[]),
        )
        return TestClient(create_app(cfg, transport="http"))

    def test_index_returns_html(self):
        """The landing page returns 200 with an HTML content-type."""
        r = self._client().get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_index_shows_title_and_docs_button(self):
        """The landing page shows the service title, version and the /docs button."""
        body = self._client().get("/").text
        assert "Test MCP Service" in body
        assert 'href="/docs"' in body
        assert "v9.9.9" in body
