"""Tests for the Survey Funnel family.

Verifies:
  - contracts/survey_funnel.yaml parses and exposes the expected top-level keys
  - SurveyFunnelManifest validates a realistic 3-step sample (the one in the brief)
  - FormStep + FormQuestion validate the shapes the renderer expects
  - generators.survey_funnel.make_design_brief returns the agreed payload keys
  - Bad inputs (>5 steps, missing options for radio, options for text) are rejected
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from design_mcp.generators import survey_funnel
from design_mcp.manifest import (
    FormQuestion,
    FormStep,
    HeroSection,
    SeoBlock,
    SurveyFunnelManifest,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "contracts" / "survey_funnel.yaml"
)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

def test_contract_parses_and_has_expected_top_level_keys() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert data["family"] == "survey-funnel"
    assert data["version"] == 1
    # Must define the form_engine block (new — Survey Funnel-specific)
    assert "form_engine" in data
    fe = data["form_engine"]
    assert fe["default_steps"] == 3
    assert fe["max_steps"] == 5
    assert fe["linear_only"] is True
    # Shared sections / theming / forbidden lists are present
    for key in (
        "mandatory_sections",
        "optional_sections",
        "seo",
        "image_contract",
        "theming",
        "forbidden",
        "required_in_head",
        "output",
    ):
        assert key in data, f"contract missing top-level key: {key}"
    # Font allowlist mirrors landing-page family
    assert "Montserrat" in data["theming"]["font_allowlist"]


# ---------------------------------------------------------------------------
# FormQuestion / FormStep
# ---------------------------------------------------------------------------

def test_form_question_text_no_options() -> None:
    q = FormQuestion(name="email", type="email", label="Email", required=True)
    assert q.options is None
    assert q.required is True


def test_form_question_radio_requires_options() -> None:
    with pytest.raises(ValidationError):
        FormQuestion(name="size", type="radio", label="Size", required=True)


def test_form_question_text_rejects_options() -> None:
    with pytest.raises(ValidationError):
        FormQuestion(
            name="name",
            type="text",
            label="Name",
            options=["a", "b"],
            required=True,
        )


def test_form_step_validates_with_questions() -> None:
    step = FormStep(
        id="step-1",
        heading="What type of property?",
        questions=[
            FormQuestion(
                name="property_type",
                type="radio",
                label="Property type",
                options=["House", "Apartment", "Commercial"],
                required=True,
            ),
        ],
    )
    assert step.id == "step-1"
    assert len(step.questions) == 1
    assert step.questions[0].options == ["House", "Apartment", "Commercial"]


def test_form_step_rejects_bad_id_pattern() -> None:
    with pytest.raises(ValidationError):
        FormStep(
            id="step_one",   # underscore — must match step-N
            heading="Heading",
            questions=[FormQuestion(name="x", type="text", label="X")],
        )


# ---------------------------------------------------------------------------
# SurveyFunnelManifest — the sample from the brief
# ---------------------------------------------------------------------------

SAMPLE_MANIFEST_YAML = """
family: survey-funnel
slug: solarquotes-test
intent: Solar panel quote comparison for Aussie homeowners
seo:
  title: SolarQuotes — Compare solar panel installers
  site_name: SolarQuotes
  meta_description: Get up to 3 quotes from accredited Australian solar installers in under 3 minutes.
hero:
  headline: Compare solar installer quotes in 3 minutes
  subheading: Up to 3 accredited installers, instant comparison
  cta_label: Get My Quotes
  image_url: https://picsum.photos/seed/solar/1200/800
  image_alt: Solar panels on Australian rooftop
steps:
  - id: step-1
    heading: What type of property?
    questions:
      - { name: property_type, type: radio, label: Property type, options: [House, Apartment, Commercial], required: true }
  - id: step-2
    heading: When are you looking to install?
    questions:
      - { name: timeframe, type: radio, label: Timeframe, options: [Within 3 months, 3-6 months, Just researching], required: true }
  - id: step-3
    heading: Your contact details
    questions:
      - { name: name, type: text, label: Full name, required: true }
      - { name: email, type: email, label: Email, required: true }
      - { name: phone, type: tel, label: Mobile, required: true }
otp_enabled: true
submit_label: Get My Quotes
optional_sections: [progress_indicator, trust_badges]
"""


def test_sample_3_step_manifest_validates() -> None:
    data = yaml.safe_load(SAMPLE_MANIFEST_YAML)
    m = SurveyFunnelManifest(**data)
    assert m.family == "survey-funnel"
    assert m.slug == "solarquotes-test"
    assert len(m.steps) == 3
    assert m.otp_enabled is True
    assert m.submit_label == "Get My Quotes"
    assert "progress_indicator" in m.optional_sections
    # Contact step is final, has the 3 contact inputs
    final = m.steps[-1]
    assert {q.name for q in final.questions} == {"name", "email", "phone"}


def test_manifest_rejects_more_than_5_steps() -> None:
    base = yaml.safe_load(SAMPLE_MANIFEST_YAML)
    # 6 steps — should fail
    base["steps"] = [
        {
            "id": f"step-{i}",
            "heading": f"Step {i}",
            "questions": [{"name": f"q{i}", "type": "text", "label": f"Q{i}"}],
        }
        for i in range(1, 7)
    ]
    with pytest.raises(ValidationError):
        SurveyFunnelManifest(**base)


def test_manifest_otp_defaults_to_false() -> None:
    m = SurveyFunnelManifest(
        slug="defaults-test",
        intent="Stub intent that is long enough for validation.",
        seo=SeoBlock(
            title="Defaults test funnel",
            site_name="Acquirely Test",
            meta_description=(
                "A stub manifest used to verify defaults — otp_enabled should be false."
            ),
        ),
        hero=HeroSection(
            headline="Headline",
            subheading="Subheading",
            cta_label="Start",
            image_url="https://picsum.photos/seed/defaults/1600/900",
            image_alt="Placeholder",
        ),
        steps=[
            FormStep(
                id="step-1",
                heading="Heading",
                questions=[FormQuestion(name="name", type="text", label="Name")],
            )
        ],
        submit_label="Submit",
    )
    assert m.otp_enabled is False
    assert m.optional_sections == []


# ---------------------------------------------------------------------------
# generators.survey_funnel.make_design_brief
# ---------------------------------------------------------------------------

def test_make_design_brief_returns_expected_keys() -> None:
    payload = survey_funnel.make_design_brief(
        "Compare solar quotes for Aussie homeowners",
        requested_slug="solarquotes-test",
    )
    assert set(payload.keys()) == {
        "instructions",
        "contract",
        "manifest_schema",
        "slug_hint",
    }
    assert isinstance(payload["instructions"], str) and len(payload["instructions"]) > 200
    assert payload["contract"]["family"] == "survey-funnel"
    assert payload["manifest_schema"]["properties"]["family"]["const"] == "survey-funnel"
    assert payload["slug_hint"] == "solarquotes-test"


def test_make_design_brief_auto_slugifies_when_omitted() -> None:
    payload = survey_funnel.make_design_brief("Compare Solar Quotes — Aussie Homeowners!")
    # lowercased, non-alphanum collapsed to hyphens, stripped
    assert payload["slug_hint"] == "compare-solar-quotes-aussie-homeowners"


# ---------------------------------------------------------------------------
# Stub renderer smoke test (exercises _render_html + _fieldset + _question +
# the OTP branch on/off paths).
# ---------------------------------------------------------------------------

def test_stub_render_contains_required_pieces() -> None:
    html, m, _summary = survey_funnel._stub_output(
        "solarquotes-stub",
        "Compare solar quotes for Aussie homeowners — stub brief copy.",
    )
    # 3 fieldsets (default stub), single <h1>, theming bake-in, Tailwind CDN
    assert html.count("<fieldset data-step=") == 3
    assert html.count("<h1") == 1
    assert ":root" in html and "--color-primary" in html
    assert 'src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"' in html
    # Final form posts to the Acquirely backend (generic lead endpoint)
    assert 'action="/api/add-lead"' in html
    assert "/api/handle_Client_Lead_Submission" not in html
    # Stub uses otp_enabled=False → no OTP section emitted
    assert m.otp_enabled is False
    assert 'class="otp' not in html


def test_render_with_otp_enabled_emits_otp_section() -> None:
    data = yaml.safe_load(SAMPLE_MANIFEST_YAML)  # otp_enabled=true in the sample
    m = SurveyFunnelManifest(**data)
    html = survey_funnel._render_html(m)
    assert 'class="otp' in html
    assert "/api/verificationsms" in html


# ---------------------------------------------------------------------------
# Stock-image flow — mirrors tests/test_images.py for the landing_page family.
# Survey funnel reuses the SHARED IMAGE & ICON RULES block + the same 3-option
# images_choice field; these tests assert that wiring is live for surveys.
# ---------------------------------------------------------------------------

class TestSurveyImagesChoiceField:
    def test_images_choice_field_present(self) -> None:
        from design_mcp.generators.survey_funnel import _CLARIFYING_FIELDS

        keys = [cf.key for cf in _CLARIFYING_FIELDS]
        assert "images_choice" in keys

    def test_images_choice_options_verbatim_and_in_order(self) -> None:
        from design_mcp.generators.survey_funnel import _CLARIFYING_FIELDS

        cf = next(f for f in _CLARIFYING_FIELDS if f.key == "images_choice")
        # EXACT same 3 options as the landing_page family (verbatim, in order).
        assert cf.suggested_options == (
            "Yes — I'll paste image URLs in chat now",
            "Yes — search free stock photos (Pexels + Unsplash) for me",
            "No — clean modern look with icons + gradients only",
        )

    def test_images_choice_options_match_landing_page(self) -> None:
        # DRY guard: survey + landing image options must stay identical so the
        # shared image-flow block documents the same branch strings for both.
        from design_mcp.generators.landing_page import (
            _CLARIFYING_FIELDS as LP_FIELDS,
        )
        from design_mcp.generators.survey_funnel import (
            _CLARIFYING_FIELDS as SF_FIELDS,
        )

        lp = next(f for f in LP_FIELDS if f.key == "images_choice")
        sf = next(f for f in SF_FIELDS if f.key == "images_choice")
        assert sf.suggested_options == lp.suggested_options

    def test_images_choice_default_is_icons_only(self) -> None:
        from design_mcp.generators._brief_template import SURVEY_FUNNEL_DEFAULTS

        # Speed-mode must default to the no-fabrication icon/gradient path.
        assert (
            SURVEY_FUNNEL_DEFAULTS["images_choice"]
            == "No — clean modern look with icons + gradients only"
        )


class TestSurveyBriefRendersImageRules:
    def _brief(self) -> str:
        return survey_funnel.make_design_brief(
            "Compare solar quotes for Aussie homeowners",
            requested_slug="solar-stock-test",
        )["instructions"]

    def test_image_icon_rules_block_present(self) -> None:
        text = self._brief()
        assert "IMAGE & ICON RULES" in text
        assert "NEVER FABRICATE" in text
        # Names the family-agnostic tools the caller can already use.
        assert "search_stock_images" in text
        assert "fetch_icons" in text
        assert "search_icons" in text

    def test_forbids_inline_svg_and_fabricated_photo_urls(self) -> None:
        text = self._brief()
        assert "NEVER write inline" in text and "<svg>" in text
        assert "NEVER fabricate" in text and "Pexels" in text and "Unsplash" in text

    def test_stock_branch_shows_inline_gallery_before_asking(self) -> None:
        text = self._brief()
        # Same inline-gallery requirement the landing_page brief carries.
        assert "INLINE NUMBERED MARKDOWN-IMAGE GALLERY" in text
        assert "url_medium" in text
        assert "![Photo by {photographer}]({url_medium})" in text

    def test_describes_three_images_choice_branches(self) -> None:
        text = self._brief()
        assert "Yes — I'll paste image URLs in chat now" in text
        assert "Yes — search free stock photos (Pexels + Unsplash) for me" in text
        assert "No — clean modern look with icons + gradients only" in text

    def test_image_block_is_the_shared_one(self) -> None:
        # DRY proof: the survey brief contains the EXACT shared constant from
        # _brief_template.py — not a hand-copied paraphrase.
        from design_mcp.generators._brief_template import _IMAGE_ICON_RULES_BLOCK

        assert _IMAGE_ICON_RULES_BLOCK.strip() in self._brief()


# ---------------------------------------------------------------------------
# Manifest — optional results/CTA image slot (net-new for survey funnels).
# ---------------------------------------------------------------------------

class TestResultsCtaImageSlot:
    def _base(self) -> dict:
        return yaml.safe_load(SAMPLE_MANIFEST_YAML)

    def test_results_cta_with_image_validates(self) -> None:
        from design_mcp.manifest import ResultsCta

        data = self._base()
        data["optional_sections"] = ["progress_indicator", "results_cta"]
        data["results_cta"] = {
            "heading": "You're a great match",
            "body": "Based on your answers we found accredited installers near you.",
            "cta_label": "See my quotes",
            "image_url": "https://images.pexels.com/photos/1234/large.jpg",
            "image_alt": "Happy homeowner reviewing solar quotes on a laptop",
        }
        m = SurveyFunnelManifest(**data)
        assert isinstance(m.results_cta, ResultsCta)
        assert m.results_cta.image_url is not None
        # Renders into the HTML, lazy-loaded (below the fold, not the LCP image).
        html = survey_funnel._render_html(m)
        assert 'id="results"' in html
        assert 'loading="lazy"' in html
        assert "Happy homeowner reviewing solar quotes on a laptop" in html

    def test_results_cta_image_optional(self) -> None:
        # The supporting image is optional — a text-only results block is valid.
        data = self._base()
        data["optional_sections"] = ["results_cta"]
        data["results_cta"] = {
            "heading": "You're a great match",
            "body": "We found accredited installers near you.",
            "cta_label": "See my quotes",
        }
        m = SurveyFunnelManifest(**data)
        assert m.results_cta is not None
        assert m.results_cta.image_url is None

    def test_results_cta_image_url_requires_alt(self) -> None:
        data = self._base()
        data["optional_sections"] = ["results_cta"]
        data["results_cta"] = {
            "heading": "Heading here",
            "body": "Body copy long enough to validate cleanly.",
            "cta_label": "Continue",
            "image_url": "https://images.pexels.com/photos/9/large.jpg",
            # image_alt deliberately omitted
        }
        with pytest.raises(ValidationError):
            SurveyFunnelManifest(**data)

    def test_results_cta_flag_requires_data(self) -> None:
        data = self._base()
        data["optional_sections"] = ["results_cta"]  # flag on, no payload
        data.pop("results_cta", None)
        with pytest.raises(ValidationError):
            SurveyFunnelManifest(**data)

    def test_results_cta_data_requires_flag(self) -> None:
        data = self._base()
        data["optional_sections"] = ["progress_indicator"]  # flag missing
        data["results_cta"] = {
            "heading": "Heading here",
            "body": "Body copy long enough to validate cleanly.",
            "cta_label": "Continue",
        }
        with pytest.raises(ValidationError):
            SurveyFunnelManifest(**data)

    def test_contract_lists_results_cta_optional_section(self) -> None:
        data = yaml.safe_load(CONTRACT_PATH.read_text())
        ids = {s["id"] for s in data["optional_sections"]}
        assert "results_cta" in ids
        # Image-source provenance note mirrors landing_page (anti-fabrication).
        assert "image_source" in data["image_contract"]
