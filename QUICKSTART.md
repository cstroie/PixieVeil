# PixieVeil Quick Start Guide

This guide will help you get PixieVeil up and running quickly.

## Prerequisites

- Python 3.8+
- pip package manager

## Quick Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd pixieveil
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure settings:**
   ```bash
   cp config/settings.yaml.example config/settings.yaml
   ```

4. **Edit configuration (optional):**
   Open `config/settings.yaml` and adjust settings as needed:
   - DICOM server port and AE title
   - Storage paths
   - Dashboard IP and port

5. **Start the server:**
   ```bash
   python run.py
   ```

## Access the Dashboard

Open your web browser and navigate to:
