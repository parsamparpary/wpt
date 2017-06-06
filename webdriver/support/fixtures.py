import json
import os
import urlparse
import re

import webdriver

from support.asserts import assert_error
from support.http_request import HTTPRequest
from support import merge_dictionaries

default_host = "http://127.0.0.1"
default_port = "4444"

def _ensure_valid_window(session):
    """If current window is not open anymore, ensure to have a valid one selected."""
    try:
        session.window_handle
    except webdriver.NoSuchWindowException:
        session.window_handle = session.handles[0]


def _dismiss_user_prompts(session):
    """Dismisses any open user prompts in windows."""
    current_window = session.window_handle

    for window in _windows(session):
        session.window_handle = window
        try:
            session.alert.dismiss()
        except webdriver.NoSuchAlertException:
            pass

    session.window_handle = current_window


def _restore_windows(session):
    """Closes superfluous windows opened by the test without ending
    the session implicitly by closing the last window.
    """
    current_window = session.window_handle

    for window in _windows(session, exclude=[current_window]):
        session.window_handle = window
        if len(session.handles) > 1:
            session.close()

    session.window_handle = current_window


def _switch_to_top_level_browsing_context(session):
    """If the current browsing context selected by WebDriver is a
    `<frame>` or an `<iframe>`, switch it back to the top-level
    browsing context.
    """
    session.switch_frame(None)


def _windows(session, exclude=None):
    """Set of window handles, filtered by an `exclude` list if
    provided.
    """
    if exclude is None:
        exclude = []
    wins = [w for w in session.handles if w not in exclude]
    return set(wins)


def create_frame(session):
    """Create an `iframe` element in the current browsing context and insert it
    into the document. Return an element reference."""
    def create_frame():
        append = """
            var frame = document.createElement('iframe');
            document.body.appendChild(frame);
            return frame;
        """
        response = session.execute_script(append)

    return create_frame


def create_window(session):
    """Open new window and return the window handle."""
    def create_window():
        windows_before = session.handles
        name = session.execute_script("window.open()")
        assert len(session.handles) == len(windows_before) + 1
        new_windows = list(set(session.handles) - set(windows_before))
        return new_windows.pop()
    return create_window


def http(session):
    return HTTPRequest(session.transport.host, session.transport.port)


def server_config():
    return json.loads(os.environ.get("WD_SERVER_CONFIG"))


def configuration():
    host = os.environ.get("WD_HOST", default_host)
    port = int(os.environ.get("WD_PORT", default_port))
    capabilities = json.loads(os.environ.get("WD_CAPABILITIES", "{}"))

    return {
        "host": host,
        "port": port,
        "capabilities": capabilities
    }


_current_session = None


def session(configuration, request):
    """Create and start a session for a test that does not itself test session creation.

    By default the session will stay open after each test, but we always try to start a
    new one and assume that if that fails there is already a valid session. This makes it
    possible to recover from some errors that might leave the session in a bad state, but
    does not demand that we start a new session per test."""
    global _current_session
    if _current_session is None:
        _current_session = webdriver.Session(configuration["host"],
                                             configuration["port"],
                                             capabilities={"alwaysMatch": configuration["capabilities"]})
    try:
        _current_session.start()
    except webdriver.errors.SessionNotCreatedException:
        if not _current_session.session_id:
            raise

    # finalisers are popped off a stack,
    # making their ordering reverse
    request.addfinalizer(lambda: _switch_to_top_level_browsing_context(_current_session))
    request.addfinalizer(lambda: _restore_windows(_current_session))
    request.addfinalizer(lambda: _dismiss_user_prompts(_current_session))
    request.addfinalizer(lambda: _ensure_valid_window(_current_session))

    return _current_session


def new_session(configuration, request):
    """Return a factory function that will attempt to start a session with a given body.

    This is intended for tests that are themselves testing new session creation, and the
    session created is closed at the end of the test."""
    def end():
        global _current_session
        if _current_session is not None and _current_session.session_id:
            _current_session.end()
            _current_session = None

    def create_session(body):
        global _current_session
        _session = webdriver.Session(configuration["host"],
                                     configuration["port"],
                                     capabilities=None)
        # TODO: merge in some capabilities from the confguration capabilities
        # since these might be needed to start the browser
        value = _session.send_command("POST", "session", body=body)
        # Don't set the global session until we are sure this succeeded
        _current_session = _session
        _session.session_id = value["sessionId"]

        return value, _current_session

    end()
    request.addfinalizer(end)

    return create_session


def url(server_config):
    def inner(path, query="", fragment=""):
        rv = urlparse.urlunsplit(("http",
                                  "%s:%s" % (server_config["host"],
                                             server_config["ports"]["http"][0]),
                                  path,
                                  query,
                                  fragment))
        return rv
    return inner

def create_dialog(session):
    """Create a dialog (one of "alert", "prompt", or "confirm") and provide a
    function to validate that the dialog has been "handled" (either accepted or
    dismissed) by returning some value."""

    def create_dialog(dialog_type, text=None, result_var=None):
        assert dialog_type in ("alert", "confirm", "prompt"), (
               "Invalid dialog type: '%s'" % dialog_type)

        if text is None:
            text = ""

        assert isinstance(text, basestring), "`text` parameter must be a string"

        if result_var is None:
            result_var = "__WEBDRIVER"

        assert re.search(r"^[_$a-z$][_$a-z0-9]*$", result_var, re.IGNORECASE), (
            'The `result_var` must be a valid JavaScript identifier')

        # Script completion and modal summoning are scheduled on two separate
        # turns of the event loop to ensure that both occur regardless of how
        # the user agent manages script execution.
        spawn = """
            var done = arguments[0];
            setTimeout(done, 0);
            setTimeout(function() {{
                window.{0} = window.{1}("{2}");
            }}, 0);
        """.format(result_var, dialog_type, text)

        session.send_session_command("POST",
                                     "execute/async",
                                     {"script": spawn, "args": []})

    return create_dialog
