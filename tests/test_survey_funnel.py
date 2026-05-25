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
    # Final form posts to the Acquirely backend
    assert 'action="/api/handle_Client_Lead_Submission"' in html
    # Stub uses otp_enabled=False → no OTP section emitted
    assert m.otp_enabled is False
    assert 'class="otp' not in html


def test_render_with_otp_enabled_emits_otp_section() -> None:
    data = yaml.safe_load(SAMPLE_MANIFEST_YAML)  # otp_enabled=true in the sample
    m = SurveyFunnelManifest(**data)
    html = survey_funnel._render_html(m)
    assert 'class="otp' in html
    assert "/api/verificationsms" in html
