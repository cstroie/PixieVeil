"""
Metrics Module

This module provides functionality for tracking and counting processed images.
It serves as a central point for metrics collection used by the dashboard.

Classes:
    ImageCounter: Thread-safe counter for tracking processed images
"""

import threading
from typing import Dict, Any

# Create a global instance of ImageCounter for tracking metrics
image_counter = None


class ImageCounter:
    """
    Thread-safe counter for tracking processed images.
    
    This class provides a thread-safe mechanism to count and track the number
    of DICOM images processed by the application. It uses a lock to ensure
    atomic operations when incrementing the count.
    
    Attributes:
        _count (int): Internal counter for processed images
        _lock (threading.Lock): Lock for thread-safe operations
    """
    
    def __init__(self):
        """
        Initialize the ImageCounter with zero count and a thread lock.
        """
        self._count = 0
        self._lock = threading.Lock()
    
    def increment(self):
        """
        Increment the image counter by one in a thread-safe manner.
        
        This method safely increments the internal counter using a lock
        to prevent race conditions in multi-threaded environments.
        """
        with self._lock:
            self._count += 1
    
    def get_count(self):
        """
        Get the current image count in a thread-safe manner.
        
        Returns:
            int: Current count of processed images
        """
        with self._lock:
            return self._count


def init_image_counter():
    """
    Initialize the global image counter instance.
    
    This function should be called once during application startup
    to ensure the global image counter is properly initialized.
    """
    global image_counter
    if image_counter is None:
        image_counter = ImageCounter()
