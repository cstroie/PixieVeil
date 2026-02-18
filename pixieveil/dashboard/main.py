import logging
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
        self.logger = logging.getLogger(__name__)

    def setup_routes(self):
        self.app.router.add_get('/', self.handle_index)
        self.app.router.add_get('/metrics', self.handle_metrics)
        self.app.router.add_get('/status', self.handle_status)

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', self.settings.dashboard_port)
        await site.start()
        self.logger.info(f"Dashboard started on port {self.settings.dashboard_port}")

    async def stop(self):
        await self.app.shutdown()
        await self.app.cleanup()
        self.logger.info("Dashboard stopped")

    async def handle_index(self, request: web.Request) -> web.Response:
        self.logger.info("Handling index request")
        template = self.load_template('index.html')
        return web.Response(text=template, content_type='text/html')

    async def handle_metrics(self, request: web.Request) -> web.Response:
        self.logger.info("Handling metrics request")
        template = self.load_template('metrics.html')
        return web.Response(text=template, content_type='text/html')

    async def handle_status(self, request: web.Request) -> web.Response:
        self.logger.info("Handling status request")
        template = self.load_template('status.html')
        return web.Response(text=template, content_type='text/html')

    def load_template(self, template_name: str, **kwargs) -> str:
        """
        Loads a template file and replaces placeholders with values from kwargs.

        Args:
            template_name (str): The name of the template file to load.
            **kwargs: Key-value pairs to replace placeholders in the template.

        Returns:
            str: The rendered template with placeholders replaced.
        """
        self.logger.info(f"Loading template: {template_name}")
        with open(f'pixieveil/dashboard/templates/{template_name}', 'r') as file:
            template = file.read()
        for key, value in kwargs.items():
            template = template.replace(f'{{{{{key}}}}}', str(value))
        return template
