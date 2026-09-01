"""Unit tests for format detection and page-range parsing.

Both are pure functions with no external dependencies, the most worthwhile and
easiest part of the service to test: every injected file hits its first branch
point in detect_format.
"""

import io
import zipfile

import pytest

from src.domain.convert.pdfops import parse_page_range
from src.domain.convert.types import (
    ConversionFailedError,
    UnsupportedFormatError,
    detect_format,
    normalize_format,
    stem_of,
)


class TestNormalizeFormat:
    def test_strips_dot_and_lowercases(self):
        assert normalize_format(".PDF") == "pdf"
        assert normalize_format("Docx") == "docx"

    def test_resolves_aliases(self):
        assert normalize_format("jpeg") == "jpg"
        assert normalize_format("htm") == "html"
        assert normalize_format("markdown") == "md"

    def test_macro_enabled_variants_collapse_to_base_format(self):
        """docm / xlsm / pptm share the base format's container and parse fine.

        Folded into the alias table instead of duplicating a full set of routes
        each; the engines all detect the format from content anyway.
        """
        assert normalize_format("docm") == "docx"
        assert normalize_format("xlsm") == "xlsx"
        assert normalize_format("pptm") == "pptx"
        assert normalize_format("ppsx") == "pptx"

    def test_xlsb_maps_to_the_excel_family(self):
        """XLSB is BIFF12 inside rather than XML, but anydoc reads it.

        This test once asserted the opposite, based on "a synthetic BIFF12
        container is rejected by anydoc"; but that file was never a valid xlsb,
        so rejecting it was correct. Reading the anydoc source confirmed the
        Format::Excel comment: "every container calamine reads: .xlsx, .xlsm,
        .xlsb, and binary .xls".

        Lesson: concluding "unsupported" from a hand-made fake file only tests
        your own assumptions.
        """
        assert normalize_format("xlsb") == "xlsx"

    def test_rejects_unknown(self):
        assert normalize_format("exe") is None
        assert normalize_format("") is None
        assert normalize_format(None) is None


class TestDetectFormat:
    def test_extension_wins_over_useless_mime(self):
        """Injected files often carry application/octet-stream; trust the extension."""
        assert detect_format("report.docx", "application/octet-stream") == "docx"

    def test_falls_back_to_mime_when_no_extension(self):
        assert detect_format("noext", "application/pdf") == "pdf"

    def test_falls_back_to_magic_bytes(self):
        assert detect_format(None, None, b"%PDF-1.7 ...") == "pdf"
        assert detect_format(None, None, b"\x89PNG\r\n\x1a\n") == "png"

    def test_bare_zip_magic_is_not_guessed(self):
        """With only a PK header and no recognizable content, a wrong guess is worse than none."""
        assert detect_format(None, "application/octet-stream", b"PK\x03\x04") is None

    def test_returns_none_when_all_signals_fail(self):
        assert detect_format("mystery", "application/octet-stream", b"\x00\x01") is None


class TestContentSniffing:
    """With neither file name nor MIME, detect from the container's internal layout.

    Not a theoretical edge case: after injecting a file, the host application's
    file-injection (artifact) mechanism blanks the parameter that held the
    upload reference. If the AI put the reference into file_name (a natural
    mistake), the name is gone; `Artifact resolved: (unnamed) -> format=pptx` in
    real logs is exactly that. Content detection keeps the service working then.
    """

    def _ooxml(self, part_path: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr(part_path, "<xml/>")
        return buf.getvalue()

    def _odf(self, mimetype: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("mimetype", mimetype)
            z.writestr("content.xml", "<xml/>")
        return buf.getvalue()

    def test_ooxml_detected_by_directory_structure(self):
        assert detect_format(None, None, self._ooxml("word/document.xml")) == "docx"
        assert detect_format(None, None, self._ooxml("xl/workbook.xml")) == "xlsx"
        assert detect_format(None, None, self._ooxml("ppt/presentation.xml")) == "pptx"

    def test_odf_and_epub_detected_by_mimetype_entry(self):
        assert detect_format(None, None, self._odf(
            "application/vnd.oasis.opendocument.text")) == "odt"
        assert detect_format(None, None, self._odf(
            "application/vnd.oasis.opendocument.spreadsheet")) == "ods"
        assert detect_format(None, None, self._odf(
            "application/vnd.oasis.opendocument.presentation")) == "odp"
        assert detect_format(None, None, self._odf("application/epub+zip")) == "epub"

    def test_xlsb_container_is_recognized_as_excel(self):
        """XLSB's workbook is .bin rather than .xml, but still lives under xl/.

        It shares the internal code with xlsx; every engine detects the real
        format from content, anydoc via calamine and LibreOffice via its own
        sniffing.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("xl/workbook.bin", b"\x85\x00BIFF12")
        assert detect_format(None, None, buf.getvalue()) == "xlsx"

    def test_pdf_header_tolerates_leading_junk(self):
        """ISO 32000 does not require %PDF- at offset 0; real readers accept leading junk.

        With a plain startswith, a PDF with a BOM or leading whitespace fails
        detection. anydoc scans the first 1024 bytes; this mirrors that.
        """
        assert detect_format(None, None, b"\xef\xbb\xbf   %PDF-1.7 body") == "pdf"
        assert detect_format(None, None, b"\x00" * 500 + b"%PDF-1.4") == "pdf"
        # Beyond the tolerance window it must no longer be recognized
        assert detect_format(None, None, b"\x00" * 2000 + b"%PDF-1.4") is None

    def test_ole_stream_names_are_matched_case_insensitively(self):
        """The CFB spec compares names upper-cased; producers spell them differently.

        anydoc's detect.rs uses eq_ignore_ascii_case and notes "producers vary
        (WORKBOOK, BOOK)". A case-sensitive comparison misses those producers.
        """
        for written_as in ("Workbook", "WORKBOOK", "Book", "BOOK"):
            header = bytearray(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)
            header[0x1E:0x20] = (9).to_bytes(2, "little")
            header[0x30:0x34] = (0).to_bytes(4, "little")
            body = bytearray(b"\x00" * 512)
            body[0:len(written_as) * 2] = written_as.encode("utf-16-le")
            assert detect_format(None, None, bytes(header) + bytes(body)) == "xls", \
                f"detection failed when written as {written_as}"

    def test_corrupt_zip_does_not_raise(self):
        """A corrupt ZIP must quietly return None, not blow up the whole request."""
        assert detect_format(None, None, b"PK\x03\x04" + b"\x00" * 50) is None

    def test_ole_directory_located_via_header_not_scan(self):
        """The OLE2 directory may sit at the end (LibreOffice .ppt does); locate it via the header.

        Synthesize an OLE with the directory late in the file: sector size 512,
        directory starting at sector 20, so its real offset is (20+1)*512 = 10752,
        far beyond any "scan the head" window.
        """
        sector_size, dir_sector = 512, 20
        header = bytearray(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)
        header[0x1E:0x20] = (9).to_bytes(2, "little")          # 2^9 = 512
        header[0x30:0x34] = dir_sector.to_bytes(4, "little")
        body = bytearray(b"\x00" * (sector_size * 24))
        offset = (dir_sector + 1) * sector_size - sector_size  # body excludes the header
        body[offset:offset + 40] = "PowerPoint Document".encode("utf-16-le")
        assert detect_format(None, None, bytes(header) + bytes(body)) == "ppt"

    def test_filename_still_wins_over_content(self):
        """With a file name the extension still wins; content detection is only a fallback."""
        docx_bytes = self._ooxml("word/document.xml")
        assert detect_format("thing.pptx", None, docx_bytes) == "pptx"


class TestStemOf:
    def test_strips_path_and_extension(self):
        assert stem_of("/tmp/dir/report.docx") == "report"
        assert stem_of("C:\\docs\\report.final.docx") == "report.final"

    def test_defaults_when_empty(self):
        assert stem_of(None) == "converted"
        assert stem_of("") == "converted"


class TestParsePageRange:
    def test_single_pages(self):
        assert parse_page_range("1,3,5", 10) == [0, 2, 4]

    def test_inclusive_range(self):
        assert parse_page_range("2-4", 10) == [1, 2, 3]

    def test_open_ended_range(self):
        assert parse_page_range("8-", 10) == [7, 8, 9]

    def test_empty_spec_means_all_pages(self):
        assert parse_page_range("", 3) == [0, 1, 2]

    def test_preserves_user_ordering_for_reordering(self):
        """'3,1' reorders pages; do not helpfully sort it."""
        assert parse_page_range("3,1", 5) == [2, 0]

    def test_deduplicates_overlaps(self):
        assert parse_page_range("1-3,2", 5) == [0, 1, 2]

    def test_rejects_out_of_bounds(self):
        with pytest.raises(ConversionFailedError) as exc:
            parse_page_range("1-99", 10)
        assert "out of bounds" in str(exc.value)

    def test_rejects_bad_syntax(self):
        with pytest.raises(ConversionFailedError) as exc:
            parse_page_range("abc", 10)
        assert "invalid page range" in str(exc.value)


class TestUnsupportedRequestMessages:
    """Error messages must let the AI decide between "give up" and "retry".

    From real logs: the user asked to convert a file to wav, and the AI received
    "could not recognize the file format, provide a file_name with an extension",
    an error unrelated to the real problem. It went on guessing file names,
    moving parameters and switching tools, six useless calls in 12 seconds,
    never learning the key fact that audio is simply unsupported.
    """

    def test_unsupported_target_is_rejected_before_touching_the_file(self):
        """target_format is chosen entirely by the AI and needs no file; validate it first.

        Pass a file_content that is guaranteed to fail parsing: with the right
        order the message talks about the target format, not the file.
        """
        import asyncio

        from src.domain.convert.service import convert_artifact

        with pytest.raises(UnsupportedFormatError) as exc:
            asyncio.run(convert_artifact(
                file_content="bm90IGEgcmVhbCBmaWxl",  # "not a real file"
                file_name=None, mime_type=None, target_format="wav",
            ))
        message = str(exc.value)
        assert "wav" in message
        assert "audio" in message, "must say audio is unsupported, or the AI keeps retrying"
        assert "Could not recognize this file" not in message, "must not blame the file"

    def test_unrecognized_input_tells_ai_to_stop(self):
        """When the input is unrecognizable, tell the AI when not to retry."""
        import asyncio

        from src.domain.convert.service import convert_artifact

        with pytest.raises(UnsupportedFormatError) as exc:
            asyncio.run(convert_artifact(
                file_content="bm90IGEgcmVhbCBmaWxl",
                file_name=None, mime_type=None, target_format="pdf",
            ))
        message = str(exc.value)
        assert "do not retry" in message
        assert "docx" in message, "must list supported formats so the AI knows the alternatives"


class TestPlainTextFileContent:
    """Plain text in file_content must be used as-is, not rejected.

    The contract says base64, but in a multi-step flow the AI simply holds a
    piece of Markdown and has no reason to encode it first. From real logs: it
    pasted `# 年度供應商評估報告...` directly, hit `'ascii' codec can't encode
    characters in position 2-3`, then guessed and retried 8 times. base64 is a
    transport encoding, not the essence of the contract. The CJK samples below
    are deliberate for that reason.
    """

    def _resolve(self, content, name=None):
        from src.domain.convert.service import resolve_artifact_input
        return resolve_artifact_input(content, name, None)

    def test_markdown_source_text_is_accepted(self):
        src = self._resolve("# 年度供應商評估報告\n\n## 背景\n\n- 重點\n")
        assert src.fmt == "md"

    def test_html_and_json_are_recognized(self):
        assert self._resolve("<html><body><h1>x</h1></body></html>").fmt == "html"
        assert self._resolve('{"a": 1, "b": [2]}').fmt == "json"

    def test_featureless_text_falls_back_to_txt(self):
        assert self._resolve("這是一段沒有任何語法特徵的中文說明。").fmt == "txt"

    def test_real_base64_still_wins(self):
        import base64 as b64
        payload = b64.b64encode(b"%PDF-1.7 hello world padding").decode()
        assert self._resolve(payload).fmt == "pdf"

    def test_a_bare_file_name_is_diagnosed_not_converted(self):
        """The AI occasionally puts the file name into file_content.

        This must run before plain-text inference; otherwise it falls back to
        txt and the user receives a Word file whose only content is a file name.
        """
        with pytest.raises(UnsupportedFormatError) as exc:
            self._resolve("年度供應商評估報告.docx")
        assert "looks like a file name" in str(exc.value)
