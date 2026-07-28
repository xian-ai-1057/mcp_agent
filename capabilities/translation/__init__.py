"""Glossary-backed translation profile and quality policy."""

from capabilities.translation.policy import TranslationSelfCheck
from capabilities.translation.prompts import TRANSLATION_RULES

__all__ = ["TRANSLATION_RULES", "TranslationSelfCheck"]
