# PixieVeil Agent Guidelines

## Project Overview

PixieVeil is a DICOM anonymization server that receives medical imaging data via C-STORE, anonymizes it according to DICOM PS3.15 standards, and optionally uploads archives to remote storage. It includes a web dashboard for real-time metrics.

## Environment

- **Python**: 3.8+
- **Key Dependencies**: pynetdicom, pydicom, aiohttp, pyyaml, pydantic
- **Entry Point**: `python run.py`

---

## Commands

### Running the Application

```bash
python run.py
```

### Running a Single Test

No formal test suite exists. To run tests manually, use pytest:

```bash
pytest tests/                    # Run all tests
pytest tests/test_file.py        # Run specific test file
pytest tests/test_file.py::test_function  # Run specific test function
```

If pytest is not installed:

```bash
pip install pytest
pytest tests/ -v
```

### Linting and Type Checking

Install development dependencies:

```bash
pip install flake8 mypy pylint black
```

Run linting:

```bash
flake8 pixieveil/ --max-line-length=100
black --check pixieveil/
pylint pixieveil/
mypy pixieveil/
```

---

## Code Style Guidelines

### Imports

- Standard library imports first, then third-party, then local
- Use explicit imports: `from pixieveil.config import Settings`
- Avoid wildcard imports (`from module import *`)
- Group imports with a single blank line between groups

```python
# Standard library
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional

# Third-party
import pydicom
from pydicom.uid import generate_uid
from pynetdicom import AE, evt

# Local
from pixieveil.config import Settings
from pixieveil.storage.storage_manager import StorageManager
```

### Formatting

- Line length: 100 characters maximum
- Indentation: 4 spaces
- Use blank lines sparingly to separate logical sections
- Use spaces around operators (`x = 1`, not `x=1`)
- No trailing whitespace

### Types

- Use type hints for all function parameters and return values
- Use `Optional[X]` for nullable types, not `Union[X, None]`
- Use `Dict`, `List`, `Tuple` from typing (or use lowercase `dict`, `list`, `tuple` for Python 3.9+)

```python
def process_image(ds: pydicom.Dataset, profile: Optional[str] = None) -> bool:
    ...
```

### Naming Conventions

- **Classes**: PascalCase (`Anonymizer`, `DicomServer`, `Settings`)
- **Functions/Variables**: snake_case (`get_patient_id_mapping`, `storage_manager`)
- **Constants**: UPPER_SNAKE_CASE (`MAX_STORAGE_GB`)
- **Private methods**: prefix with underscore (`_apply_field_value_strategy`)
- **Private classes**: prefix with underscore (rarely used)

### Docstrings

All public classes and methods must have docstrings. Use Google-style or NumPy-style:

```python
def anonymize(self, ds: pydicom.Dataset) -> pydicom.Dataset:
    """
    Anonymize a DICOM dataset using the active profile.
    
    Args:
        ds: The DICOM dataset to anonymize.
        
    Returns:
        The anonymized DICOM dataset.
        
    Raises:
        ValueError: If the dataset is invalid.
    """
```

### Error Handling

- Use exceptions for error conditions; avoid returning error codes
- Log errors with appropriate severity before raising
- Use specific exception types when possible
- Handle exceptions at the appropriate level

```python
try:
    result = process_image(ds)
except ValueError as e:
    logger.error(f"Invalid dataset: {e}")
    raise
```

### Logging

- Use `logger = logging.getLogger(__name__)` at module level
- Use appropriate log levels:
  - `DEBUG`: Detailed diagnostic info
  - `INFO`: Confirmation things work as expected
  - `WARNING`: Unexpected but handled gracefully
  - `ERROR`: Serious problem, function couldn't perform
  - `CRITICAL`: Program may crash

```python
logger.debug("Starting DICOM server...")
logger.info(f"DICOM server running on port {self.ae_port}")
logger.error(f"Failed to start DICOM server: {e}")
```

---

## Architecture

```
Modality ──C-STORE──► DicomServer
                           │
                      CStoreSCPHandler
                           │
                      StorageManager
                           │
                      Anonymizer.anonymize()
                           │
                      move to base_path/<study>/<series>/<image>.dcm
                           │
                   check_study_completions()
                         ├─ ZipManager.create_zip()
                         └─ RemoteStorage.upload_file()
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `pixieveil/config/settings.py` | Configuration loading with Pydantic |
| `pixieveil/dicom_server/server.py` | DICOM SCP server (C-ECHO, C-STORE) |
| `pixieveil/processing/anonymizer.py` | DICOM field anonymization |
| `pixieveil/storage/storage_manager.py` | Image processing and archiving |
| `pixieveil/dashboard/server.py` | HTTP dashboard |

---

## Configuration

Configuration is in `config/settings.yaml`. Copy from `config/settings.yaml.example`:

```yaml
dicom_server:
  ae_title: "PIXIEVEIL"
  port: 4070

anonymization:
  profile: "research"  # or "gdpr"
```

---

## Database/Storage

- No database; uses filesystem with study/series hierarchy
- Output: `base_path/<study>/<series>/<image>.dcm`
- Audit log: `mapping_log.jsonl` (JSONL format)
- Archives: ZIP files per completed study
