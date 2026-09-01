"""Tests for the desktop app and cross-platform binary discovery.

The desktop app shares the conversion core with MCP, so conversion logic is not
re-tested here. Only the desktop-specific parts are: file in/out, no overwrite,
batch fault tolerance, and Windows path handling (run on Linux, hence all mocked).
"""

from unittest.mock import patch

from desktop.converter import _unique_path, convert_file, missing_engine_hint
from src.domain.convert.engines import discovery


class TestOutputNaming:
    def test_does_not_overwrite_existing_files(self, tmp_path):
        """A desktop tool must not eat the user's files by default."""
        existing = tmp_path / "report.pdf"
        existing.write_bytes(b"original")
        assert _unique_path(existing, overwrite=False).name == "report (1).pdf"

    def test_overwrite_flag_is_respected(self, tmp_path):
        existing = tmp_path / "report.pdf"
        existing.write_bytes(b"x")
        assert _unique_path(existing, overwrite=True) == existing

    def test_unused_name_is_returned_as_is(self, tmp_path):
        target = tmp_path / "fresh.pdf"
        assert _unique_path(target, overwrite=False) == target


class TestFailuresAreValuesNotExceptions:
    """In a batch, "three succeed, one fails" is the normal case.

    Expressing that as an exception forces callers to wrap a try around the loop,
    and the failed file still has to show up in the list rather than disappear.
    """

    def test_unreadable_file_returns_a_result(self, tmp_path):
        result = convert_file(tmp_path / "does_not_exist.docx", "pdf")
        assert result.ok is False and "Failed to read" in result.message

    def test_unknown_target_format_returns_a_result(self, tmp_path):
        source = tmp_path / "a.txt"
        source.write_text("hi")
        result = convert_file(source, "wav")
        assert result.ok is False and "Unsupported target format" in result.message

    def test_unrecognized_source_returns_a_result(self, tmp_path):
        source = tmp_path / "mystery.bin"
        source.write_bytes(b"\x00\x01\x02\x03")
        result = convert_file(source, "pdf")
        assert result.ok is False and "Could not recognize" in result.message


class TestWindowsDiscovery:
    """Binary discovery on Windows.

    LibreOffice lives in %ProgramFiles%\\LibreOffice\\program\\ and is not on PATH,
    so `shutil.which` never finds it. Without this layer four of the six engines
    report NOT AVAILABLE on Windows and the capability matrix shrinks to 44
    routes even though everything is installed.
    """

    def setup_method(self):
        discovery.clear_cache()

    def teardown_method(self):
        discovery.clear_cache()

    def test_path_lookup_wins_when_available(self):
        with patch.object(discovery.shutil, "which", return_value="/usr/bin/soffice"):
            assert discovery.find_binary("soffice") == "/usr/bin/soffice"

    def test_windows_install_locations_are_searched(self, tmp_path):
        fake = tmp_path / "LibreOffice" / "program" / "soffice.exe"
        fake.parent.mkdir(parents=True)
        fake.write_text("")

        with patch.object(discovery, "IS_WINDOWS", True), \
             patch.object(discovery.shutil, "which", return_value=None), \
             patch.dict(discovery._WINDOWS_HINTS, {"soffice": [str(fake)]}):
            assert discovery.find_binary("soffice") == str(fake)

    def test_results_are_cached(self):
        with patch.object(discovery.shutil, "which", return_value="/x/pandoc") as which:
            discovery.find_binary("pandoc")
            discovery.find_binary("pandoc")
        assert which.call_count == 1, "re-probing makes every matrix query rescan PATH"

    def test_missing_binary_returns_none(self):
        with patch.object(discovery.shutil, "which", return_value=None), \
             patch.object(discovery, "IS_WINDOWS", False):
            assert discovery.find_binary("nonexistent-tool") is None


class TestFileUrl:
    """LibreOffice's -env:UserInstallation must be a valid file:// URL.

    String concatenation happens to be right on Linux (/tmp/x -> file:///tmp/x),
    but C:\\Users\\x on Windows becomes file://C:\\Users\\x: one slash short and
    backslashes unconverted, so LibreOffice rejects it outright.
    """

    def test_posix_path_gets_three_slashes(self, tmp_path):
        url = discovery.path_to_file_url(tmp_path / "profile")
        assert url.startswith("file:///")
        assert "\\" not in url

    def test_naive_concatenation_would_be_wrong_on_windows(self):
        """Document the shape of the bug: concatenation yields an invalid URL for Windows paths."""
        windows_path = "C:\\Users\\tony\\AppData\\Local\\Temp\\lo_1"
        naive = f"file://{windows_path}"
        assert not naive.startswith("file:///"), "only two slashes; LibreOffice would reject it"
        assert "\\" in naive, "backslashes were not converted to forward slashes"


class TestEngineHint:
    def test_hint_is_empty_when_everything_is_installed(self):
        with patch("desktop.converter.available_engines", return_value={
            "libreoffice": True, "pandoc": True, "ocr": True,
            "anydoc": True, "pillow": True, "text": True,
        }):
            assert missing_engine_hint() == ""

    def test_hint_names_capabilities_not_binaries(self):
        """The user need not know what pandoc is, only which feature is missing."""
        with patch("desktop.converter.available_engines", return_value={
            "libreoffice": False, "pandoc": False, "ocr": False,
            "anydoc": True, "pillow": True, "text": True,
        }):
            hint = missing_engine_hint()
        assert "pandoc" not in hint and "libreoffice" not in hint.lower()
        assert "PDF" in hint and "text recognition" in hint
