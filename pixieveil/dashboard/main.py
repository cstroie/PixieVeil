import aiohttp_jinja2
import jinja2
from aiohttp import web
from pixieveil.config.settings import Settings

class Dashboard:
    """
    Main dashboard class handling web server setup and route management.

    This class provides the web-based dashboard interface for the PixieVeil application.
    It manages the HTTP server, handles web routes, and provides real-time updates
    through Server-Sent Events.

    The dashboard provides:
    - Main dashboard page with system status
    - Metrics page showing processing statistics
    - Status page showing current system state
    """

    def __init__(self, settings: Settings, storage_manager):
        self.settings = settings
        self.storage_manager = storage_manager
        self.app = web.Application()
        self.setup_routes()
        self.setup_templates()

    def setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/metrics', self.handle_metrics)
        self.app.router.add_get('/status', self.handle_status)

    def setup_templates(self):
        aiohttp_jinja2.setup(self.app, loader=jinja2.FileSystemLoader('pixieveil/dashboard/templates'))

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', self.settings.dashboard_port)
        await site.start()

    async def stop(self):
        await self.app.shutdown()
        await self.app.cleanup()

    @aiohttp_jinja2.template('index.html')
    async def handle_index(self, request: web.Request) -> web.Response:
        return {}

    @aiohttp_jinja2.template('metrics.html')
    async def handle_metrics(self, request: web.Request) -> web.Response:
        return {}

    @aiohttp_jinja2.template('status.html')
    async def handle_status(self, request: web.Request) -> web.Response:
        return {}
