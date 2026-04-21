"""
Configuration Settings Module

This module provides configuration management for the PixieVeil application.
It handles loading and validation of application settings from YAML configuration files.

Classes:
    AnonymizationProfile: Configuration for a single anonymization profile
    Settings: Main configuration class that manages all application settings
"""

import yaml
from pathlib import Path
from typing import Dict, Any, Optional, Union
from pydantic import BaseModel, Field


class AnonymizationProfile(BaseModel):
    """
    Configuration for a single anonymization profile.
    
    This class defines how specific DICOM fields should be handled during anonymization.
    Field values can be:
    - null: clear the field (set to empty string)
    - "PSEUDO": replace with deterministic pseudonym based on original value
    - "NEWUID": generate a new DICOM UID
    - "KEEP": retain the original value without modification
    - "CLEAR": set to empty string (same as null)
    - string literal: replace with the specified string (e.g., "ANON", "DEID_CENTER")
    
    Attributes:
        PatientName: How to handle patient name
        PatientID: How to handle patient ID
        PatientBirthDate: How to handle birth date
        PatientAge: How to handle patient age
        PatientSex: How to handle patient sex
        InstitutionName: How to handle institution name
        StudyID: How to handle study ID
        StudyInstanceUID: How to handle study UID strategy
        StudyDescription: How to handle study description
        SeriesInstanceUID: How to handle series UID strategy
        SeriesDescription: How to handle series description
        FrameOfReferenceUID: How to handle frame of reference UID strategy
        ReferringPhysicianName: How to handle referring physician
        OperatorsName: How to handle operator name
        PerformingPhysicianName: How to handle performing physician
        AccessionNumber: How to handle accession number
        KeepPrivateTags: If False, remove private DICOM tags
        PixelBlackout: If True, set all pixel data to zeros
        RetainStudyDate: If True, keep study date and time unchanged
    """
    
    PatientName: Optional[str] = None
    PatientID: Optional[str] = None
    PatientBirthDate: Optional[str] = None
    PatientAge: Optional[str] = None
    PatientSex: Optional[str] = None
    InstitutionName: Optional[str] = None
    StudyID: Optional[str] = None
    StudyInstanceUID: Optional[str] = None
    StudyDescription: Optional[str] = None
    SeriesInstanceUID: Optional[str] = None
    SeriesDescription: Optional[str] = None
    FrameOfReferenceUID: Optional[str] = None
    ReferringPhysicianName: Optional[str] = None
    OperatorsName: Optional[str] = None
    PerformingPhysicianName: Optional[str] = None
    AccessionNumber: Optional[str] = None
    KeepPrivateTags: bool = False
    PixelBlackout: bool = False
    RetainStudyDate: bool = False


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
    
    Attributes:
        dicom_server (Dict[str, Any]): Configuration for DICOM server settings
        anonymization (Dict[str, Any]): Configuration for DICOM anonymization rules
        storage (Dict[str, Any]): Configuration for storage paths and remote storage
        http_server (Dict[str, Any]): Configuration for HTTP server settings
        study (Dict[str, Any]): Configuration for study completion settings
        series_filter (Dict[str, Any]): Configuration for series filtering criteria
        logging (Dict[str, Any]): Configuration for logging settings
        
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
    defacing: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Settings":
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
        # After attempting fallbacks, ensure a file actually exists
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        with open(config_path, "r") as f:
            # yaml.safe_load may return None for empty files; default to empty dict
            config_data = yaml.safe_load(f) or {}

        return cls(**config_data)
    
    def get_anonymization_profile(self, profile_name: Optional[str] = None) -> AnonymizationProfile:
        """
        Get an anonymization profile by name, with fallback to default profile.
        
        Args:
            profile_name (str, optional): Name of the profile to retrieve. If None,
                                        uses the default profile from config.
                
        Returns:
            AnonymizationProfile: The requested or default anonymization profile
                
        Raises:
            ValueError: If the requested profile does not exist
            
        Note:
            If the anonymization section is not configured, returns a default
            research profile for backward compatibility.
        """
        if not self.anonymization:
            # Default profile for backward compatibility
            return AnonymizationProfile(
                PatientName="PSEUDO",
                PatientID="PSEUDO",
                PatientBirthDate="CLEAR",
                PatientAge="KEEP",
                PatientSex="KEEP",
                InstitutionName="DEID_CENTER",
                StudyID="RESEARCH",
                StudyInstanceUID="PSEUDOUID",
                StudyDescription="KEEP",
                SeriesInstanceUID="PSEUDOUID",
                SeriesDescription="KEEP",
                FrameOfReferenceUID="PSEUDOUID",
                ReferringPhysicianName="CLEAR",
                OperatorsName="CLEAR",
                PerformingPhysicianName="CLEAR",
                AccessionNumber="CLEAR",
                KeepPrivateTags=False,
                PixelBlackout=False,
                RetainStudyDate=True
            )
        
        # Determine which profile to use
        if profile_name is None:
            profile_name = self.anonymization.get("profile", "research")
        
        # Get profiles dict
        profiles = self.anonymization.get("profiles", {})
        
        if profile_name not in profiles:
            raise ValueError(
                f"Anonymization profile '{profile_name}' not found. "
                f"Available profiles: {list(profiles.keys())}"
            )
        
        profile_data = profiles[profile_name]
        return AnonymizationProfile(**profile_data)
