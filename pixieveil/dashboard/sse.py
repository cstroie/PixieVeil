import asyncio
import logging
import json
import threading
from datetime import datetime
from typing import Dict, Any

from aiohttp import web

logger = logging.getLogger(__name__)

class ImageCounter:
    """Thread-safe image counter"""
    def __init__(self):
        self._count = 0
        self._lock = threading.Lock()
    
    def increment(self):
        with self._lock:
            self._count += 1
    
    def get_count(self):
        with self._lock:
            return self._count

image_counter = ImageCounter()

class ServerSentEvents:
    async def handle_events(self, request: web.Request) -> web.StreamResponse:
        """
        Handle Server-Sent Events for real-time updates.
        """
        stream = web.StreamResponse()
        stream.content_type = "text/event-stream"
        await stream.prepare(request)
        
        try:
            while not request.transport.is_closing():
                from pixieveil.storage import StorageManager
                # Send status update with image count
                status = {
                    "status": "running",
                    "timestamp": datetime.now().isoformat(),
                    "image_count": image_counter.get_count(),
                    "completed_studies": StorageManager.completed_count
                }
                await stream.write(f"data: {json.dumps(status)}\n\n".encode())
                await asyncio.sleep(5)  # Send update every 5 seconds
        except (asyncio.CancelledError, ConnectionResetError) as e:
            # Normal disconnection scenarios
            logger.debug(f"SSE connection closed: {str(e)}")
        except Exception as e:
            logger.error(f"Error in SSE: {str(e)}")
            
        return stream
