"""The faces the paper can be set in: one table, read by both sides.

`FONT_FILES` maps the family name the reader picks on the Look tab (the
strings in `app.settings.FONT_CHOICES_*`) to the files bundled in `fonts/`,
each with the `font-weight` and `font-style` its `@font-face` carries.
`render/template.html` loops over it to set the sheet; `GET /fonts.css`
serves the same faces to the Look tab's picker, so the sample on the page
is the face that comes out of the printer. Add a family in one place and
both agree; there is nowhere for them to disagree.

`STACKS` is the CSS stack each family is set in, fallbacks included: an
unknown name is not in it, and the template falls back to the default.

Every file here is under the SIL Open Font License; each family's licence
travels with it as `fonts/OFL-<Family>.txt`.
"""
from __future__ import annotations

from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"

#: The weight range a variable face covers: one file for the whole axis.
VAR = "100 900"

#: family -> [(file in fonts/, font-weight, font-style)]
FONT_FILES: dict[str, list[tuple[str, str, str]]] = {
    # -- mastheads ------------------------------------------------------
    "Maguntia": [
        ("UnifrakturMaguntia-Book.ttf", "normal", "normal"),
    ],
    "UnifrakturCook": [
        ("UnifrakturCook-Bold.ttf", VAR, "normal"),
    ],
    "Pirata One": [
        ("PirataOne-Regular.ttf", VAR, "normal"),
    ],
    "Grenze Gotisch": [
        ("GrenzeGotisch-var.ttf", VAR, "normal"),
    ],
    "Cinzel Decorative": [
        ("CinzelDecorative-Regular.ttf", "normal", "normal"),
        ("CinzelDecorative-Bold.ttf", "bold", "normal"),
    ],
    # -- text and headline faces ---------------------------------------
    "Old Standard": [
        ("OldStandard-Regular.ttf", "normal", "normal"),
        ("OldStandard-Bold.ttf", "bold", "normal"),
        ("OldStandard-Italic.ttf", "normal", "italic"),
    ],
    "PT Serif": [
        ("PT_Serif-Web-Regular.ttf", "normal", "normal"),
        ("PT_Serif-Web-Bold.ttf", "bold", "normal"),
        ("PT_Serif-Web-Italic.ttf", "normal", "italic"),
        ("PT_Serif-Web-BoldItalic.ttf", "bold", "italic"),
    ],
    "EB Garamond": [
        ("EBGaramond-var.ttf", VAR, "normal"),
        ("EBGaramond-Italic-var.ttf", VAR, "italic"),
    ],
    "Libre Baskerville": [
        ("LibreBaskerville-var.ttf", VAR, "normal"),
        ("LibreBaskerville-Italic-var.ttf", VAR, "italic"),
    ],
    "Playfair Display": [
        ("PlayfairDisplay-var.ttf", VAR, "normal"),
        ("PlayfairDisplay-Italic-var.ttf", VAR, "italic"),
    ],
    "Merriweather": [
        ("Merriweather-var.ttf", VAR, "normal"),
        ("Merriweather-Italic-var.ttf", VAR, "italic"),
    ],
    "Source Serif 4": [
        ("SourceSerif4-var.ttf", VAR, "normal"),
        ("SourceSerif4-Italic-var.ttf", VAR, "italic"),
    ],
    "Crimson Pro": [
        ("CrimsonPro-var.ttf", VAR, "normal"),
        ("CrimsonPro-Italic-var.ttf", VAR, "italic"),
    ],
    "Lora": [
        ("Lora-var.ttf", VAR, "normal"),
        ("Lora-Italic-var.ttf", VAR, "italic"),
    ],
    "Literata": [
        ("Literata-var.ttf", VAR, "normal"),
        ("Literata-Italic-var.ttf", VAR, "italic"),
    ],
    "Bodoni Moda": [
        ("BodoniModa-var.ttf", VAR, "normal"),
        ("BodoniModa-Italic-var.ttf", VAR, "italic"),
    ],
    "Cormorant Garamond": [
        ("CormorantGaramond-var.ttf", VAR, "normal"),
        ("CormorantGaramond-Italic-var.ttf", VAR, "italic"),
    ],
    "Libre Caslon Text": [
        ("LibreCaslonText-var.ttf", VAR, "normal"),
        ("LibreCaslonText-Italic-var.ttf", VAR, "italic"),
    ],
    "Oswald": [
        ("Oswald-var.ttf", VAR, "normal"),
    ],
}

#: family -> the CSS stack it is set in, with the fallbacks a machine
#: without the bundled file would reach for.
STACKS: dict[str, str] = {
    "Maguntia": '"Maguntia", "Old English Text MT", serif',
    "UnifrakturCook": '"UnifrakturCook", "Old English Text MT", serif',
    "Pirata One": '"Pirata One", "Old English Text MT", serif',
    "Grenze Gotisch": '"Grenze Gotisch", "Old English Text MT", serif',
    "Cinzel Decorative": '"Cinzel Decorative", "Times New Roman", serif',
    "Old Standard": '"Old Standard", "Times New Roman", serif',
    "PT Serif": '"PT Serif", Georgia, serif',
    "EB Garamond": '"EB Garamond", Garamond, Georgia, serif',
    "Libre Baskerville": '"Libre Baskerville", Baskerville, Georgia, serif',
    "Playfair Display": '"Playfair Display", "Times New Roman", serif',
    "Merriweather": '"Merriweather", Georgia, serif',
    "Source Serif 4": '"Source Serif 4", Georgia, serif',
    "Crimson Pro": '"Crimson Pro", Garamond, Georgia, serif',
    "Lora": '"Lora", Georgia, serif',
    "Literata": '"Literata", Georgia, serif',
    "Bodoni Moda": '"Bodoni Moda", Didot, "Times New Roman", serif',
    "Cormorant Garamond": '"Cormorant Garamond", Garamond, Georgia, serif',
    "Libre Caslon Text": '"Libre Caslon Text", "Times New Roman", serif',
    "Oswald": '"Oswald", "Helvetica Neue", Arial, sans-serif',
}


def stack(name: str, fallback: str = "Old Standard") -> str:
    """The CSS stack for a family, or the fallback family's stack.

    An unknown name never fails: the paper is set in the default face, the
    same way `template.html` falls back.
    """
    return STACKS.get(name) or STACKS[fallback]


def faces(name: str) -> list[tuple[str, str, str]]:
    """`(file, weight, style)` for one family; empty for an unknown name."""
    return FONT_FILES.get(name, [])


def face_css(base_url: str = "/fonts") -> str:
    """The `@font-face` rules for every bundled family, one per line.

    `base_url` is where the files are served from: `/fonts` for the web
    page, a `file://` directory for the render.
    """
    lines = []
    for family, rules in FONT_FILES.items():
        for file, weight, style in rules:
            lines.append(
                f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
                f'font-style: {style}; src: url("{base_url}/{file}"); }}'
            )
    return "\n".join(lines) + "\n"


def missing_files() -> list[str]:
    """Files the table names that are not in `fonts/`. Empty is the rule."""
    return [f for rules in FONT_FILES.values() for f, _, _ in rules
            if not (FONT_DIR / f).is_file()]
