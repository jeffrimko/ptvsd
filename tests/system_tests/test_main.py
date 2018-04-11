import os
import unittest

import ptvsd
from tests.helpers.debugclient import EasyDebugClient as DebugClient
from tests.helpers.threading import get_locked_and_waiter
from tests.helpers.vsc import parse_message, VSCMessages
from tests.helpers.workspace import Workspace, PathEntry


def lifecycle_handshake(session, command='launch', options=None):
    with session.wait_for_event('initialized'):
        req_initialize = session.send_request(
            'initialize',
            adapterID='spam',
        )
        req_command = session.send_request(command, **options or {})
    # TODO: pre-set breakpoints
    req_done = session.send_request('configurationDone')
    return req_initialize, req_command, req_done


class TestsBase(object):

    @property
    def workspace(self):
        try:
            return self._workspace
        except AttributeError:
            self._workspace = Workspace()
            self.addCleanup(self._workspace.cleanup)
            return self._workspace

    @property
    def pathentry(self):
        try:
            return self._pathentry
        except AttributeError:
            self._pathentry = PathEntry()
            self.addCleanup(self._pathentry.cleanup)
            self._pathentry.install()
            return self._pathentry

    def write_script(self, name, content):
        return self.workspace.write_python_script(name, content=content)

    def write_debugger_script(self, filename, port, run_as):
        cwd = os.getcwd()
        kwargs = {
            'filename': filename,
            'port_num': port,
            'debug_id': None,
            'debug_options': None,
            'run_as': run_as,
        }
        return self.write_script('debugger.py', """
            import sys
            sys.path.insert(0, {!r})
            from ptvsd.debugger import debug
            debug(
                {filename!r},
                {port_num!r},
                {debug_id!r},
                {debug_options!r},
                {run_as!r},
            )
            """.format(cwd, **kwargs))


class CLITests(TestsBase, unittest.TestCase):

    def test_script_args(self):
        lockfile = self.workspace.lockfile()
        donescript, lockwait = lockfile.wait_for_script()
        filename = self.pathentry.write_module('spam', """
            import sys
            print(sys.argv)
            sys.stdout.flush()

            {}
            import time
            time.sleep(10000)
            """.format(donescript.replace('\n', '\n            ')))
        with DebugClient() as editor:
            adapter, session = editor.launch_script(
                filename,
                '--eggs',
            )
            lifecycle_handshake(session, 'launch')
            lockwait(timeout=2.0)
            session.send_request('disconnect')
        out = adapter.output

        self.assertEqual(out.decode('utf-8'),
                         "[{!r}, '--eggs']\n".format(filename))

    def test_run_to_completion(self):
        filename = self.pathentry.write_module('spam', """
            import sys
            print('done')
            sys.stdout.flush()
            """)
        with DebugClient() as editor:
            adapter, session = editor.launch_script(
                filename,
            )
            lifecycle_handshake(session, 'launch')
            adapter.wait()
        out = adapter.output.decode('utf-8')
        rc = adapter.exitcode

        self.assertIn('done', out.splitlines())
        self.assertEqual(rc, 0)

    def test_failure(self):
        filename = self.pathentry.write_module('spam', """
            import sys
            sys.exit(42)
            """)
        with DebugClient() as editor:
            adapter, session = editor.launch_script(
                filename,
            )
            lifecycle_handshake(session, 'launch')
            adapter.wait()
        rc = adapter.exitcode

        self.assertEqual(rc, 42)


class DebugTests(TestsBase, unittest.TestCase):

    def test_script(self):
        argv = []
        filename = self.write_script('spam.py', """
            import sys
            print('done')
            sys.stdout.flush()
            """)
        script = self.write_debugger_script(filename, 9876, run_as='script')
        with DebugClient(port=9876) as editor:
            adapter, session = editor.host_local_debugger(argv, script)
            lifecycle_handshake(session, 'launch')
            adapter.wait()
        out = adapter.output.decode('utf-8')
        rc = adapter.exitcode

        self.assertIn('done', out.splitlines())
        self.assertEqual(rc, 0)

    # python -m ptvsd --server --port 1234 --file one.py


class LifecycleTests(TestsBase, unittest.TestCase):

    @property
    def messages(self):
        try:
            return self._messages
        except AttributeError:
            self._messages = VSCMessages()
            return self._messages

    def new_response(self, *args, **kwargs):
        return self.messages.new_response(*args, **kwargs)

    def new_event(self, *args, **kwargs):
        return self.messages.new_event(*args, **kwargs)

    def assert_received(self, received, expected):
        received = [parse_message(msg) for msg in received]
        expected = [parse_message(msg) for msg in expected]
        self.assertEqual(received, expected)

    def test_pre_init(self):
        lock, wait = get_locked_and_waiter()

        def handle_msg(msg):
            if msg.type != 'event':
                return False
            if msg.event != 'output':
                return False
            lock.release()
            return True
        filename = self.pathentry.write_module('spam', '')
        with DebugClient() as editor:
            adapter, session = editor.launch_script(
                filename,
                handlers=[
                    (handle_msg, "event 'output'"),
                ],
            )
            wait(reason="event 'output'")
        out = adapter.output

        self.assert_received(session.received, [
            # TODO: Use self.new_event()...
            {
                'type': 'event',
                'seq': 0,
                'event': 'output',
                'body': {
                    'output': 'ptvsd',
                    'data': {
                        'version': ptvsd.__version__,
                    },
                    'category': 'telemetry',
                },
            },
        ])
        self.assertEqual(out, b'')

    def test_launch_ptvsd_client(self):
        argv = []
        lockfile = self.workspace.lockfile()
        done, waitscript = lockfile.wait_in_script()
        filename = self.write_script('spam.py', waitscript)
        script = self.write_debugger_script(filename, 9876, run_as='script')
        with DebugClient(port=9876) as editor:
            adapter, session = editor.host_local_debugger(argv, script)
            (req_initialize, req_launch, req_config
             ) = lifecycle_handshake(session, 'launch')
            done()
            adapter.wait()

        self.assert_received(session.received, [
            self.new_event(
                'output',
                category='telemetry',
                output='ptvsd',
                data={'version': ptvsd.__version__}),
            self.new_response(req_initialize, **dict(
                supportsExceptionInfoRequest=True,
                supportsConfigurationDoneRequest=True,
                supportsConditionalBreakpoints=True,
                supportsSetVariable=True,
                supportsValueFormattingOptions=True,
                supportsExceptionOptions=True,
                exceptionBreakpointFilters=[
                    {
                        'filter': 'raised',
                        'label': 'Raised Exceptions',
                        'default': False
                    },
                    {
                        'filter': 'uncaught',
                        'label': 'Uncaught Exceptions',
                        'default': True
                    },
                ],
                supportsEvaluateForHovers=True,
                supportsSetExpression=True,
                supportsModulesRequest=True,
            )),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(req_config),
            self.new_event('exited', exitCode=0),
            self.new_event('terminated'),
        ])

    def test_launch_ptvsd_server(self):
        lockfile = self.workspace.lockfile()
        done, waitscript = lockfile.wait_in_script()
        filename = self.write_script('spam.py', waitscript)
        with DebugClient() as editor:
            adapter, session = editor.launch_script(
                filename,
            )
            (req_initialize, req_launch, req_config
             ) = lifecycle_handshake(session, 'launch')
            done()
            adapter.wait()

        self.assert_received(session.received, [
            self.new_event(
                'output',
                category='telemetry',
                output='ptvsd',
                data={'version': ptvsd.__version__}),
            self.new_response(req_initialize, **dict(
                supportsExceptionInfoRequest=True,
                supportsConfigurationDoneRequest=True,
                supportsConditionalBreakpoints=True,
                supportsSetVariable=True,
                supportsValueFormattingOptions=True,
                supportsExceptionOptions=True,
                exceptionBreakpointFilters=[
                    {
                        'filter': 'raised',
                        'label': 'Raised Exceptions',
                        'default': False
                    },
                    {
                        'filter': 'uncaught',
                        'label': 'Uncaught Exceptions',
                        'default': True
                    },
                ],
                supportsEvaluateForHovers=True,
                supportsSetExpression=True,
                supportsModulesRequest=True,
            )),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(req_config),
            self.new_event('exited', exitCode=0),
            self.new_event('terminated'),
        ])

    @unittest.skip('re-attach needs fixing')
    def test_attach(self):
        lockfile = self.workspace.lockfile()
        done, waitscript = lockfile.wait_in_script()
        filename = self.write_script('spam.py', waitscript)
        with DebugClient() as editor:
            # Launch and detach.
            # TODO: This is not an ideal way to spin up a process
            # to which we can attach.  However, ptvsd has no such
            # capabilitity at present and attaching without ptvsd
            # running isn't an option currently.
            adapter, session = editor.launch_script(
                filename,
            )
            lifecycle_handshake(session, 'launch')
            editor.detach()

            # Re-attach.
            session = editor.attach()
            (req_initialize, req_launch, req_config
             ) = lifecycle_handshake(session, 'attach')

            done()
            adapter.wait()

        self.assert_received(session.received, [
            self.new_event(
                'output',
                category='telemetry',
                output='ptvsd',
                data={'version': ptvsd.__version__}),
            self.new_response(req_initialize, **dict(
                supportsExceptionInfoRequest=True,
                supportsConfigurationDoneRequest=True,
                supportsConditionalBreakpoints=True,
                supportsSetVariable=True,
                supportsValueFormattingOptions=True,
                supportsExceptionOptions=True,
                exceptionBreakpointFilters=[
                    {
                        'filter': 'raised',
                        'label': 'Raised Exceptions',
                        'default': False
                    },
                    {
                        'filter': 'uncaught',
                        'label': 'Uncaught Exceptions',
                        'default': True
                    },
                ],
                supportsEvaluateForHovers=True,
                supportsSetExpression=True,
                supportsModulesRequest=True,
            )),
            self.new_event('initialized'),
            self.new_response(req_launch),
            self.new_response(req_config),
            self.new_event('exited', exitCode=0),
            self.new_event('terminated'),
        ])
