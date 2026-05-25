"""Pydantic models for the page-meta.yaml frontmatter (Scott-style).

Skill A produces this alongside each <slug>.html. Skill B's Map agent reads it
to know how to populate CMS fields. The audit agent validates against it.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class HeroSection(BaseModel):
    headline: str = Field(..., min_length=3, max_length=120)
    subheading: str = Field(..., min_length=3, max_length=300)
    cta_label: str = Field(..., min_length=2, max_length=40)
    cta_url: str = Field(default="#signup")
    image_url: str
    image_alt: str = Field(..., min_length=3)


class FeatureCard(BaseModel):
    heading: str = Field(..., min_length=3, max_length=80)
    paragraph: str = Field(..., min_length=3, max_length=300)
    image_url: str
    image_alt: str = Field(..., min_length=3)


class FormConfig(BaseModel):
    submit_label: str = Field(..., min_length=2, max_length=40)
    privacy_link: str = "#privacy"


class SeoBlock(BaseModel):
    title: str = Field(..., min_length=5, max_length=70)
    meta_description: str = Field(..., min_length=20, max_length=160)
    canonical_url: Optional[HttpUrl] = None
    og_image_url: Optional[str] = None


# Allowlist must match contracts/landing_page.yaml `font_allowlist`.
# These are the only fonts the CMS supports today (existing leadloom dropdown).
ALLOWED_FONTS = {
    "Montserrat",
    "Roboto",
    "Helvetica",
    "Nunito",
    "Open Sans",
    "Century Gothic",
}


class ThemeTokens(BaseModel):
    color_primary: str = "#1F4E79"
    color_accent: str = "#2E75B6"
    color_text_body: str = "#1F2937"
    color_bg_body: str = "#FFFFFF"
    font_heading: str = "Montserrat"
    font_body: str = "Montserrat"

    @field_validator("font_heading", "font_body")
    @classmethod
    def _font_must_be_allowed(cls, v: str) -> str:
        if v not in ALLOWED_FONTS:
            raise ValueError(
                f"font '{v}' not in CMS allowlist {sorted(ALLOWED_FONTS)}. "
                f"Adding a new font requires updating the leadloom CMS dropdown first."
            )
        return v


class LandingPageManifest(BaseModel):
    """The page-meta.yaml schema for a Landing Page design."""
    family: Literal["landing-page"] = "landing-page"
    version: int = 1
    slug: str = Field(..., pattern=r"^[a-z0-9-]+$", min_length=3, max_length=60)
    published: bool = False
    intent: str = Field(..., min_length=10, description="One-paragraph statement of what this site sells / who it targets")
    seo: SeoBlock
    hero: HeroSection
    features: list[FeatureCard] = Field(..., min_length=3, max_length=3)
    form: FormConfig
    optional_sections: list[Literal["testimonials", "faq", "trust_badges", "sticky_cta_mobile"]] = Field(default_factory=list)
    theme: ThemeTokens = Field(default_factory=ThemeTokens)


# ---------------------------------------------------------------------------
# Survey Funnel family — multi-step lead capture (1..5 steps, optional OTP).
# Mirrors LandingPageManifest's shape; replaces `features` with `steps[]` and
# `form` with `submit_label` + `otp_enabled`. SEO / theme / hero are identical.
# ---------------------------------------------------------------------------

# Allowed HTML input types for FormQuestion. Kept narrow on purpose — the
# generator's renderer + Skill B's audit both branch on this enum.
QuestionType = Literal["text", "email", "tel", "radio", "select", "checkbox"]


class FormQuestion(BaseModel):
    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$", min_length=2, max_length=40)
    type: QuestionType
    label: str = Field(..., min_length=2, max_length=120)
    options: Optional[list[str]] = Field(
        default=None,
        description="Required for radio/select/checkbox question types; ignored otherwise.",
    )
    required: bool = True

    @model_validator(mode="after")
    def _options_only_for_choice_types(self) -> "FormQuestion":
        # Use a model_validator so the check fires even when `options` is omitted
        # (field_validator on `options` does not run for a None default).
        if self.type in {"radio", "select", "checkbox"}:
            if not self.options or len(self.options) < 2:
                raise ValueError(
                    f"question type '{self.type}' requires at least 2 options"
                )
        else:
            if self.options:
                raise ValueError(
                    f"question type '{self.type}' must not have options (got {self.options})"
                )
        return self


class FormStep(BaseModel):
    id: str = Field(..., pattern=r"^step-[0-9]+$", description="Stable id, e.g. step-1, step-2")
    heading: str = Field(..., min_length=3, max_length=120)
    questions: list[FormQuestion] = Field(..., min_length=1, max_length=10)


class SurveyFunnelManifest(BaseModel):
    """The page-meta.yaml schema for a Survey Funnel design."""
    family: Literal["survey-funnel"] = "survey-funnel"
    version: int = 1
    slug: str = Field(..., pattern=r"^[a-z0-9-]+$", min_length=3, max_length=60)
    published: bool = False
    intent: str = Field(..., min_length=10, description="One-paragraph statement of who/what this funnel qualifies")
    seo: SeoBlock
    hero: HeroSection
    steps: list[FormStep] = Field(..., min_length=1, max_length=5)
    otp_enabled: bool = Field(
        default=False,
        description=(
            "Per-site CMS toggle (manage_sites.otp_enabled). When true, an OTP "
            "verification step renders between the final fieldset and submit."
        ),
    )
    submit_label: str = Field(..., min_length=2, max_length=40)
    optional_sections: list[Literal["progress_indicator", "trust_badges", "testimonials", "sticky_cta_mobile"]] = Field(default_factory=list)
    theme: ThemeTokens = Field(default_factory=ThemeTokens)
