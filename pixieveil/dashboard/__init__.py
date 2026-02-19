"""
Dashboard Module

This module provides the web-based dashboard interface for the PixieVeil application.
It includes the main dashboard page and JSON API endpoints for real-time metrics.

Classes:
    Dashboard: Main dashboard class handling web server setup and route management
"""

from .server import Dashboard

__all__ = ['Dashboard', 'image_counter', 'init_image_counter']
