"""Routing table and conversion planning tests.

Engine availability is pinned with mocks; otherwise results would depend on
whether the machine running the tests has pandoc / soffice installed, and a test
like that is worse than no test.
"""

from unittest.mock import patch

import pytest

from src.domain.convert import registry
from src.domain.convert.registry import (
    DIRECT_ROUTES,
    FIDELITY_HIGH,
    FIDELITY_LOSSY,
    FIDELITY_STRUCTURAL,
    capability_matrix,
    plan_conversion,
    resolve_formats,
)
from src.domain.convert.types import NoConversionPathError, UnsupportedFormatError


class _AlwaysAvailable:
    def is_available(self):
        return True

    def can_handle(self, src, dst):
        return True


class _Unavailable:
    def is_available(self):
        return False

    def can_handle(self, src, dst):
        return False


@pytest.fixture
def all_engines_available():
    """Pretend every engine is installed, decoupling tests from the host machine.

    Without this isolation the same test passes on a dev box with pandoc and
    fails on a clean CI runner, for reasons unrelated to the logic under test.
    """
    stubs = {name: _AlwaysAvailable() for name in registry.ENGINES}
    with patch.dict(registry.ENGINES, stubs):
        yield


class TestDirectRoutes:
    def test_office_to_pdf_is_high_fidelity(self):
        """docx -> pdf must go through LibreOffice, the only route that keeps the layout."""
        route = DIRECT_ROUTES[("docx", "pdf")]
        assert route.engine == "libreoffice"
        assert route.fidelity == FIDELITY_HIGH

    def test_markdown_to_docx_uses_pandoc(self):
        route = DIRECT_ROUTES[("md", "docx")]
        assert route.engine == "pandoc"
        assert route.fidelity == FIDELITY_STRUCTURAL

    def test_pdf_to_markdown_is_marked_lossy(self):
        """Extracting text from PDF is always lossy; plan_conversion decides on this flag."""
        route = DIRECT_ROUTES[("pdf", "md")]
        assert route.engine == "anydoc"
        assert route.fidelity == FIDELITY_LOSSY

    def test_no_direct_path_for_markdown_to_pdf(self):
        """md -> pdf needs multiple hops; the main reason plan_conversion exists."""
        assert ("md", "pdf") not in DIRECT_ROUTES

    def test_no_direct_path_for_pdf_to_docx(self):
        assert ("pdf", "docx") not in DIRECT_ROUTES

    def test_image_conversions_are_high_fidelity(self):
        assert DIRECT_ROUTES[("png", "jpg")].engine == "pillow"
        assert DIRECT_ROUTES[("png", "jpg")].fidelity == FIDELITY_HIGH

    def test_image_to_text_uses_ocr_not_anydoc(self):
        """Text from images must go through OCR.

        anydoc does not handle images at all; the previously used MarkItDown relied
        on LLM image descriptions rather than OCR and returned only EXIF metadata
        without an llm_client. Neither can extract text from images. This test
        guards "the routing table must not claim capabilities it does not have".
        """
        for fmt in ("png", "jpg", "tiff"):
            assert DIRECT_ROUTES[(fmt, "md")].engine == "ocr"

    def test_pdf_to_markdown_prefers_anydoc_over_ocr(self):
        """A PDF with a text layer only needs anydoc; OCR is the fallback when no text comes out."""
        assert DIRECT_ROUTES[("pdf", "md")].engine == "anydoc"


class TestResolveFormats:
    def test_normalizes_both_sides(self, ):
        assert resolve_formats(".DOCX", "Pdf") == ("docx", "pdf")

    def test_rejects_unknown_source(self):
        with pytest.raises(UnsupportedFormatError):
            resolve_formats("exe", "pdf")

    def test_rejects_unknown_target(self):
        with pytest.raises(UnsupportedFormatError):
            resolve_formats("docx", "dwg")


class TestPlanConversion:
    def test_same_format_needs_no_work(self):
        assert plan_conversion("pdf", "pdf") == []

    def test_direct_path_is_single_hop(self, all_engines_available):
        plan = plan_conversion("docx", "pdf")
        assert len(plan) == 1
        assert plan[0].engine == "libreoffice"

    def test_impossible_conversion_raises(self, all_engines_available):
        """No engine combination reaches png -> xlsx, and none should be forced."""
        with pytest.raises(NoConversionPathError):
            plan_conversion("png", "xlsx")

    def test_markdown_to_pdf_via_docx_pivot(self, all_engines_available):
        """md -> pdf should chain as md -(pandoc)-> docx -(soffice)-> pdf.

        Picking docx rather than html as the pivot matters: both routes carry the
        same fidelity labels, but LibreOffice's HTML import handles tables and
        layout noticeably worse. This test guards the ordering in _pivot_cost().
        """
        plan = plan_conversion("md", "pdf")
        assert [r.dst for r in plan] == ["docx", "pdf"]
        assert [r.engine for r in plan] == ["pandoc", "libreoffice"]

    def test_prefers_fewer_hops(self, all_engines_available):
        """md -> pptx has a direct route (pandoc writes pptx) and must not detour."""
        plan = plan_conversion("md", "pptx")
        assert len(plan) == 1
        assert plan[0].engine == "pandoc"

    def test_lossy_path_refused_by_default(self, all_engines_available):
        """pdf -> docx only goes through anydoc; by default refuse rather than emit a bad file."""
        with pytest.raises(NoConversionPathError) as exc:
            plan_conversion("pdf", "docx", prefer_fidelity=True)
        # The message must tell the AI where the escape hatch is, or it just
        # relays a technical error to the user
        assert "allow_quality_loss" in exc.value.details["hint"]

    def test_lossy_path_allowed_when_explicitly_opted_in(self, all_engines_available):
        plan = plan_conversion("pdf", "docx", prefer_fidelity=False)
        assert [r.dst for r in plan] == ["md", "docx"]
        assert any(r.fidelity == FIDELITY_LOSSY for r in plan)

    def test_lossy_hops_are_annotated_when_allowed(self, all_engines_available):
        """When a lossy path is allowed every hop is annotated so the warning reaches the user."""
        plan = plan_conversion("pdf", "docx", prefer_fidelity=False)
        lossy = [r for r in plan if r.fidelity == FIDELITY_LOSSY]
        assert lossy and all(r.note for r in lossy)

    def test_direct_lossy_route_is_not_blocked(self, all_engines_available):
        """pdf -> md is the extraction the user explicitly asked for; the gate must not block it.

        The quality gate only constrains multi-hop paths we chained on our own
        initiative, not conversions the user named directly; otherwise
        extract_text would break entirely.
        """
        plan = plan_conversion("pdf", "md", prefer_fidelity=True)
        assert len(plan) == 1
        assert plan[0].fidelity == FIDELITY_LOSSY

    def test_respects_engine_availability(self):
        """Without pandoc, the md -> pdf multi-hop path that needs it must disappear."""
        stubs = dict.fromkeys(registry.ENGINES)
        for name in stubs:
            stubs[name] = _Unavailable() if name == "pandoc" else _AlwaysAvailable()
        with patch.dict(registry.ENGINES, stubs), pytest.raises(NoConversionPathError):
            plan_conversion("md", "pdf")

    def test_never_exceeds_max_hops(self, all_engines_available):
        for src, dst in [("md", "pdf"), ("csv", "pdf"), ("rtf", "epub"),
                         ("xlsx", "docx"), ("doc", "md")]:
            assert len(plan_conversion(src, dst)) <= registry.MAX_HOPS

    def test_no_format_visited_twice(self, all_engines_available):
        """A path must not revisit a format (e.g. docx -> html -> docx -> pdf)."""
        plan = plan_conversion("csv", "pdf")
        visited = [plan[0].src] + [r.dst for r in plan]
        assert len(visited) == len(set(visited))


class TestCapabilityMatrix:
    def test_only_lists_installed_engines(self):
        """An engine that is not installed must not appear in the capability claims."""
        with patch.dict(
            registry.ENGINES,
            {"pandoc": _Unavailable(), "libreoffice": _Unavailable()},
        ):
            matrix = capability_matrix()
            assert "pdf" not in matrix.get("docx", [])

    def test_matrix_targets_are_sorted(self):
        matrix = capability_matrix()
        for targets in matrix.values():
            assert targets == sorted(targets)
