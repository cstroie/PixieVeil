"""
Fixed RHYTHM vocabularies served to the dashboard via ``/api/options``.

Kept server-side (not hard-coded in the page's JavaScript) so there is one
source of truth for the dropdowns, and so the scanner list can come from the
same protocol catalog (``config/protocols.yaml``) ExamExtractor matches against.
"""

# The 8 (region, clinical indication, IV contrast) combinations RHYTHM's
# clinical_rows define — see integrations/rhythm/*_protocol.yaml.
REGION_INDICATIONS: list[dict[str, str]] = [
    {"region": "Head", "indication": "Trauma", "contrast": "Non-contrast"},
    {"region": "Mastoid bone/Inner Ear",
     "indication": "Hearing loss; congenital malformations, infection, cholesteatoma, "
                   "cochlear implants",
     "contrast": "Non-contrast"},
    {"region": "Chest", "indication": "Fungal infections", "contrast": "Non-contrast"},
    {"region": "Chest", "indication": "Complicated infections", "contrast": "Contrast-enhanced"},
    {"region": "Chest/HRCT (Inspiration/Expiration)",
     "indication": "Interstitial lung diseases, small airways disease, cystic fibrosis, asthma, "
                   "primary ciliary dyskinesia, chronic lung disease of prematurity",
     "contrast": "Non-contrast"},
    {"region": "Chest-Abdomen",
     "indication": "Tumor staging & follow-up (Wilms tumor, neuroblastoma, other)",
     "contrast": "Contrast-enhanced"},
    {"region": "Abdomen", "indication": "Acute abdomen", "contrast": "Contrast-enhanced"},
    {"region": "Neck-Chest-Abdomen", "indication": "Lymphoma", "contrast": "Contrast-enhanced"},
]

IMAGE_QUALITY: list[str] = ["Diagnostic", "Suboptimal", "Non-diagnostic"]

IV_CONTRAST: list[str] = ["Non-contrast", "Contrast-enhanced"]


def scanner_options(catalog) -> list[dict]:
    """Scanners from the protocol catalog for the review form's dropdown:
    ``[{"label", "id" (RHYTHM scanner UUID or None), "catalog_id"}]``.
    Wildcard ("any scanner") groups are skipped."""
    out = [{"label": s.label, "id": s.rhythm_scanner_id, "catalog_id": s.id}
           for s in catalog.scanners if not s.is_wildcard]
    return sorted(out, key=lambda o: o["label"])
