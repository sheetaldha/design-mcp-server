"""Pydantic models for the page-meta.yaml frontmatter (Scott-style).

Skill A produces this alongside each <slug>.html. Skill B's Map agent reads it
to know how to populate CMS fields. The audit agent validates against it.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

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


# ---------------------------------------------------------------------------
# Optional-section payloads — structured data the Landing Page family carries
# when the corresponding flag is enabled in `optional_sections`. Skill B's Map
# agent reads these to compile React layout components without HTML-scraping.
# ---------------------------------------------------------------------------

class Testimonial(BaseModel):
    quote: str = Field(min_length=20, max_length=400)
    author: str = Field(min_length=2, max_length=80)
    location: Optional[str] = Field(default=None, max_length=80)
    outcome: Optional[str] = Field(default=None, max_length=120)  # e.g. "Sold $82k above reserve"


class FaqItem(BaseModel):
    question: str = Field(min_length=10, max_length=200)
    answer: str = Field(min_length=20, max_length=800)


class TrustBadge(BaseModel):
    label: str = Field(min_length=2, max_length=80)
    icon_url: Optional[str] = Field(default=None)  # optional logo/icon URL
    detail: Optional[str] = Field(default=None, max_length=120)  # e.g. "4.8★ on Google · 1,200 reviews"


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
    testimonials: Optional[list[Testimonial]] = Field(
        default=None,
        description="Required if 'testimonials' in optional_sections. Min 2, max 6 items.",
    )
    faq: Optional[list[FaqItem]] = Field(
        default=None,
        description="Required if 'faq' in optional_sections. Min 3, max 10 items.",
    )
    trust_badges: Optional[list[TrustBadge]] = Field(
        default=None,
        description="Required if 'trust_badges' in optional_sections. Min 3, max 8 items.",
    )
    theme: ThemeTokens = Field(default_factory=ThemeTokens)

    @model_validator(mode="after")
    def _optional_section_data_matches_flags(self) -> "LandingPageManifest":
        """Enforce a two-way contract between `optional_sections` flags and their
        structured-data payloads.

        - If a flag is set, the corresponding field must be populated within the
          per-section bounds (testimonials 2-6, faq 3-10, trust_badges 3-8).
        - If the data is provided without the flag, refuse it (orphan data would
          silently render nothing and confuse Skill B's Map agent).
        """
        flags = set(self.optional_sections)
        rules: list[tuple[str, str, Optional[list[Any]], int, int]] = [  # type: ignore[name-defined]
            ("testimonials", "testimonials", self.testimonials, 2, 6),
            ("faq", "faq", self.faq, 3, 10),
            ("trust_badges", "trust_badges", self.trust_badges, 3, 8),
        ]
        for flag, field_name, data, lo, hi in rules:
            enabled = flag in flags
            if enabled:
                if data is None:
                    raise ValueError(
                        f"optional_sections includes '{flag}' but `{field_name}` is missing; "
                        f"provide {lo}-{hi} items."
                    )
                if not (lo <= len(data) <= hi):
                    raise ValueError(
                        f"`{field_name}` must contain {lo}-{hi} items when '{flag}' is enabled; "
                        f"got {len(data)}."
                    )
            else:
                if data is not None:
                    raise ValueError(
                        f"`{field_name}` provided without '{flag}' in optional_sections; "
                        f"enable the flag or remove the data (orphan data is refused)."
                    )
        return self


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
