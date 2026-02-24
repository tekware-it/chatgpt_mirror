"""Qt WebEngine bridge classes used by the main window.

Separated from the UI logic so WebView integration can evolve independently
(e.g. console fallback parsing, createWindow tab handling, bridge signals).
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile


class WebBridge(QObject):
    """QWebChannel-exposed object used by injected JavaScript to send deltas/events."""
    deltaReceived = Signal(str)
    eventReceived = Signal(str)

    @Slot(str)
    def sendDelta(self, json_string: str) -> None:
        self.deltaReceived.emit(json_string)

    @Slot(str)
    def sendEvent(self, json_string: str) -> None:
        self.eventReceived.emit(json_string)


class MirrorWebPage(QWebEnginePage):
    """Custom page that routes console bridge messages and supports in-app tab creation."""
    consoleDeltaReceived = Signal(str)
    consoleEventReceived = Signal(str)
    CONSOLE_DELTA_PREFIX = "__CGM_DELTA__"
    CONSOLE_EVENT_PREFIX = "__CGM_EVT__"

    def __init__(self, profile: QWebEngineProfile, parent: Optional[QObject] = None, new_page_factory=None) -> None:
        super().__init__(profile, parent)
        self._new_page_factory = new_page_factory

    def createWindow(self, _type):  # type: ignore[override]
        if callable(self._new_page_factory):
            try:
                page = self._new_page_factory()
                if page is not None:
                    return page
            except Exception:
                pass
        return super().createWindow(_type)

    def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:  # type: ignore[override]
        if isinstance(message, str) and message.startswith(self.CONSOLE_DELTA_PREFIX):
            self.consoleDeltaReceived.emit(message[len(self.CONSOLE_DELTA_PREFIX) :])
            return
        if isinstance(message, str) and message.startswith(self.CONSOLE_EVENT_PREFIX):
            self.consoleEventReceived.emit(message[len(self.CONSOLE_EVENT_PREFIX) :])
            return
        super().javaScriptConsoleMessage(level, message, line_number, source_id)
