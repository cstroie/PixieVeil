"""
Server-Sent Events (SSE) Module

This module provides functionality for real-time updates to the web dashboard
using Server-Sent Events. It enables live monitoring of system status and metrics.

Classes:
    ImageCounter: Thread-safe counter for tracking processed images
    ServerSentEvents: Handles SSE connections and broadcasts real-time updates
"""

import asyncio
import logging
import json
import threading
from datetime import datetime
from typing import Dict, Any

from aiohttp import web

logger = logging.getLogger(__name__)


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


# Create a global instance of ImageCounter
image_counter = ImageCounter()


class ServerSentEvents:
    """
    Handles Server-Sent Events for real-time dashboard updates.
    
    This class manages SSE connections and broadcasts real-time updates
    to connected web clients. It provides live monitoring of system status,
    image processing metrics, and study completion information.
    
    The SSE handler broadcasts updates every 5 seconds with:
    - Current server status
    - Timestamp of the update
    - Total number of processed images
    - Number of completed studies
    
    Attributes:
        None (uses class-level ImageCounter instance)
    """
    
    async def handle_events(self, request: web.Request) -> web.StreamResponse:
        """
        Handle Server-Sent Events for real-time updates.
        
        This method establishes and maintains an SSE connection with a client,
        broadcasting periodic updates about system status and metrics.
        
        The method handles:
        - Connection establishment and maintenance
        - Periodic status broadcasts every 5 seconds
        - Graceful disconnection handling
        - Error logging for connection issues
        
        Args:
            request (web.Request): The incoming HTTP request from the client
            
        Returns:
            web.StreamResponse: SSE stream response for real-time updates
            
        Note:
            The method continues running until the client disconnects or
            the connection is closed. It handles normal disconnection
            scenarios gracefully and logs errors for unexpected issues.
        """
        stream = web.StreamResponse()
        stream.content_type = "text/event-stream"
        await stream.prepare(request)
        
        try:
            while not request.transport.is_closing():
                # Send status update with image count and completed studies
                status = {
                    "status": "running",
                    "timestamp": datetime.now().isoformat(),
                    "image_count": image_counter.get_count(),
                    "completed_studies": request.app['storage_manager'].completed_count
                }
                await stream.write(f"data: {json.dumps(status)}\n\n".encode())
                await asyncio.sleep(5)  # Send update every 5 seconds
        except (asyncio.CancelledError, ConnectionResetError) as e:
            # Normal disconnection scenarios
            logger.debug(f"SSE connection closed: {str(e)}")
        except Exception as e:
            logger.error(f"Error in SSE: {str(e)}")
            
        return stream
