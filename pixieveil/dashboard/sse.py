import asyncio
import logging
import json
from datetime import datetime
from typing import Dict, Any

from aiohttp import web

logger = logging.getLogger(__name__)

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
                # Send status update
                status = {
                    "status": "running",
                    "timestamp": datetime.now().isoformat()
                }
                await stream.write(f"data: {json.dumps(status)}\n\n".encode())
                await asyncio.sleep(5)  # Send update every 5 seconds
        except (asyncio.CancelledError, ConnectionResetError) as e:
            # Normal disconnection scenarios
            logger.debug(f"SSE connection closed: {str(e)}")
        except Exception as e:
            logger.error(f"Error in SSE: {str(e)}")
            
        return stream
