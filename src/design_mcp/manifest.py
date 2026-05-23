"""Pydantic models for the page-meta.yaml frontmatter (Scott-style).

Skill A produces this alongside each <slug>.html. Skill B's Map agent reads it
to know how to populate CMS fields. The audit agent validates against it.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


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


class ThemeTokens(BaseModel):
    color_primary: str = "#1F4E79"
    color_accent: str = "#2E75B6"
    color_text_body: str = "#1F2937"
    color_bg_body: str = "#FFFFFF"
    font_heading: str = "Inter, sans-serif"
    font_body: str = "Inter, sans-serif"


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
