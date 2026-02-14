# PixieVeil

PixieVeil is a DICOM anonymization server that receives DICOM images from medical modalities, anonymizes them according to configurable rules, and provides access to the anonymized data through HTTP.

## Features

- **DICOM Server**: Receives DICOM images from modalities using pynetdicom
- **Anonymization**: Comprehensive DICOM field anonymization compliant with DICOM PS3.15 standards
- **Study Management**: Assembles complete studies and handles multi-series studies with automatic completion detection
- **Series Filtering**: Configurable filtering based on modality and series characteristics
- **Storage**: Local storage with organized structure, ZIP archive creation, and optional remote storage upload
- **HTTP Dashboard**: Real-time web dashboard with live metrics and status updates via Server-Sent Events
- **Processing Pipeline**: Comprehensive image processing pipeline with validation, filtering, and anonymization
- **Logging**: Configurable logging with file and console output

## Architecture

PixieVeil is built with a modular architecture:

- **DICOM Server**: Handles incoming DICOM connections and C-STORE requests
- **Storage Manager**: Manages DICOM image storage, processing, and study completion monitoring
- **Processing Pipeline**: Orchestrates image validation, filtering, and anonymization
- **Dashboard**: Web-based interface for monitoring and management
- **Remote Storage**: Optional integration with remote storage services

## Requirements

- Python 3.8+
- pynetdicom>=2.0.0
- pydicom>=2.0.0
- aiohttp>=3.0.0
- pydantic
- pyyaml

## Installation

1. Clone the repository:
   ```bash
   git clone <repository-url>
   cd pixieveil
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure settings:
   ```bash
   cp config/settings.yaml.example config/settings.yaml
   # Edit config/settings.yaml with your preferences
   ```

4. Start the server:
   ```bash
   python run.py
   ```

## Configuration

The application is configured via `config/settings.yaml`. The configuration includes:

### DICOM Server
- `port`: DICOM server port (default: 11112)
- `ae_title`: Application Entity title

### Storage
- `base_path`: Base directory for storing DICOM studies
- `temp_path`: Temporary directory for incoming images
- `remote_storage`: Optional remote storage configuration

### Study Management
- `completion_timeout`: Timeout for study completion detection (seconds)

### Series Filtering
- `exclude_modalities`: List of modalities to exclude
- `keep_original_series`: Whether to keep only original series

### Anonymization
- Configuration for DICOM field anonymization rules

### HTTP Server
- `ip`: Dashboard IP address
- `port`: Dashboard port

### Logging
- `level`: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

## Usage

### Starting the Server

