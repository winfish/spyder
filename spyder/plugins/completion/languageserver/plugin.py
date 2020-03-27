# -*- coding: utf-8 -*-
#
# Copyright © Spyder Project Contributors
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)

"""
Manager for all LSP clients connected to the servers defined
in our Preferences.
"""

# Standard library imports
import functools
import logging
import os
import os.path as osp
import sys

# Third-party imports
from qtpy.QtCore import Slot, QPoint, QTimer
from qtpy.QtWidgets import QMenu, QMessageBox, QCheckBox

# Local imports
from spyder.config.base import _, get_conf_path, running_under_pytest
from spyder.config.lsp import PYTHON_CONFIG
from spyder.config.manager import CONF
from spyder.api.completion import SpyderCompletionPlugin
from spyder.utils.misc import check_connection_port, getcwd_or_home
from spyder.plugins.completion.languageserver import LSP_LANGUAGES
from spyder.plugins.completion.languageserver.client import LSPClient
from spyder.plugins.completion.languageserver.confpage import (
    LanguageServerConfigPage)
from spyder.plugins.completion.languageserver.widgets.status import (
    LSPStatusWidget)
from spyder.utils.qthelpers import add_actions, create_action


logger = logging.getLogger(__name__)


class LanguageServerPlugin(SpyderCompletionPlugin):
    """Language Server Protocol manager."""
    CONF_SECTION = 'lsp-server'
    CONF_FILE = False

    COMPLETION_CLIENT_NAME = 'lsp'
    STOPPED = 'stopped'
    RUNNING = 'running'
    LOCALHOST = ['127.0.0.1', 'localhost']
    CONFIGWIDGET_CLASS = LanguageServerConfigPage
    MAX_RESTART_ATTEMPS = 5
    TIME_BETWEEN_RESTARTS = 2000  # ms

    def __init__(self, parent):
        SpyderCompletionPlugin.__init__(self, parent)

        self.clients = {}
        self.clients_restart_count = {}
        self.clients_restart_timers = {}
        self.clients_restarting = {}
        self.requests = set({})
        self.register_queue = {}
        self.update_configuration()

        # Status bar widget
        statusbar = parent.statusBar()
        self.status_widget = LSPStatusWidget(
            self.main, statusbar, plugin=self)
        self.status_widget.set_value('stopped')

        # Signals
        self.status_widget.sig_clicked.connect(self.show_menu)

    # --- Status bar widget handling
    def show_menu(self):
        """Display a PyLS status bar widget menu, if pyls is down."""
        client = self.clients['python']

        if (client['status'] != self.RUNNING
                or client['instance'].lsp_server.poll() is not None):
            menu = self.status_widget.menu
            menu.clear()
            restart_action = create_action(
                self,
                text=_("Restart python language server"),
                triggered=lambda: self.restart_ls('python', force=True),
            )
            add_actions(menu, [restart_action])
            rect = self.status_widget.contentsRect()
            pos = self.status_widget.mapToGlobal(
                rect.topLeft() + QPoint(0, -rect.height()))
            self.status_widget.menu.popup(pos)

    def restart_ls(self, language, force=False):
        """Restart language server on failure."""
        client_config = {
            'status': self.STOPPED,
            'config': self.get_language_config(language),
            'instance': None,
        }

        if force:
            logger.info("Manual restart for {}...".format(language))
            self.set_status('restarting...')
            self.restart_client(language, client_config)

        elif self.clients_restarting[language]:
            attemp = (self.MAX_RESTART_ATTEMPS
                      - self.clients_restart_count[language] + 1)
            logger.info("Automatic restart attemp {} for {}...".format(
                attemp, language))
            self.set_status('restarting...')

            self.clients_restart_count[language] -= 1
            self.restart_client(language, client_config)
            client = self.clients[language]

            # Restarted the maximum amount of times without
            if self.clients_restart_count[language] <= 0:
                logger.info("Restart failed!")
                self.clients_restarting[language] = False
                self.clients_restart_timers[language].stop()
                self.clients_restart_timers[language] = None
                self.report_lsp_down(language)

            # Restarted succesfully
            if (client['status'] == self.RUNNING
                    and client['instance'].lsp_server.poll() is None):
                logger.info("Restart successful!")
                self.clients_restarting[language] = False
                self.clients_restart_timers[language].stop()
                self.clients_restart_timers[language] = None
                self.clients_restart_count[language] = 0
                self.set_status('ready')

    def set_status(self, status):
        """
        Show status for the current file.
        """
        self.status_widget.set_value('PyLS: {}'.format(status))

    def on_initialize(self, options, language):
        """
        Update the status bar widget on client initilization.
        """
        if self.clients_restarting.get(language, True):
            self.set_status('ready')

    def update_status(self):
        """
        Check language of current editor file to hide/show status widget.
        """
        if self.main:
            if self.main.editor:
                filename = self.main.editor.get_current_filename()
                self.status_widget.setVisible(filename.endswith('.py'))

    def handle_lsp_down(self, language):
        """
        Handle automatic restart of client/server on failure.
        """
        if not self.clients_restarting.get(language, False):
            logger.info("Automatic restart for {}...".format(language))

            timer = QTimer(self)
            timer.setSingleShot(False)
            timer.setInterval(self.TIME_BETWEEN_RESTARTS)
            timer.timeout.connect(lambda: self.restart_ls(language))

            self.set_status('restarting...')
            self.clients_restarting[language] = True
            self.clients_restart_count[language] = self.MAX_RESTART_ATTEMPS
            self.clients_restart_timers[language] = timer
            timer.start()

    # --- Other methods
    def register_file(self, language, filename, codeeditor):
        if language in self.clients:
            language_client = self.clients[language]['instance']
            if language_client is None:
                self.register_queue[language].append((filename, codeeditor))
            else:
                language_client.register_file(filename, codeeditor)

    def get_languages(self):
        """
        Get the list of languages we need to start servers and create
        clients for.
        """
        languages = ['python']
        all_options = CONF.options(self.CONF_SECTION)
        for option in all_options:
            if option in [l.lower() for l in LSP_LANGUAGES]:
                languages.append(option)
        return languages

    def get_language_config(self, language):
        """Get language configuration options from our config system."""
        if language == 'python':
            return self.generate_python_config()
        else:
            return self.get_option(language)

    def get_root_path(self, language):
        """
        Get root path to pass to the LSP servers.

        This can be the current project path or the output of
        getcwd_or_home (except for Python, see below).
        """
        path = None

        # Get path of the current project
        if self.main and self.main.projects:
            path = self.main.projects.get_active_project_path()

        if not path:
            # We can't use getcwd_or_home for LSP servers because if it
            # returns home and you have a lot of files on it
            # then computing completions takes a long time
            # and blocks the LSP server.
            # Instead we use an empty directory inside our config one,
            # just like we did for Rope in Spyder 3.
            path = get_conf_path('lsp_root_path')
            if not osp.exists(path):
                os.mkdir(path)

        return path

    @Slot()
    def project_path_update(self, project_path, update_kind):
        """
        Send a new initialize message to each LSP server when the project
        path has changed so they can update the respective server root paths.
        """
        self.main.projects.stop_lsp_services()
        for language in self.clients:
            language_client = self.clients[language]
            if language_client['status'] == self.RUNNING:
                self.main.editor.stop_completion_services(language)
                folder = self.get_root_path(language)
                instance = language_client['instance']
                instance.folder = folder
                instance.initialize()

    @Slot(str)
    def report_server_error(self, error):
        """Report server errors in our error report dialog."""
        self.main.console.exception_occurred(error, is_traceback=True,
                                             is_pyls_error=True)

    def report_no_external_server(self, host, port, language):
        """
        Report that connection couldn't be established with
        an external server.
        """
        if self.main.hide_report_lsp_no_external_server:
            return

        # Only show this section on windows
        win_message = (
            "<br><br>"
            "To fix this, please verify that your firewall or antivirus "
            "allows Python processes to open ports in your system, or the "
            "settings you introduced in our Preferences to connect to "
            "external LSP servers."
        ) if os.name == 'nt' else ''

        dismiss_box = QCheckBox(
            _("Hide this message during the current session")
        )
        msgbox = QMessageBox(self.main)
        msgbox.setIcon(QMessageBox.Warning)
        msgbox.setWindowTitle(_("Warning"))
        msgbox.setText(
            _("It appears there is no {language} language server listening "
              "at address:"
              "<br><br>"
              "<tt>{host}:{port}</tt>"
              "<br><br>"
              "Therefore, completion and linting for {language} will not "
              "work during this session." + win_message
              ).format(host=host, port=port, language=language.capitalize())
        )
        yes_button = msgbox.addButton(QMessageBox.Yes)
        msgbox.addButton(QMessageBox.No)
        msgbox.setCheckBox(dismiss_box)
        msgbox.exec_()

        self.main.hide_report_lsp_no_external_server = dismiss_box.isChecked()

        if msgbox.clickedButton() == yes_button:
            self.main.restart()

    @Slot(str)
    def report_lsp_down(self, language):
        """
        Report that either the transport layer or the LSP server are
        down.
        """
        self.set_status('down')

        if self.main.hide_report_lsp_down_message:
            return

        # Only show this section on windows
        win_message = (
            "To fix this, please verify that your firewall or antivirus "
            "allows Python processes to open ports in your system, or "
            "restart Spyder.<br><br>"
        ) if os.name == 'nt' else ''

        dismiss_box = QCheckBox(
            _("Hide this message during the current session")
        )
        msgbox = QMessageBox(self.main)
        msgbox.setIcon(QMessageBox.Warning)
        msgbox.setWindowTitle(_("Warning"))
        msgbox.setText(
            _("Completion and linting in the editor for {language} files "
              "will not work during the current session, or stopped working."
              "<br><br>"
              + win_message +
              "Do you want to restart Spyder?").format(
                  language=language.capitalize())
        )
        yes_button = msgbox.addButton(QMessageBox.Yes)
        msgbox.addButton(QMessageBox.No)
        msgbox.setCheckBox(dismiss_box)
        msgbox.exec_()

        self.main.hide_report_lsp_down_message = dismiss_box.isChecked()

        if msgbox.clickedButton() == yes_button:
            self.main.restart()

    def start_client(self, language):
        """Start an LSP client for a given language."""
        started = False
        if language in self.clients:
            language_client = self.clients[language]

            queue = self.register_queue[language]

            # Don't start LSP services when testing unless we demand
            # them.
            if running_under_pytest():
                if not os.environ.get('SPY_TEST_USE_INTROSPECTION'):
                    return started

            # Start client
            started = language_client['status'] == self.RUNNING
            if language_client['status'] == self.STOPPED:
                config = language_client['config']

                # If we're trying to connect to an external server,
                # verify that it's listening before creating a
                # client for it.
                if config['external']:
                    host = config['host']
                    port = config['port']
                    response = check_connection_port(host, port)
                    if not response:
                        self.report_no_external_server(host, port, language)
                        return False

                language_client['instance'] = LSPClient(
                    parent=self,
                    server_settings=config,
                    folder=self.get_root_path(language),
                    language=language
                )

                self.register_client_instance(language_client['instance'])

                logger.info("Starting LSP client for {}...".format(language))
                language_client['instance'].start()
                language_client['status'] = self.RUNNING
                for entry in queue:
                    language_client['instance'].register_file(*entry)
                self.register_queue[language] = []
        return started

    def register_client_instance(self, instance):
        """Register signals emmited by a client instance."""
        if self.main:
            self.main.sig_pythonpath_changed.connect(
                self.update_configuration)
            self.main.sig_main_interpreter_changed.connect(
                self.update_configuration)
            instance.sig_lsp_down.connect(self.handle_lsp_down)
            instance.sig_initialize.connect(self.on_initialize)

            if self.main.editor:
                instance.sig_initialize.connect(
                    self.main.editor.register_completion_server_settings)
                self.main.editor.sig_editor_focus_changed.connect(
                    self.update_status)
            if self.main.console:
                instance.sig_server_error.connect(self.report_server_error)
            if self.main.projects:
                instance.sig_initialize.connect(
                    self.main.projects.register_lsp_server_settings)

    def shutdown(self):
        logger.info("Shutting down LSP manager...")
        for language in self.clients:
            self.close_client(language)

    def update_configuration(self):
        for language in self.get_languages():
            client_config = {'status': self.STOPPED,
                             'config': self.get_language_config(language),
                             'instance': None}
            if language not in self.clients:
                self.clients[language] = client_config
                self.register_queue[language] = []
            else:
                current_lang_config = self.clients[language]['config']
                new_lang_config = client_config['config']
                restart_diff = ['cmd', 'args', 'host',
                                'port', 'external', 'stdio']
                restart = any([current_lang_config[x] != new_lang_config[x]
                               for x in restart_diff])
                if restart:
                    logger.debug("Restart required for {} client!".format(
                        language))
                    if self.clients[language]['status'] == self.STOPPED:
                        # If we move from an external non-working server to
                        # an internal one, we need to start a new client.
                        if (current_lang_config['external'] and
                                not new_lang_config['external']):
                            self.restart_client(language, client_config)
                        else:
                            self.clients[language] = client_config
                    elif self.clients[language]['status'] == self.RUNNING:
                        self.restart_client(language, client_config)
                else:
                    if self.clients[language]['status'] == self.RUNNING:
                        client = self.clients[language]['instance']
                        client.send_plugin_configurations(
                            new_lang_config['configurations'])

    def restart_client(self, language, config):
        """Restart a client."""
        self.main.editor.stop_completion_services(language)
        self.main.projects.stop_lsp_services()
        self.close_client(language)
        self.clients[language] = config
        self.start_client(language)

    def update_client_status(self, active_set):
        for language in self.clients:
            if language not in active_set:
                self.close_client(language)

    def close_client(self, language):
        if language in self.clients:
            language_client = self.clients[language]
            if language_client['status'] == self.RUNNING:
                logger.info("Stopping LSP client for {}...".format(language))
                # language_client['instance'].shutdown()
                # language_client['instance'].exit()
                language_client['instance'].stop()
            language_client['status'] = self.STOPPED

    def receive_response(self, response_type, response, language, req_id):
        if req_id in self.requests:
            self.requests.discard(req_id)
            self.sig_response_ready.emit(
                self.COMPLETION_CLIENT_NAME, req_id, response)

    def send_request(self, language, request, params, req_id):
        if language in self.clients:
            language_client = self.clients[language]
            if language_client['status'] == self.RUNNING:
                self.requests.add(req_id)
                client = self.clients[language]['instance']
                params['response_callback'] = functools.partial(
                    self.receive_response, language=language, req_id=req_id)
                client.perform_request(request, params)
                return
        self.sig_response_ready.emit(self.COMPLETION_CLIENT_NAME,
                                     req_id, {})

    def send_notification(self, language, request, params):
        if language in self.clients:
            language_client = self.clients[language]
            if language_client['status'] == self.RUNNING:
                client = self.clients[language]['instance']
                client.perform_request(request, params)

    def broadcast_notification(self, request, params):
        """Send notification/request to all available LSP servers."""
        language = params.pop('language', None)
        if language:
            self.send_notification(language, request, params)
        else:
            for language in self.clients:
                self.send_notification(language, request, params)

    def generate_python_config(self):
        """
        Update Python server configuration with the options saved in our
        config system.
        """
        python_config = PYTHON_CONFIG.copy()

        # Server options
        cmd = self.get_option('advanced/module')
        host = self.get_option('advanced/host')
        port = self.get_option('advanced/port')

        # Pycodestyle
        cs_exclude = self.get_option('pycodestyle/exclude').split(',')
        cs_filename = self.get_option('pycodestyle/filename').split(',')
        cs_select = self.get_option('pycodestyle/select').split(',')
        cs_ignore = self.get_option('pycodestyle/ignore').split(',')
        cs_max_line_length = self.get_option('pycodestyle/max_line_length')

        pycodestyle = {
            'enabled': self.get_option('pycodestyle'),
            'exclude': [exclude.strip() for exclude in cs_exclude if exclude],
            'filename': [filename.strip()
                         for filename in cs_filename if filename],
            'select': [select.strip() for select in cs_select if select],
            'ignore': [ignore.strip() for ignore in cs_ignore if ignore],
            'hangClosing': False,
            'maxLineLength': cs_max_line_length
        }

        # Linting - Pyflakes
        pyflakes = {
            'enabled': self.get_option('pyflakes')
        }

        # Pydocstyle
        convention = self.get_option('pydocstyle/convention')

        if convention == 'Custom':
            ds_ignore = self.get_option('pydocstyle/ignore').split(',')
            ds_select = self.get_option('pydocstyle/select').split(',')
            ds_add_ignore = []
            ds_add_select = []
        else:
            ds_ignore = []
            ds_select = []
            ds_add_ignore = self.get_option('pydocstyle/ignore').split(',')
            ds_add_select = self.get_option('pydocstyle/select').split(',')

        pydocstyle = {
            'enabled': self.get_option('pydocstyle'),
            'convention': convention,
            'addIgnore': [ignore.strip()
                          for ignore in ds_add_ignore if ignore],
            'addSelect': [select.strip()
                          for select in ds_add_select if select],
            'ignore': [ignore.strip() for ignore in ds_ignore if ignore],
            'select': [select.strip() for select in ds_select if select],
            'match': self.get_option('pydocstyle/match'),
            'matchDir': self.get_option('pydocstyle/match_dir')
        }

        # Jedi configuration
        if self.get_option('default', section='main_interpreter'):
            environment = None
        else:
            environment = self.get_option('custom_interpreter',
                                          section='main_interpreter')
        jedi = {
            'environment': environment,
            'extra_paths': self.get_option('spyder_pythonpath',
                                           section='main', default=[]),
        }
        jedi_completion = {
            'enabled': self.get_option('code_completion'),
            'include_params':  self.get_option('code_snippets')
        }
        jedi_signature_help = {
            'enabled': self.get_option('jedi_signature_help')
        }
        jedi_definition = {
            'enabled': self.get_option('jedi_definition'),
            'follow_imports': self.get_option('jedi_definition/follow_imports')
        }

        # Advanced
        external_server = self.get_option('advanced/external')
        stdio = self.get_option('advanced/stdio')

        # Setup options in json
        python_config['cmd'] = cmd
        if host in self.LOCALHOST and not stdio:
            python_config['args'] = ('--host {host} --port {port} --tcp '
                                     '--check-parent-process')
        else:
            python_config['args'] = '--check-parent-process'
        python_config['external'] = external_server
        python_config['stdio'] = stdio
        python_config['host'] = host
        python_config['port'] = port

        plugins = python_config['configurations']['pyls']['plugins']
        plugins['pycodestyle'] = pycodestyle
        plugins['pyflakes'] = pyflakes
        plugins['pydocstyle'] = pydocstyle
        plugins['jedi'] = jedi
        plugins['jedi_completion'] = jedi_completion
        plugins['jedi_signature_help'] = jedi_signature_help
        plugins['jedi_definition'] = jedi_definition
        plugins['preload']['modules'] = self.get_option('preload_modules')

        return python_config
