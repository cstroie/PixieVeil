# PixieVeil

PixieVeil is a DICOM anonymization server that receives DICOM images from medical modalities, anonymizes them according to configurable rules, and provides access to the anonymized data through HTTP.

## Features

- **DICOM Server**: Receives DICOM images from modalities using pynetdicom
- **Anonymization**: Integrates with dicom_anonymization for GDPR-strict and research profiles
- **Study Management**: Assembles complete studies and handles multi-series studies
- **Series Filtering**: Identifies and keeps only original series
- **Storage**: Local storage with organized structure and zip file creation
- **HTTP Export**: Provides access to anonymized data through HTTP API
- **Dashboard**: Real-time status updates and metrics display

## Requirements

- Python 3.8+
- pynetdicom
- pydicom
- aiohttp
- dicom_anonymization

## Installation

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Configure settings: `cp config/settings.yaml.example config/settings.yaml`
4. Start the server: `python -m pixieveil`

## Configuration

See `config/settings.yaml` for configuration options.

## License

This project is licensed under the GPL3 License - see the LICENSE file for details.
