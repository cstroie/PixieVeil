"""
Configuration Settings Module

This module provides configuration management for the PixieVeil application.
It handles loading and validation of application settings from YAML configuration files.

Classes:
    Settings: Main configuration class that manages all application settings
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

class Settings(BaseModel):
    """
    Main configuration class that manages all application settings.
    
    This class provides centralized configuration management for the PixieVeil application.
    It uses Pydantic for data validation and type checking, ensuring that all configuration
    values are properly validated against expected types and constraints.
    
    The configuration includes settings for:
    - DICOM server configuration (port, AE title, etc.)
    - Anonymization rules and preferences
    - Storage paths and remote storage settings
    - HTTP server configuration for the dashboard
    - Study completion timeout settings
    - Series filtering criteria
    - Logging configuration
    - Anonymization profiles (GDPR, RESEARCH, etc.)
    
    Attributes:
        dicom_server (Dict[str, Any]): Configuration for DICOM server settings
        anonymization (Dict[str, Any]): Configuration for DICOM anonymization rules
        storage (Dict[str, Any]): Configuration for storage paths and remote storage
        http_server (Dict[str, Any]): Configuration for HTTP server settings
        study (Dict[str, Any]): Configuration for study completion settings
        series_filter (Dict[str, Any]): Configuration for series filtering criteria
        logging (Dict[str, Any]): Configuration for logging settings
        anonymization_profiles (Dict[str, Dict[str, Any]]): Configuration for anonymization profiles
        
    Note:
        All configuration sections use default empty dictionaries to ensure
        the application can start even if specific sections are not defined
        in the configuration file.
    """
    
    dicom_server: Dict[str, Any] = Field(default_factory=dict)
    anonymization: Dict[str, Any] = Field(default_factory=dict)
    storage: Dict[str, Any] = Field(default_factory=dict)
    http_server: Dict[str, Any] = Field(default_factory=dict)
    study: Dict[str, Any] = Field(default_factory=dict)
    series_filter: Dict[str, Any] = Field(default_factory=dict)
    logging: Dict[str, Any] = Field(default_factory=dict)
    anonymization_profiles: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    def get_anonymization_profile(self, profile_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get the anonymization profile configuration.
        
        Args:
            profile_name (Optional[str]): Name of the profile to retrieve.
                                        If None, uses the default profile from settings.
                
        Returns:
            Dict[str, Any]: The anonymization profile configuration
        """
        if profile_name is None:
            # Get default profile from settings
            profile_name = self.anonymization.get("default", "RESEARCH")
        
        # Get profiles from nested structure
        profiles = self.anonymization.get("profiles", {})
        
        # Return the requested profile or empty dict if not found
        return profiles.get(profile_name, {})

    @classmethod
    def load(cls, config_path: Path = None) -> "Settings":
        """
        Load application settings from a YAML configuration file.
        
        This class method loads and validates application settings from a YAML file.
        It provides intelligent fallback behavior to ensure the application can
        start even if the primary configuration file is missing.
        
        The method follows this priority order for configuration file loading:
        1. Custom config_path if provided
        2. config/settings.yaml if it exists
        3. config/settings.yaml.example as fallback
        
        Args:
            config_path (Path, optional): Custom path to configuration file.
                                        If None, uses default locations.
                
        Returns:
            Settings: A validated Settings instance containing all configuration data
                
        Raises:
            FileNotFoundError: If no configuration file can be found
            yaml.YAMLError: If the configuration file contains invalid YAML
            ValidationError: If the configuration data doesn't match expected schema
                
        Note:
            The method uses Pydantic's validation to ensure all configuration
            values are properly typed and validated before returning the Settings instance.
        """
        if config_path is None:
            config_path = Path("config/settings.yaml")
            if not config_path.exists():
                config_path = Path("config/settings.yaml.example")

        with open(config_path, "r") as f:
            config_data = yaml.safe_load(f)

        return cls(**config_data)
