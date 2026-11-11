"""
Fixed RHYTHM vocabularies served to the dashboard via ``/api/options``.

Kept server-side (not hard-coded in the page's JavaScript) so there is one
source of truth for the dropdowns, and so the scanner list can come from the
same ``scanner_lookup.yaml`` that ExamExtractor matches against.
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


def scanner_options(scanner_map: dict[str, str]) -> list[dict[str, str]]:
    """Turn scanner_lookup.yaml's ``{"MAKE|Model": uuid}`` map into
    ``[{"label": "MAKE Model", "id": uuid}]``, skipping malformed entries."""
    out = []
    for key, uuid in scanner_map.items():
        if not isinstance(key, str) or not isinstance(uuid, str) or not uuid:
            continue
        out.append({"label": key.replace("|", " "), "id": uuid})
    return sorted(out, key=lambda o: o["label"])
