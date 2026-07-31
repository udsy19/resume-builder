"""
LaTeX template registry.

Built-in templates live in templates/*.tex. Users may also upload a custom
template — LaTeX only.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

DEFAULT_TEMPLATE_ID = "udaya"


@dataclass
class Template:
    id: str
    name: str
    description: str

    @property
    def path(self) -> Path:
        return TEMPLATES_DIR / f"{self.id}.tex"

    def read(self) -> str:
        return self.path.read_text()


BUILTIN_TEMPLATES: List[Template] = [
    Template(
        id="udaya",
        name="Udaya's Template",
        description="Dense single-page layout with bold dates/locations, linked certifications, and categorized skills rows. Default.",
    ),
    Template(
        id="jakes",
        name="Jake's Resume",
        description="The classic Jake Gutierrez template — clean single-column layout with project headings and a compact skills block.",
    ),
    Template(
        id="mst",
        name="Big Tech New Grad",
        description="Student/new-grad layout with a relevant-coursework grid under Education and an Activities & Honors section.",
    ),
    Template(
        id="sb2nov",
        name="Software Engineer",
        description="Sourabh Bajaj's template — titled bullet items (Project: description) suited to experienced engineers.",
    ),
]


def list_templates() -> List[dict]:
    return [
        {"id": t.id, "name": t.name, "description": t.description, "default": t.id == DEFAULT_TEMPLATE_ID}
        for t in BUILTIN_TEMPLATES
    ]


def get_template(template_id: str) -> Optional[Template]:
    return next((t for t in BUILTIN_TEMPLATES if t.id == template_id), None)


MAX_TEMPLATE_BYTES = 64 * 1024

# Commands that read/execute outside the document — no resume template needs them.
_DANGEROUS = re.compile(
    r"\\write18|\\openin|\\openout|\\immediate\s*\\write"
    r"|\\(?:input|include)\s*\{\s*(?:/|\.\.|[a-zA-Z]:)"  # absolute or parent paths
)


def validate_custom_template(latex: str) -> Optional[str]:
    """Return an error message if the uploaded template is not usable LaTeX, else None."""
    if len(latex.encode()) > MAX_TEMPLATE_BYTES:
        return "Template is too large (max 64KB) — resume templates should be small."
    if "\\documentclass" not in latex:
        return "Template must be a complete LaTeX document (missing \\documentclass)."
    if "\\begin{document}" not in latex or "\\end{document}" not in latex:
        return "Template must contain \\begin{document} ... \\end{document}."
    if _DANGEROUS.search(latex):
        return "Template uses file-access commands (\\write18, \\openin, absolute \\input paths) that aren't allowed."
    return None
