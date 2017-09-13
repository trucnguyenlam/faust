"""Worker

A "worker" starts a single instance of a Faust application.

The Worker is the terminal interface to App, and is the third
entry point in this list:

1) :program:`faust worker`
2) -> :class:`faust.bin.worker.worker`
3) -> :class:`faust.Worker`
4) -> :class:`faust.App`

You can call ``await app.start()`` directly to get a side-effect free
instance that can be embedded in any environment. It won't even emit logs
to the console unless you have configured :mod:`logging` manually.

The worker only concerns itself with the terminal, process
signal handlers, logging, debugging mechanisms, etc.

.. admonition:: Web server

    The Worker also starts the web server, the app will not start it.

.. admonition:: Multiple apps

    If you want your worker to start multiple apps, you would have
    to pass them in with the ``*services`` starargs::

        worker = Worker(app1, app2, app3, app4)

    This way the extra apps will be started together with the main app,
    and the main app of the worker (``worker.app``) will end up being
    the first positional argument (``app1``).

    Note that the web server will only concern itself with the
    main app, so if you want web access to the other apps you have to
    include web servers for them (also passed in as ``*services`` starargs).
"""
import asyncio
import logging
import os
import platform
import socket
import sys

from itertools import chain
from pathlib import Path
from typing import Any, IO, Iterable, Set, Type, Union

from progress.spinner import Spinner

from . import __version__ as faust_version
from .bin._env import DEBUG, DEFAULT_BLOCKING_TIMEOUT
from .types import AppT, SensorT
from .utils.imports import SymbolArg, symbol_by_name
from .utils.logging import get_logger, level_name
from .utils.objects import cached_property
from .utils.services import ServiceT, ServiceWorker
from .web.site import Website as _Website

try:
    # if installed we use this to set ps.name (argv[0])
    from setproctitle import setproctitle
except ImportError:
    def setproctitle(title: str) -> None: ...  # noqa

__all__ = ['Worker']

#: Path to default Web site class.
DEFAULT_WEBSITE_CLS = 'faust.web.site:Website'

#: Name prefix of process in ps/top listings.
PSIDENT = '[Faust:Worker]'

#: ASCII-art used in startup banner.
ARTLINES = """\
                                       .x+=:.        s
   oec :                               z`    ^%      :8
  @88888                 x.    .          .   <k    .88
  8"*88%        u      .@88k  z88u      .@8Ned8"   :888ooo
  8b.        us888u.  ~"8888 ^8888    .@^%8888"  -*8888888
 u888888> .@88 "8888"   8888  888R   x88:  `)8b.   8888
  8888R   9888  9888    8888  888R   8888N=*8888   8888
  8888P   9888  9888    8888  888R    %8"    R88   8888
  *888>   9888  9888    8888 ,888B .   @8Wou 9%   .8888Lu=
  4888    9888  9888   "8888Y 8888"  .888888P`    ^%888*
  '888    "888*""888"   `Y"   'YP    `   ^"F        'Y"
   88R     ^Y"   ^Y'
   88>
   48
   '8
"""

#: Format string for startup banner.
F_BANNER = """
{art}
{ident}
[ .id          -> {app.id}
  .web         -> {website.url}
  .log         -> {logfile} ({loglevel})
  .pid         -> {pid}
  .hostname    -> {hostname}
  .loop        -> {loop}
  .transport   -> {app.url} {transport_extra}
  .store       -> {app.store} ]
""".strip()

#: Format string for banner info line.
F_IDENT = """
ƒaµS† v{faust_v} {system} ({transport_v} {http_v} {py}={py_v})
""".strip()

logger = get_logger(__name__)


class SpinnerHandler(logging.Handler):
    """A logger handler that iterates our progress spinner for each log."""

    # For every logging call we advance the terminal spinner (-\/-)

    def __init__(self, worker: 'Worker', **kwargs: Any) -> None:
        self.worker = worker
        super().__init__(**kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        # the spinner is only in effect with WARN level and below.
        if self.worker.spinner:
            self.worker.spinner.next()  # noqa: B305


class Worker(ServiceWorker):
    """Worker.

    Usage:
        You can start a worker using:

            1) the :program:`faust worker` program.

            2) instantiating Worker programmatically and calling
               `execute_from_commandline()`::

                    >>> worker = Worker(app)
                    >>> worker.execute_from_commandline()

            3) or if you already have an event loop, calling ``await start``,
               but in that case *you are responsible for gracefully shutting
               down the event loop*::

                    async def start_worker(app: AppT):
                        worker = Worker(app)
                        await worker.start()

    Arguments:
        app (AppT): The Faust app to start.
        *services (ServiceT): Services to start with worker.
            This includes application instances to start.

    Keyword Arguments:
        sensors (Iterable[SensorT]): List of sensors to include.
        debug (bool): Enables debugging mode [disabled by default].
        quiet (bool): Do not output anything to console [disabled by default].
        loglevel (Union[str, int]): Level to use for logging, can be string
            (one of: CRIT|ERROR|WARN|INFO|DEBUG), or integer.
        logfile (Union[str, IO]): Name of file or a stream to log to.
        logformat (str): Format to use when logging messages.
        stdout (IO): Standard out stream.
        stderr (IO): Standard err stream.
        blocking_timeout (float): When :attr:`debug` is enabled this
            sets the timeout for detecting that the event loop is blocked.
        workdir (Union[str, Path]): Custom working directory for the process
            that the worker will change into when started.
            This working directory change is permanent for the process,
            or until something else changes the working directory again.
        Website: Class used to serve the Faust web site
            (defaults to :class:`faust.web.site.Website`).
        web_port (int): Port for web site to bind to (defaults to 6066).
        web_bind (str): Host to bind web site to (defaults to "0.0.0.0").
        advertised_host (str): FIXME
        with_uvloop: This argument is unused by Worker, but still needs
            to be supported as a keyword argument because of
            :func:`faust.bin.worker.parse_worker_args`.
            It's too late to switch event loop implementations at this point,
            so the :option:`--with-uvloop` option is handled by
            the :class:`faust.bin.worker.worker` command.
        loop (asyncio.AbstractEventLoop): Custom event loop object.
    """
    logger = logger

    #: Format string for the banner itself.
    f_banner = F_BANNER

    #: ASCII-art included in the banner.
    art = ARTLINES

    #: Format string for the ident line in the banner (with
    #: the Faust version).
    f_ident = F_IDENT

    #: The Faust app started by this worker.
    app: AppT

    #: Additional sensors to add to the Faust app.
    sensors: Set[SensorT]

    #: Current working directory.
    #: Note that if passed as an argument to Worker, the worker
    #: will change to this directory when started.
    workdir: Path

    #: Port to run the embedded web server on (defaults to 6066).
    web_port: int

    #: Host to bind web server port to (defaults to '0.0.0.0').
    web_bind: str

    #: Class that starts our web server and serves the Faust website.
    Website: Type[_Website]

    #: Class that displays a terminal progress spinner (see :pypi:`progress`).
    spinner: Spinner

    def __init__(
            self, app: AppT, *services: ServiceT,
            sensors: Iterable[SensorT] = None,
            debug: bool = DEBUG,
            quiet: bool = False,
            loglevel: Union[str, int] = None,
            logfile: Union[str, IO] = None,
            logformat: str = None,
            stdout: IO = sys.stdout,
            stderr: IO = sys.stderr,
            blocking_timeout: float = DEFAULT_BLOCKING_TIMEOUT,
            workdir: Union[Path, str] = None,
            Website: SymbolArg[Type[_Website]] = DEFAULT_WEBSITE_CLS,
            web_port: int = None,
            web_bind: str = None,
            with_uvloop: bool = False,  # <-- NOT USED BUT MUST SUPPORT
            advertised_host: str = None,
            loop: asyncio.AbstractEventLoop = None,
            **kwargs: Any) -> None:
        self.app = app
        self.sensors = set(sensors or [])
        self.workdir = Path(workdir or Path.cwd())
        self.Website = symbol_by_name(Website)
        self.web_port = web_port
        self.web_bind = web_bind
        super().__init__(
            *services,
            debug=debug,
            quiet=quiet,
            loglevel=loglevel,
            logfile=logfile,
            logformat=logformat,
            stdout=stdout,
            stderr=stderr,
            blocking_timeout=blocking_timeout,
            loop=loop,
            **kwargs)
        self.spinner = Spinner(file=self.stdout)

    async def on_startup_finished(self) -> None:
        # block detection started here after changelog stuff,
        # and blocking RocksDB bulk updates.
        await self.maybe_start_blockdetection()
        if self.spinner:
            self.spinner.finish()
            self.spinner = None
            self.say('ready- ^')
        else:
            self.log.info('Ready')

    def faust_ident(self) -> str:
        return self.f_ident.format(
            py=platform.python_implementation(),
            faust_v=faust_version,
            system=platform.system(),
            transport_v=self.app.transport.driver_version,
            http_v=self.website.web.driver_version,
            py_v=platform.python_version(),
        )

    def banner(self) -> str:
        """Generate the text banner emitted before the worker starts."""
        transport_extra = ''
        # uvloop didn't leave us with any way to identify itself,
        # and also there's no uvloop.__version__ attribute.
        if self.loop.__class__.__module__ == 'uvloop':
            transport_extra = '+uvloop'
        return self.f_banner.format(
            art=self.art,
            ident=self.faust_ident(),
            app=self.app,
            website=self.website.web,
            logfile=self.logfile if self.logfile else '-stderr-',
            loglevel=level_name(self.loglevel or 'WARN').lower(),
            pid=os.getpid(),
            hostname=socket.gethostname(),
            transport_extra=transport_extra,
            loop=asyncio.get_event_loop(),
        )

    def on_init_dependencies(self) -> Iterable[ServiceT]:
        # App service is now a child of worker.
        self.app.beacon.reattach(self.beacon)
        # Transfer sensors to app
        for sensor in self.sensors:
            self.app.sensors.add(sensor)
        # Callback called once the opp is running and fully
        # functional, we use it to e.g. print the "ready" message.
        self.app.on_startup_finished = self.on_startup_finished
        return chain([self.website], self.services, [self.app])

    async def on_first_start(self) -> None:
        if self.workdir and Path.cwd().absolute() != self.workdir.absolute():
            os.chdir(Path(self.workdir).absolute())
        await super().on_first_start()  # <-- sets up logging

    def _setproctitle(self, info: str, *, ident: str = PSIDENT) -> None:
        setproctitle(f'{ident} {info}')

    async def on_execute(self) -> None:
        # This is called as soon as we starts and prints the banner.
        self._setproctitle('init')
        self.say(self.banner())
        self._say('^ ', end='')

    def on_setup_root_logger(self,
                             logger: logging.Logger,
                             level: int) -> None:
        # This is used to set up the terminal progress spinner
        # so that it spins for every log message emitted.
        if level and level < logging.WARN:
            self.spinner = None

        if self.spinner:
            logger.handlers[0].setLevel(level)
            logger.addHandler(
                SpinnerHandler(self, level=logging.DEBUG))
            logger.setLevel(logging.DEBUG)

    @cached_property
    def website(self) -> _Website:
        return self.Website(
            self.app,
            bind=self.web_bind,
            port=self.web_port,
            loop=self.loop,
            beacon=self.beacon,
        )
