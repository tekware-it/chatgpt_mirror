#!/usr/bin/env python3
"""
MVP desktop ChatGPT mirror for Ubuntu 22.04 using PySide6 + QtWebEngine.

The app shows:
- Left: native message viewer (fast-ish list view with per-message widgets)
- Right: embedded chatgpt.com web view for login/SSO and debugging

Important constraints:
- No OpenAI API usage
- No private endpoint calls
- Reads only rendered DOM content via injected JavaScript
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import tempfile
import time
import uuid
from html import escape as html_escape
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QEvent,
    QEventLoop,
    QMarginsF,
    QModelIndex,
    QObject,
    QLocale,
    QPoint,
    QUrl,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPageLayout, QPageSize, QPixmap
from PySide6.QtGui import QAction, QActionGroup, QTextCharFormat, QTextDocument, QSyntaxHighlighter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QRadioButton,
    QPushButton,
    QPlainTextEdit,
    QMenu,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtPrintSupport import QPrinter
from mirror_models import Message, MessageListModel, MessagePart, preview_from_markdown
from mirror_storage import IMAGE_BYTES_CACHE, OfflineStore, ensure_data_root, ensure_profile_root
from mirror_web import MirrorWebPage, WebBridge

try:
    from pygments import highlight as pygments_highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name
    from pygments.lexers.special import TextLexer

    PYGMENTS_AVAILABLE = True
except Exception:
    PYGMENTS_AVAILABLE = False
    pygments_highlight = None  # type: ignore[assignment]
    HtmlFormatter = None  # type: ignore[assignment]
    get_lexer_by_name = None  # type: ignore[assignment]
    TextLexer = None  # type: ignore[assignment]

PDF_PYGMENTS_STYLE = "default"


from mirror_js import JS_INJECTOR

from mirror_widgets import (
    CodeBlockWidget,
    CodeFullTextWidget,
    CodePlainTextEdit,
    ImagePartWidget,
    MarkdownTextWidget,
    MessageListPane,
    MessageRowWidget,
    SimpleCodeHighlighter,
    markdown_to_html,
    monospace_font,
    normalize_code_lang,
)

APP_DISPLAY_NAME = "ChatGPT Mirror"
# Release automation can override this (for example from a Git tag in CI).
APP_VERSION = os.environ.get("CHATGPT_MIRROR_VERSION", "dev")
GITHUB_PROJECT_URL = "https://github.com/tekware-it/chatgpt_mirror"
GITHUB_SPONSORS_URL = "https://github.com/sponsors/tekware-it"

class MainWindow(QMainWindow):
    """One application tab: native mirror (left) + ChatGPT WebView (right).

    This class coordinates:
    - DOM delta ingestion from the injected JS extractor
    - native model/view updates
    - scroll synchronization between native list and WebView
    - exports and per-tab offline snapshot persistence
    """
    def __init__(
        self,
        tabs_host: Optional[QWidget] = None,
        shared_profile: Optional[QWebEngineProfile] = None,
        profile_root: Optional[Path] = None,
        offline_store: Optional[OfflineStore] = None,
        tab_id: Optional[str] = None,
        storage_db_file: Optional[str] = None,
        initial_snapshot: Optional[dict] = None,
        initial_url: Optional[str] = "https://chatgpt.com",
    ) -> None:
        super().__init__()
        self._tabs_host = tabs_host
        self._offline_store = offline_store
        self.tab_id = (tab_id or uuid.uuid4().hex[:12]).strip()
        self.storage_db_file = (storage_db_file or "").strip() or None
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1600, 900)

        self.model = MessageListModel(self)
        self.left_pane = MessageListPane(self.model)
        self.web_view = QWebEngineView()

        # Use a disk-backed WebEngine profile so chatgpt.com cookies/session survive app restarts.
        if shared_profile is None:
            profile_root = ensure_profile_root()
            self.web_profile = QWebEngineProfile("chatgpt_mirror_profile", self)
            self.web_profile.setPersistentStoragePath(str(profile_root / "qtwebengine"))
            self.web_profile.setCachePath(str(profile_root / "qtwebengine-cache"))
            self.web_profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
            self.web_profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        else:
            self.web_profile = shared_profile
            if profile_root is None:
                profile_root = ensure_profile_root()
        self.web_page = MirrorWebPage(self.web_profile, self.web_view, new_page_factory=self._create_new_tab_page)
        self.web_view.setPage(self.web_page)

        self.bridge = WebBridge()
        self.channel = QWebChannel(self.web_view.page())
        self.channel.registerObject("bridge", self.bridge)
        self.web_view.page().setWebChannel(self.channel)

        self.bridge.deltaReceived.connect(self.on_delta_received)
        self.bridge.eventReceived.connect(self.on_web_event_received)
        self.web_page.consoleDeltaReceived.connect(self.on_delta_received)
        self.web_page.consoleEventReceived.connect(self.on_web_event_received)
        self.web_view.loadFinished.connect(self.on_load_finished)

        self._native_scroll_sync_timer = QTimer(self)
        self._native_scroll_sync_timer.setSingleShot(True)
        self._native_scroll_sync_timer.timeout.connect(self._send_native_top_key_to_web)
        self._suppress_native_scroll_until = 0.0
        self._ignore_web_scroll_events_until = 0.0
        self._auto_scroll_enabled = True
        self._web_to_native_sync_enabled = True
        self._native_to_web_sync_enabled = True
        self._keep_dom_count = 30
        self._restore_pruned_on_view_enabled = False
        self._scroll_sync_debug_enabled = False
        self._native_img_use_firefox_headers = True
        ImagePartWidget.set_use_firefox_headers(self._native_img_use_firefox_headers)
        self._profile_root = profile_root
        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.timeout.connect(self._persist_offline_snapshot_now)
        self.left_pane.list_view.verticalScrollBar().valueChanged.connect(self._on_native_scroll_value_changed)
        self.left_pane.autoScrollChanged.connect(self._on_auto_scroll_changed)
        self.left_pane.webToNativeSyncChanged.connect(self._on_web_to_native_sync_changed)
        self.left_pane.nativeToWebSyncChanged.connect(self._on_native_to_web_sync_changed)
        self.left_pane.keepDomChanged.connect(self._on_keep_dom_changed)
        self.left_pane.restorePrunedOnViewChanged.connect(self._on_restore_pruned_on_view_changed)
        self.left_pane.scrollSyncDebugChanged.connect(self._on_scroll_sync_debug_changed)
        self.left_pane.nativeImageFirefoxHeadersChanged.connect(self._on_native_image_firefox_headers_changed)
        self.left_pane.browserLanguageChanged.connect(self._on_browser_language_changed)
        self.left_pane.resetSessionRequested.connect(self._on_reset_session_requested)
        self.left_pane.exportRequested.connect(self._on_export_requested)
        self.left_pane.exportDebugVisibleRequested.connect(self._on_export_debug_visible_requested)
        self.left_pane.exportPdfImagesDebugRequested.connect(self._on_export_pdf_images_debug_requested)
        self.left_pane.aboutRequested.connect(self._show_about_dialog)

        splitter = QSplitter(Qt.Horizontal)  # Horizontal splitter => left/right panes.
        splitter.addWidget(self.left_pane)
        splitter.addWidget(self.web_view)
        splitter.setSizes([700, 900])
        self.setCentralWidget(splitter)

        if initial_snapshot:
            self._restore_offline_snapshot(initial_snapshot)

        self._apply_browser_language_setting()
        self.web_view.urlChanged.connect(lambda _u: self._schedule_persist_offline_snapshot())
        self.web_view.titleChanged.connect(lambda _t: self._schedule_persist_offline_snapshot())
        if initial_url:
            self.web_view.setUrl(QUrl(initial_url))

    def _show_about_dialog(self) -> None:
        """Show the application About dialog with version and project links."""
        dlg = QDialog(self)
        dlg.setWindowTitle("About")
        dlg.setModal(True)
        dlg.resize(520, 260)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QLabel(f"<b>{APP_DISPLAY_NAME}</b>")
        title.setStyleSheet("QLabel { font-size: 16px; }")
        layout.addWidget(title)

        subtitle = QLabel(
            "Native ChatGPT DOM mirror with offline snapshots and export tools. "
            "It reduces lag on long conversations by pruning WebView DOM nodes and "
            "improves the reading/copying experience with a native viewer."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("QLabel { color: #374151; }")
        layout.addWidget(subtitle)

        details = QLabel(
            "<p style='margin:0'>"
            f"<b>Version:</b> <code>{html_escape(APP_VERSION)}</code><br>"
            "Release builds should set this from the Git tag."
            "</p>"
            "<p style='margin-top:8px'>"
            f"<a href='{html_escape(GITHUB_PROJECT_URL)}'>GitHub project page</a><br>"
            f"<a href='{html_escape(GITHUB_SPONSORS_URL)}'>GitHub Sponsors</a>"
            "</p>"
        )
        details.setTextFormat(Qt.RichText)
        details.setTextInteractionFlags(Qt.TextBrowserInteraction)
        details.setOpenExternalLinks(True)
        details.setWordWrap(True)
        layout.addWidget(details, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok, parent=dlg)
        buttons.accepted.connect(dlg.accept)
        layout.addWidget(buttons)

        dlg.exec()

    def _create_new_tab_page(self):
        host = self._tabs_host
        if host is None:
            return None
        creator = getattr(host, "create_mirror_tab", None)
        if not callable(creator):
            return None
        try:
            new_tab = creator(url=None, switch=True)
            if new_tab is None:
                return None
            return new_tab.web_view.page()
        except Exception:
            return None

    @Slot(bool)
    def on_load_finished(self, ok: bool) -> None:
        if not ok:
            return
        # Inject bootstrap + extractor after each page load/navigation.
        self.web_view.page().runJavaScript(JS_INJECTOR)
        QTimer.singleShot(800, self._apply_keep_dom_setting_to_web)
        QTimer.singleShot(900, self._apply_restore_pruned_on_view_setting_to_web)
        QTimer.singleShot(1000, self._apply_scroll_sync_debug_setting_to_web)

    @Slot(str)
    def on_delta_received(self, json_string: str) -> None:
        """Apply JS extractor deltas and schedule a debounced offline snapshot save."""
        try:
            payload = json.loads(json_string)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, list):
            return
        self.model.apply_deltas(payload)
        self._schedule_persist_offline_snapshot()
        # Scroll to latest when new messages arrive; avoid jerky behavior by doing it async.
        if self._auto_scroll_enabled and self.model.rowCount() > 0:
            QTimer.singleShot(0, self._scroll_to_bottom)

    def _schedule_persist_offline_snapshot(self, delay_ms: int = 700) -> None:
        """Debounce SQLite snapshot writes to keep the UI responsive during streaming updates."""
        if self._offline_store is None:
            host = self._tabs_host
            if host is not None and hasattr(host, "schedule_persist_all"):
                try:
                    host.schedule_persist_all()  # type: ignore[attr-defined]
                except Exception:
                    pass
            return
        self._persist_timer.start(max(100, int(delay_ms)))
        host = self._tabs_host
        if host is not None and hasattr(host, "schedule_manifest_save"):
            try:
                host.schedule_manifest_save()  # type: ignore[attr-defined]
            except Exception:
                pass

    def _persist_offline_snapshot_now(self) -> None:
        """Write the current native snapshot for this tab to its SQLite file."""
        if self._offline_store is None:
            return
        try:
            url = self.web_view.url().toString()
        except Exception:
            url = ""
        try:
            title = self.web_view.title() or ""
        except Exception:
            title = ""
        tab_title = ""
        host = self._tabs_host
        if host is not None and hasattr(host, "tab_display_title_for_pane"):
            try:
                tab_title = str(host.tab_display_title_for_pane(self) or "")  # type: ignore[attr-defined]
            except Exception:
                tab_title = ""
        try:
            messages = self.model.messages_in_order()
            self.storage_db_file = self._offline_store.save_tab_snapshot(
                self.tab_id,
                url=url,
                title=title,
                tab_title=tab_title,
                messages=messages,
                db_file=self.storage_db_file,
            )
        except Exception:
            pass

    def _restore_offline_snapshot(self, snapshot: dict) -> None:
        """Restore native messages/collapse state from a previously saved SQLite snapshot."""
        if not isinstance(snapshot, dict):
            return
        payload = snapshot.get("messages")
        if isinstance(payload, list) and payload:
            try:
                self.model.apply_deltas(payload)
                # restore collapse state (not part of deltas contract)
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "").strip()
                    if not key or not item.get("collapsed"):
                        continue
                    row = self.model.row_for_key(key)
                    msg = self.model.message_at_row(row) if row >= 0 else None
                    if msg:
                        msg.collapsed = True
                self.left_pane._refresh_indices_from(0)
            except Exception:
                pass

    def offline_snapshot_url_guess(self) -> str:
        try:
            u = self.web_view.url().toString()
        except Exception:
            u = ""
        if u:
            return u
        page_state = None
        if self._offline_store is not None:
            try:
                snap = self._offline_store.load_tab_snapshot(self.tab_id, self.storage_db_file)
                page_state = (snap or {}).get("page_state") if isinstance(snap, dict) else None
            except Exception:
                page_state = None
        if isinstance(page_state, dict):
            return str(page_state.get("url") or "")
        return ""

    @Slot(str)
    def on_web_event_received(self, json_string: str) -> None:
        """Handle JS-side scroll sync events and route them to the native list."""
        try:
            evt = json.loads(json_string)
        except json.JSONDecodeError:
            return
        if not isinstance(evt, dict):
            return
        if evt.get("type") == "scroll_debug":
            if self._scroll_sync_debug_enabled:
                print(f"[scroll-sync] {json.dumps(evt, ensure_ascii=False)}")
            return
        if evt.get("type") != "scroll_top_key":
            return
        if not self._web_to_native_sync_enabled:
            return
        if self._cursor_is_over(self.left_pane.list_view.viewport()):
            return
        if time.monotonic() < self._ignore_web_scroll_events_until:
            return
        key = str(evt.get("key") or "")
        if not key:
            return
        try:
            progress = float(evt.get("progress", 0.0))
        except Exception:
            progress = 0.0
        progress = max(0.0, min(1.0, progress))
        self._suppress_native_scroll_until = time.monotonic() + 0.35
        ok = self.left_pane.scroll_key_with_progress(key, progress)
        if self._scroll_sync_debug_enabled:
            reason = str(evt.get("reason") or "")
            print(
                f"[scroll-sync] web->native key={key} progress={progress:.3f} "
                f"reason={reason or '-'} ok={ok}"
            )

    def _scroll_to_bottom(self) -> None:
        idx = self.model.index(max(0, self.model.rowCount() - 1), 0)
        if idx.isValid():
            self.left_pane.list_view.scrollTo(idx, QListView.PositionAtBottom)

    def _cursor_is_over(self, widget: QWidget) -> bool:
        try:
            gp = QCursor.pos()
            lp = widget.mapFromGlobal(gp)
            return widget.rect().contains(lp)
        except Exception:
            return False

    def _on_native_scroll_value_changed(self, _value: int) -> None:
        if not self._native_to_web_sync_enabled:
            return
        if self._cursor_is_over(self.web_view):
            return
        if time.monotonic() < self._suppress_native_scroll_until:
            return
        self._native_scroll_sync_timer.start(80)

    def _send_native_top_key_to_web(self) -> None:
        """Tell the WebView to scroll to the message currently visible at the top of the native list."""
        top_info = self.left_pane.top_visible_info()
        if not top_info:
            return
        key, progress = top_info
        if self._scroll_sync_debug_enabled:
            print(f"[scroll-sync] native->web request key={key} progress={progress:.3f}")
        self._ignore_web_scroll_events_until = time.monotonic() + 0.45
        script = (
            "(function(){"
            "if(window.__chatgptMirror && typeof window.__chatgptMirror.scrollToKey==='function'){"
            f"window.__chatgptMirror.scrollToKey({json.dumps(key)}, {progress:.6f});"
            "}"
            "})();"
        )
        self.web_view.page().runJavaScript(script)

    @Slot(bool)
    def _on_auto_scroll_changed(self, enabled: bool) -> None:
        self._auto_scroll_enabled = bool(enabled)

    @Slot(bool)
    def _on_web_to_native_sync_changed(self, enabled: bool) -> None:
        self._web_to_native_sync_enabled = bool(enabled)

    @Slot(bool)
    def _on_native_to_web_sync_changed(self, enabled: bool) -> None:
        self._native_to_web_sync_enabled = bool(enabled)

    @Slot(int)
    def _on_keep_dom_changed(self, count: int) -> None:
        self._keep_dom_count = max(5, int(count))
        self._apply_keep_dom_setting_to_web()

    def _apply_keep_dom_setting_to_web(self) -> None:
        script = (
            "(function(){"
            "if(window.__chatgptMirror){"
            f"window.__chatgptMirror.keepDom={int(self._keep_dom_count)};"
            "if(typeof window.__chatgptMirror.scanNow==='function'){window.__chatgptMirror.scanNow('keepdom_change');}"
            "}"
            "})();"
        )
        self.web_view.page().runJavaScript(script)

    @Slot(bool)
    def _on_restore_pruned_on_view_changed(self, enabled: bool) -> None:
        self._restore_pruned_on_view_enabled = bool(enabled)
        self._apply_restore_pruned_on_view_setting_to_web()

    def _apply_restore_pruned_on_view_setting_to_web(self) -> None:
        enabled_js = "true" if self._restore_pruned_on_view_enabled else "false"
        script = (
            "(function(){"
            "if(window.__chatgptMirror){"
            f"window.__chatgptMirror.restorePrunedOnView={enabled_js};"
            "if(typeof window.__chatgptMirror.scanNow==='function'){window.__chatgptMirror.scanNow('restore_pruned_toggle');}"
            "}"
            "})();"
        )
        self.web_view.page().runJavaScript(script)

    @Slot(bool)
    def _on_scroll_sync_debug_changed(self, enabled: bool) -> None:
        self._scroll_sync_debug_enabled = bool(enabled)
        self._apply_scroll_sync_debug_setting_to_web()

    def _apply_scroll_sync_debug_setting_to_web(self) -> None:
        enabled_js = "true" if self._scroll_sync_debug_enabled else "false"
        script = (
            "(function(){"
            "if(window.__chatgptMirror){"
            f"window.__chatgptMirror.scrollSyncDebug={enabled_js};"
            "}"
            "})();"
        )
        self.web_view.page().runJavaScript(script)

    @Slot(bool)
    def _on_native_image_firefox_headers_changed(self, enabled: bool) -> None:
        self._native_img_use_firefox_headers = bool(enabled)
        ImagePartWidget.set_use_firefox_headers(self._native_img_use_firefox_headers)
        # Rebuild rows so image widgets are recreated with the new request profile.
        self.left_pane._refresh_indices_from(0)

    def _apply_browser_language_setting(self) -> None:
        mode = getattr(self, "_browser_language_mode", "system")
        if mode == "en":
            accept_lang = "en-US,en;q=0.9"
        else:
            sys_locale = QLocale.system().bcp47Name() or "en-US"
            base = sys_locale.split("-")[0] if "-" in sys_locale else sys_locale
            accept_lang = f"{sys_locale},{base};q=0.9,en;q=0.7"
        try:
            self.web_profile.setHttpAcceptLanguage(accept_lang)
        except Exception:
            pass

    @Slot(str)
    def _on_browser_language_changed(self, mode: str) -> None:
        mode = (mode or "system").strip().lower()
        if mode not in {"system", "en"}:
            mode = "system"
        self._browser_language_mode = mode
        self._apply_browser_language_setting()
        # Reload so chatgpt.com can pick up the new Accept-Language header.
        self.web_view.reload()

    @Slot()
    def _on_reset_session_requested(self) -> None:
        result = QMessageBox.question(
            self,
            "Reset sessione",
            (
                "Vuoi cancellare cookie e cache del WebView e ricaricare chatgpt.com?\n\n"
                "Nota: potresti dover rifare il login."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if result != QMessageBox.Yes:
            return

        try:
            self.web_profile.cookieStore().deleteAllCookies()
        except Exception:
            pass
        try:
            self.web_profile.clearHttpCache()
        except Exception:
            pass
        try:
            self.web_profile.clearAllVisitedLinks()
        except Exception:
            pass

        # Best-effort cleanup for origin storage in the current page context.
        self.web_view.page().runJavaScript(
            """
            (async function(){
              try { localStorage.clear(); } catch(e) {}
              try { sessionStorage.clear(); } catch(e) {}
              try {
                if (window.indexedDB && indexedDB.databases) {
                  const dbs = await indexedDB.databases();
                  for (const db of dbs) { if (db && db.name) indexedDB.deleteDatabase(db.name); }
                }
              } catch(e) {}
              return true;
            })();
            """
        )
        self.web_view.setUrl(QUrl("https://chatgpt.com"))

    @Slot(str)
    def _on_export_requested(self, fmt: str) -> None:
        """Export the current conversation in the selected format."""
        all_messages = self._messages_for_export()
        if not all_messages:
            QMessageBox.information(self, "Esporta", "Nessun messaggio da esportare.")
            return
        want_pdf_zoom = fmt.lower().strip() == "pdf"
        selected = self._choose_export_message_range(len(all_messages), include_zoom=want_pdf_zoom)
        if selected is None:
            return
        start_idx, end_idx, zoom_percent, show_link_urls = selected
        messages = all_messages[start_idx:end_idx]
        if not messages:
            QMessageBox.information(self, "Esporta", "Intervallo selezionato vuoto.")
            return

        base_name = self._default_export_basename()
        fmt = fmt.lower().strip()
        if fmt == "md":
            path, _ = QFileDialog.getSaveFileName(
                self, "Esporta Markdown", f"{base_name}.md", "Markdown (*.md)"
            )
            if not path:
                return
            Path(path).write_text(self._conversation_as_markdown(messages), encoding="utf-8")
            return

        if fmt == "json":
            path, _ = QFileDialog.getSaveFileName(
                self, "Esporta JSON", f"{base_name}.json", "JSON (*.json)"
            )
            if not path:
                return
            payload = []
            for i, m in enumerate(messages, start=1):
                payload.append(
                    {
                        "index": i,
                        "key": m.key,
                        "role": m.role,
                        "parts": [
                            (
                                {"type": "text", "text": p.text}
                                if p.type == "text"
                                else (
                                    {"type": "code", "lang": p.lang, "code": p.code}
                                    if p.type == "code"
                                    else {"type": "image", "src": p.image_url, "alt": p.alt}
                                )
                            )
                            for p in m.parts
                        ],
                    }
                )
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return

        if fmt == "pdf":
            path, _ = QFileDialog.getSaveFileName(
                self, "Esporta PDF", f"{base_name}.pdf", "PDF (*.pdf)"
            )
            if not path:
                return
            self._export_pdf_from_messages(
                messages,
                path,
                zoom_percent=zoom_percent,
                show_link_urls=show_link_urls,
            )
            return

    def _choose_export_message_range(
        self,
        total_count: int,
        include_zoom: bool = False,
    ) -> Optional[tuple[int, int, int, bool]]:
        """Ask whether export should include all messages or a numeric 1-based range."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Export Range")
        dlg.setModal(True)

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        info = QLabel(f"Messages available: {total_count}")
        info.setStyleSheet("QLabel { color: #374151; }")
        outer.addWidget(info)

        all_radio = QRadioButton("All")
        range_radio = QRadioButton("Range")
        all_radio.setChecked(True)
        outer.addWidget(all_radio)
        outer.addWidget(range_radio)

        grid = QGridLayout()
        grid.setContentsMargins(16, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        from_label = QLabel("From")
        to_label = QLabel("To")
        from_spin = QSpinBox()
        from_spin.setRange(1, max(1, total_count))
        from_spin.setValue(1)
        to_spin = QSpinBox()
        to_spin.setRange(1, max(1, total_count))
        to_spin.setValue(max(1, total_count))
        from_spin.setEnabled(False)
        to_spin.setEnabled(False)
        grid.addWidget(from_label, 0, 0)
        grid.addWidget(from_spin, 0, 1)
        grid.addWidget(to_label, 1, 0)
        grid.addWidget(to_spin, 1, 1)

        zoom_spin: Optional[QSpinBox] = None
        if include_zoom:
            zoom_label = QLabel("Zoom (%)")
            zoom_spin = QSpinBox()
            zoom_spin.setRange(20, 300)
            zoom_spin.setSingleStep(5)
            zoom_spin.setValue(100)
            grid.addWidget(zoom_label, 2, 0)
            grid.addWidget(zoom_spin, 2, 1)
        outer.addLayout(grid)

        links_checkbox: Optional[QCheckBox] = None
        if include_zoom:
            links_checkbox = QCheckBox("Write link URLs in full")
            links_checkbox.setChecked(False)
            outer.addWidget(links_checkbox)

        def _sync_range_enabled() -> None:
            enabled = range_radio.isChecked()
            from_spin.setEnabled(enabled)
            to_spin.setEnabled(enabled)

        all_radio.toggled.connect(_sync_range_enabled)
        range_radio.toggled.connect(_sync_range_enabled)
        _sync_range_enabled()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dlg)
        outer.addWidget(buttons)

        def _accept() -> None:
            if not range_radio.isChecked():
                dlg.accept()
                return
            f = int(from_spin.value())
            t = int(to_spin.value())
            if f > t:
                QMessageBox.warning(dlg, "Invalid Range", '"From" must be <= "To".')
                return
            if f < 1 or t > total_count:
                QMessageBox.warning(dlg, "Invalid Range", "Range is outside available messages.")
                return
            dlg.accept()

        buttons.accepted.connect(_accept)
        buttons.rejected.connect(dlg.reject)

        if dlg.exec() != QDialog.Accepted:
            return None
        zoom = int(zoom_spin.value()) if zoom_spin is not None else 100
        show_link_urls = bool(links_checkbox.isChecked()) if links_checkbox is not None else False
        if all_radio.isChecked():
            return (0, total_count, zoom, show_link_urls)
        # Convert 1-based inclusive range to python slice [start:end)
        start = int(from_spin.value()) - 1
        end = int(to_spin.value())
        return (start, end, zoom, show_link_urls)

    def _messages_for_export(self) -> List[Message]:
        """Build the export message list after applying image visibility toggles."""
        messages = self.model.messages_in_order()
        try:
            include_rich = self.left_pane.show_rich_entity_images_enabled()
            include_gallery = self.left_pane.show_gallery_images_enabled()
        except Exception:
            include_rich = True
            include_gallery = True
        if include_rich and include_gallery:
            return messages

        filtered: List[Message] = []
        for m in messages:
            parts: List[MessagePart] = []
            for p in m.parts:
                if p.type != "image":
                    parts.append(p)
                    continue
                kind = (p.image_kind or "").strip().lower()
                if kind == "gallery" and include_gallery:
                    parts.append(p)
                elif kind == "rich-entity" and include_rich:
                    parts.append(p)
                elif kind not in {"gallery", "rich-entity"} and (include_rich or include_gallery):
                    parts.append(p)
            if not parts:
                continue
            filtered.append(
                Message(
                    key=m.key,
                    role=m.role,
                    parts=parts,
                    collapsed=m.collapsed,
                    size_hint=m.size_hint,
                )
            )
        return filtered

    def _default_export_basename(self) -> str:
        title = self._current_chat_title_guess()
        if not title:
            return "chatgpt_mirror_export"
        safe = self._sanitize_filename_component(title)
        return safe or "chatgpt_mirror_export"

    def _current_chat_title_guess(self) -> str:
        title = ""
        try:
            title = (self.web_view.page().title() or "").strip()
        except Exception:
            title = ""
        if not title:
            try:
                title = (self.web_view.title() or "").strip()
            except Exception:
                title = ""
        # Best-effort cleanup for common ChatGPT page title suffixes.
        for sep in (" - ", " | "):
            if sep in title:
                left, right = title.rsplit(sep, 1)
                if right.strip().lower() == "chatgpt" and left.strip():
                    title = left.strip()
                    break
        if title.strip().lower() == "chatgpt":
            return ""
        return title.strip()

    def _sanitize_filename_component(self, value: str) -> str:
        v = (value or "").strip()
        v = re.sub(r"[\\/:*?\"<>|]+", "_", v)
        v = re.sub(r"\s+", " ", v).strip()
        v = v.strip(". ")
        if len(v) > 120:
            v = v[:120].rstrip()
        return v

    def _conversation_as_markdown(self, messages: List[Message]) -> str:
        lines: List[str] = ["# ChatGPT Mirror Export", ""]
        for i, msg in enumerate(messages, start=1):
            title = "You" if msg.role == "user" else "Assistant"
            lines.append(f"## {i}. {title}")
            lines.append("")
            for part in msg.parts:
                if part.type == "text":
                    txt = part.text.strip()
                    if txt:
                        lines.append(txt)
                        lines.append("")
                elif part.type == "code":
                    lang = normalize_code_lang((part.lang or "").strip())
                    code = part.code.rstrip("\n")
                    lines.append(f"```{lang}")
                    lines.append(code)
                    lines.append("```")
                    lines.append("")
                elif part.type == "image":
                    src = (part.image_url or "").strip()
                    if src:
                        alt = (part.alt or "").strip()
                        lines.append(f"![{alt}]({src})" if alt else f"![]({src})")
                        lines.append(
                            f'<!-- cgm-thumb src="{src}" alt="{alt.replace(chr(34), "&quot;")}" -->'
                        )
                        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _markdown_fragment_to_html(self, markdown_text: str, show_link_urls: bool = False) -> str:
        marker_pattern = re.compile(
            r'<!--\s*cgm-thumb\s+src="([^"]+)"(?:\s+alt="([^"]*)")?\s*-->',
            flags=re.IGNORECASE,
        )
        md = markdown_text or ""
        # Hidden markers stay invisible in markdown renderers, but we can materialize them for PDF HTML.
        def _marker_repl(m: re.Match) -> str:
            src = m.group(1) or ""
            alt = (m.group(2) or "image")
            pdf_src = self._pdf_local_thumb_url(src)
            return (
                f'\n\n<div class="image-part"><a href="{html_escape(src)}">{html_escape(alt)}</a><br>'
                f'<img src="{html_escape(pdf_src)}" alt="{html_escape(alt)}" class="inline-image"></div>\n\n'
            )
        md = marker_pattern.sub(_marker_repl, md)
        doc = QTextDocument(self)
        try:
            doc.setMarkdown(md)
        except Exception:
            doc.setPlainText(md)
        html = doc.toHtml()
        m = re.search(r"<body[^>]*>(.*)</body>", html, flags=re.IGNORECASE | re.DOTALL)
        body_html = m.group(1) if m else html
        if show_link_urls:
            body_html = re.sub(
                r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>',
                r'<a href="\1">\2</a><span class="link-url"> (\1)</span>',
                body_html,
                flags=re.IGNORECASE | re.DOTALL,
            )
        return body_html

    def _code_html_for_pdf(self, code: str, lang: str) -> str:
        code = code.rstrip("\n")
        lang = normalize_code_lang(lang)
        if PYGMENTS_AVAILABLE and pygments_highlight and HtmlFormatter and get_lexer_by_name and TextLexer:
            try:
                lexer = get_lexer_by_name(lang) if lang else TextLexer()
            except Exception:
                lexer = TextLexer()
            formatter = HtmlFormatter(cssclass="codehilite", nowrap=False, style=PDF_PYGMENTS_STYLE)
            highlighted = pygments_highlight(code, lexer, formatter)
            title = html_escape(lang or "code")
            return (
                f'<div class="code-wrap"><div class="code-head">{title}</div>'
                f'{highlighted}</div>'
            )
        # Fallback: no Pygments installed
        title = html_escape(lang or "code")
        return (
            f'<div class="code-wrap"><div class="code-head">{title}</div>'
            f'<pre class="codehilite"><code>{html_escape(code)}</code></pre></div>'
        )

    def _pdf_local_thumb_url(self, src: str) -> str:
        src = (src or "").strip()
        if not src:
            return src
        data = IMAGE_BYTES_CACHE.get(src)
        if not data:
            return src
        try:
            h = hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]
            cache_dir = Path(tempfile.gettempdir()) / "chatgpt_mirror_pdf_images"
            cache_dir.mkdir(parents=True, exist_ok=True)
            ext = ".img"
            if data.startswith(b"\x89PNG"):
                ext = ".png"
            elif data[:3] == b"\xff\xd8\xff":
                ext = ".jpg"
            elif data[:6] in (b"GIF87a", b"GIF89a"):
                ext = ".gif"
            elif data.startswith(b"RIFF") and b"WEBP" in data[:16]:
                ext = ".webp"
            fpath = cache_dir / f"{h}{ext}"
            if (not fpath.exists()) or fpath.stat().st_size != len(data):
                fpath.write_bytes(data)
            return fpath.resolve().as_uri()
        except Exception:
            return src

    def _conversation_as_html_for_pdf(
        self,
        messages: List[Message],
        zoom_percent: int = 100,
        show_link_urls: bool = False,
    ) -> str:
        zoom_percent = max(20, min(300, int(zoom_percent or 100)))
        pygments_css = ""
        if PYGMENTS_AVAILABLE and HtmlFormatter:
            try:
                pygments_css = HtmlFormatter(
                    cssclass="codehilite",
                    style=PDF_PYGMENTS_STYLE,
                ).get_style_defs(".codehilite")
            except Exception:
                pygments_css = ""

        blocks: List[str] = []
        for i, msg in enumerate(messages, start=1):
            role = "You" if msg.role == "user" else "Assistant"
            role_class = "user" if msg.role == "user" else "assistant"
            blocks.append(
                f'<section class="msg {role_class}"><div class="msg-head">{i}. {html_escape(role)}</div>'
            )
            for part in msg.parts:
                if part.type == "text":
                    txt = part.text.strip()
                    if txt:
                        blocks.append(
                            f'<div class="text-part">{self._markdown_fragment_to_html(txt, show_link_urls=show_link_urls)}</div>'
                        )
                elif part.type == "code":
                    blocks.append(self._code_html_for_pdf(part.code, part.lang))
                elif part.type == "image":
                    src = (part.image_url or "").strip()
                    if src:
                        alt = html_escape((part.alt or "").strip() or "image")
                        pdf_src = self._pdf_local_thumb_url(src)
                        link_label = html_escape(src) if show_link_urls else "link"
                        blocks.append(
                            f'<div class="image-part"><a href="{html_escape(src)}">{link_label}</a><br>'
                            f'<img src="{html_escape(pdf_src)}" alt="{alt}" class="inline-image"></div>'
                        )
            blocks.append("</section>")

        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    @page {{
      margin: 12mm;
    }}
    body {{
      font-family: "DejaVu Sans", "Liberation Sans", "Noto Sans", "Ubuntu", sans-serif;
      color: #111827;
      font-size: 11pt;
      line-height: 1.38;
      margin: 0;
      zoom: {zoom_percent}%;
    }}
    h1,h2,h3,h4,h5,h6 {{ color: #111827; }}
    p {{ margin: 0 0 8px 0; }}
    .msg {{
      border: 1px solid #dbe2ea;
      border-radius: 10px;
      padding: 8pt 9pt;
      margin: 0 0 10pt 0;
      /* Allow page breaks across long messages; avoiding breaks here can create
         large gaps and make every message jump to a new page. */
      page-break-inside: auto;
      break-inside: auto;
    }}
    .msg-head {{
      font-weight: 700;
      font-size: 10.5pt;
      margin-bottom: 6pt;
      color: #1f2937;
      page-break-after: avoid;
      break-after: avoid;
    }}
    .msg.user .msg-head {{ color: #065f46; }}
    .msg.assistant .msg-head {{ color: #1e3a8a; }}
    .text-part {{ margin-bottom: 6px; }}
    .image-part {{
      margin: 5pt 0 8pt 0;
      border: 1px solid #dbe2ea;
      border-radius: 8px;
      padding: 5pt;
      page-break-inside: avoid;
    }}
    .inline-image {{
      max-width: 280pt;
      max-height: 180pt;
      margin-top: 4pt;
    }}
    .code-wrap {{
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      overflow: hidden;
      margin: 5pt 0 8pt 0;
      page-break-inside: avoid;
      background: #ffffff;
    }}
    .code-head {{
      background: #e2e8f0;
      color: #1f2937;
      font-size: 8.5pt;
      font-weight: 600;
      padding: 3pt 6pt;
      border-bottom: 1px solid #cbd5e1;
      text-transform: lowercase;
    }}
    pre.codehilite, .codehilite {{
      margin: 0;
      padding: 6pt 7pt;
      background: #f8fafc !important;
      color: #111827;
      font-family: "DejaVu Sans Mono", "Liberation Mono", monospace;
      font-size: 9.5pt;
      white-space: pre-wrap;
      word-wrap: break-word;
    }}
    .codehilite pre {{ margin: 0; white-space: pre-wrap; }}
    code {{ font-family: "DejaVu Sans Mono", monospace; }}
    blockquote {{
      border-left: 3px solid #cbd5e1;
      padding-left: 8px;
      color: #374151;
      margin: 6px 0;
    }}
    a {{ color: #1d4ed8; text-decoration: none; }}
    .link-url {{
      color: #4b5563;
      font-size: 8.5pt;
      word-break: break-all;
    }}
    {pygments_css}
  </style>
</head>
<body>
{''.join(blocks)}
</body>
</html>"""

    def _export_pdf_from_markdown(self, markdown_text: str, path: str) -> None:
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)

        doc = QTextDocument(self)
        # Render the PDF starting from Markdown so the export stays close to message structure.
        try:
            doc.setMarkdown(markdown_text)
        except Exception:
            # Fallback for environments with limited markdown support.
            doc.setPlainText(markdown_text)
        doc.print_(printer)

    def _export_pdf_from_messages(
        self,
        messages: List[Message],
        path: str,
        zoom_percent: int = 100,
        show_link_urls: bool = False,
    ) -> None:
        html = self._conversation_as_html_for_pdf(
            messages,
            zoom_percent=zoom_percent,
            show_link_urls=show_link_urls,
        )

        # Prefer WebEngine's PDF renderer: much better support for HTML/CSS/images than QTextDocument/QPrinter.
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "chatgpt_mirror_pdf_render"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            html_path = tmp_dir / f"export_{int(time.time() * 1000)}.html"
            html_path.write_text(html, encoding="utf-8")

            page = QWebEnginePage(self.web_profile, self)
            loop = QEventLoop(self)
            state = {"loaded": False, "printed": False, "ok": False}
            timeout = QTimer(self)
            timeout.setSingleShot(True)

            def _finish_loop():
                if loop.isRunning():
                    loop.quit()

            def _on_timeout():
                _finish_loop()

            def _on_load(ok: bool):
                state["loaded"] = True
                if not ok:
                    _finish_loop()
                    return
                try:
                    if hasattr(page, "pdfPrintingFinished"):
                        page.pdfPrintingFinished.connect(_on_pdf_finished)  # type: ignore[attr-defined]
                    layout = QPageLayout(
                        QPageSize(QPageSize.A4),
                        QPageLayout.Portrait,
                        QMarginsF(12, 12, 12, 12),
                        QPageLayout.Millimeter,
                    )
                    page.printToPdf(path, layout)
                except Exception as exc:
                    _finish_loop()

            def _on_pdf_finished(file_path: str, success: bool):
                state["printed"] = True
                state["ok"] = bool(success)
                _finish_loop()

            page.loadFinished.connect(_on_load)
            timeout.timeout.connect(_on_timeout)
            timeout.start(20000)
            page.load(QUrl.fromLocalFile(str(html_path)))
            loop.exec()
            timeout.stop()
            try:
                page.deleteLater()
            except Exception:
                pass
            # Cleanup temp HTML file lazily.
            try:
                html_path.unlink(missing_ok=True)
            except Exception:
                pass

            if state.get("printed") and state.get("ok") and Path(path).exists() and Path(path).stat().st_size > 0:
                return
        except Exception as exc:
            _ = exc

        # Fallback: QTextDocument/QPrinter (less reliable for images, but still exports text/code).
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        try:
            printer.setPageMargins(12, 12, 12, 12, QPrinter.Millimeter)
        except Exception:
            pass
        doc = QTextDocument(self)
        doc.setDefaultFont(QApplication.font())
        doc.setHtml(html)
        doc.print_(printer)

    @Slot()
    def _on_export_debug_visible_requested(self) -> None:
        key = self.left_pane.top_visible_key()
        if not key:
            QMessageBox.information(self, "Debug export", "Nessun blocco visibile rilevato.")
            return
        row = self.model.row_for_key(key)
        msg = self.model.message_at_row(row) if row >= 0 else None
        if not msg:
            QMessageBox.information(self, "Debug export", "Messaggio non trovato nel modello.")
            return

        chat_base = self._default_export_basename()
        default_name = (
            f"{chat_base}__debug_{key[:32].replace('/', '_')}.txt"
            if chat_base
            else f"chatgpt_mirror_debug_{key[:32].replace('/', '_')}.txt"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Esporta debug blocco visibile", default_name, "Text (*.txt)"
        )
        if not path:
            return

        # Build local debug payload first (available immediately).
        text_parts_md = [p.text for p in msg.parts if p.type == "text" and p.text.strip()]
        raw_md = "\n\n".join(text_parts_md).strip()
        parts_json = json.dumps(
            [
                (
                    {"type": "text", "text": p.text}
                    if p.type == "text"
                    else (
                                    {"type": "code", "lang": p.lang, "code": p.code}
                                    if p.type == "code"
                                    else {
                                        "type": "image",
                                        "src": p.image_url,
                                        "alt": p.alt,
                                        "kind": p.image_kind,
                                    }
                                )
                            )
                for p in msg.parts
            ],
            ensure_ascii=False,
            indent=2,
        )

        js = (
            "(function(){"
            f"var key={json.dumps(key)};"
            "var nodes=document.querySelectorAll('[data-cgm-message-key]');"
            "for(var i=0;i<nodes.length;i++){"
            " if(nodes[i].getAttribute('data-cgm-message-key')===String(key)){"
            "  return nodes[i].outerHTML || '';"
            " }"
            "}"
            "return '';"
            "})();"
        )

        def _write_debug_file(dom_html: object) -> None:
            dom_text = dom_html if isinstance(dom_html, str) else ""
            if not dom_text:
                dom_text = "[DOM non trovato nel WebView per questa key (possibile pruning o key mismatch)]"

            body = "\n".join(
                [
                    "=== CHATGPT MIRROR DEBUG BLOCK ===",
                    f"key: {msg.key}",
                    f"role: {msg.role}",
                    f"row: {row}",
                    "",
                    "=== RAW PARTS JSON ===",
                    parts_json,
                    "",
                    "=== RAW MARKDOWN (estratto) ===",
                    raw_md or "[vuoto]",
                    "",
                    "=== QT MARKDOWN (post-normalizzazione) ===",
                    "[normalizzazione Qt rimossa]",
                    "",
                    "=== DOM OUTER HTML (WebView) ===",
                    dom_text,
                    "",
                ]
            )
            try:
                Path(path).write_text(body, encoding="utf-8")
                QMessageBox.information(self, "Debug export", f"Salvato in:\n{path}")
            except Exception as exc:
                QMessageBox.critical(self, "Debug export", f"Errore salvataggio:\n{exc}")

        self.web_view.page().runJavaScript(js, _write_debug_file)

    @Slot()
    def _on_export_pdf_images_debug_requested(self) -> None:
        key = self.left_pane.top_visible_key()
        if not key:
            QMessageBox.information(self, "Debug PDF immagini", "Nessun blocco visibile rilevato.")
            return
        row = self.model.row_for_key(key)
        msg = self.model.message_at_row(row) if row >= 0 else None
        if not msg:
            QMessageBox.information(self, "Debug PDF immagini", "Messaggio non trovato nel modello.")
            return

        image_parts = [p for p in msg.parts if p.type == "image" and (p.image_url or "").strip()]
        if not image_parts:
            QMessageBox.information(self, "Debug PDF immagini", "Nessuna immagine nel messaggio visibile.")
            return

        base_name = self._default_export_basename()
        default_name = f"{base_name}__pdf_images_debug_{key[:24].replace('/', '_')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Debug PDF immagini", default_name, "Text (*.txt)"
        )
        if not path:
            return

        lines: List[str] = []
        lines.append("=== CHATGPT MIRROR PDF IMAGE DEBUG ===")
        lines.append(f"key: {key}")
        lines.append(f"row: {row}")
        lines.append("")
        for i, p in enumerate(image_parts, start=1):
            src = (p.image_url or "").strip()
            alt = (p.alt or "").strip()
            cache_data = IMAGE_BYTES_CACHE.get(src)
            cache_hit = cache_data is not None
            local_ref = self._pdf_local_thumb_url(src)
            local_path = ""
            exists = False
            size = 0
            if local_ref.startswith("file://"):
                try:
                    local_path = QUrl(local_ref).toLocalFile()
                    fp = Path(local_path)
                    exists = fp.exists()
                    size = fp.stat().st_size if exists else 0
                except Exception:
                    pass
            lines.extend(
                [
                    f"[{i}] src={src}",
                    f"    alt={alt}",
                    f"    cache_hit={cache_hit}",
                    f"    cache_bytes={(len(cache_data) if cache_data else 0)}",
                    f"    pdf_ref={local_ref}",
                    f"    local_exists={exists}",
                    f"    local_size={size}",
                    "",
                ]
            )

        html_snippet = self._conversation_as_html_for_pdf([msg])
        lines.append("=== HTML SNIPPET ===")
        lines.append(html_snippet[:12000])
        lines.append("")
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            QMessageBox.information(self, "Debug PDF immagini", f"Salvato in:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Debug PDF immagini", f"Errore salvataggio:\n{exc}")


class TabbedMainWindow(QMainWindow):
    """Top-level window hosting multiple `MainWindow` panes as tabs.

    It owns the shared WebEngine profile (single login session) and the tab manifest.
    """
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1600, 900)
        self._profile_root = ensure_profile_root()
        self._data_root = ensure_data_root()
        self._offline_store = OfflineStore(self._data_root)
        self.web_profile = QWebEngineProfile("chatgpt_mirror_profile", self)
        self.web_profile.setPersistentStoragePath(str(self._profile_root / "qtwebengine"))
        self.web_profile.setCachePath(str(self._profile_root / "qtwebengine-cache"))
        self.web_profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self.web_profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.currentChanged.connect(lambda _i: self.schedule_manifest_save())
        self._tabs_plus_btn = QToolButton(self)
        self._tabs_plus_btn.setText("+")
        self._tabs_plus_btn.setPopupMode(QToolButton.InstantPopup)
        self._tabs_plus_btn.setToolTip("Nuovo / Apri")
        self._tabs_plus_btn.setCursor(Qt.PointingHandCursor)
        self._tabs_plus_btn.setStyleSheet(
            "QToolButton { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; "
            "padding: 1px 8px; font-weight: 700; }"
            "QToolButton:hover { background: #eef2f7; }"
        )
        plus_menu = QMenu(self._tabs_plus_btn)
        act_new_tab = QAction("New Tab", self)
        act_new_tab.triggered.connect(lambda: self.create_mirror_tab(url="https://chatgpt.com", switch=True))
        plus_menu.addAction(act_new_tab)
        act_open_tab = QAction("Open Local Snapshot (.sqlite)…", self)
        act_open_tab.triggered.connect(self._open_snapshot_dialog)
        plus_menu.addAction(act_open_tab)
        self._tabs_plus_btn.setMenu(plus_menu)
        self.tabs.setCornerWidget(self._tabs_plus_btn, Qt.TopLeftCorner)
        self.setCentralWidget(self.tabs)
        self._manifest_save_timer = QTimer(self)
        self._manifest_save_timer.setSingleShot(True)
        self._manifest_save_timer.timeout.connect(self._save_manifest_now)
        self._snapshot_save_timer = QTimer(self)
        self._snapshot_save_timer.setSingleShot(True)
        self._snapshot_save_timer.timeout.connect(self._persist_all_tabs_now)

        self._restore_tabs_from_disk()
        if self.tabs.count() == 0:
            self.create_mirror_tab(url="https://chatgpt.com", switch=True)

    def create_mirror_tab(
        self,
        url: Optional[str] = "https://chatgpt.com",
        switch: bool = True,
        tab_id: Optional[str] = None,
        storage_db_file: Optional[str] = None,
        initial_snapshot: Optional[dict] = None,
    ) -> Optional[MainWindow]:
        """Create a new in-app tab, optionally restoring an offline snapshot."""
        pane = MainWindow(
            tabs_host=self,
            shared_profile=self.web_profile,
            profile_root=self._profile_root,
            offline_store=self._offline_store,
            tab_id=tab_id,
            storage_db_file=storage_db_file,
            initial_snapshot=initial_snapshot,
            initial_url=url,
        )
        idx = self.tabs.addTab(pane, "Nuovo tab")
        saved_tab_title = ""
        if isinstance(initial_snapshot, dict):
            page_state = initial_snapshot.get("page_state")
            if isinstance(page_state, dict):
                saved_tab_title = str(page_state.get("tab_title") or "").strip()
        if saved_tab_title:
            shown = saved_tab_title if len(saved_tab_title) <= 28 else (saved_tab_title[:27].rstrip() + "…")
            self.tabs.setTabText(idx, shown)
            self.tabs.setTabToolTip(idx, saved_tab_title)
        try:
            pane.web_view.titleChanged.connect(lambda title, p=pane: self._update_tab_title_for_pane(p, title))
            pane.web_view.urlChanged.connect(lambda _u, p=pane: self._update_tab_title_for_pane(p, pane.web_view.title()))
            pane.web_view.urlChanged.connect(lambda _u: self.schedule_manifest_save())
        except Exception:
            pass
        if not saved_tab_title:
            self._update_tab_title_for_pane(pane, pane.web_view.title())
        if switch:
            self.tabs.setCurrentIndex(idx)
        self.schedule_manifest_save()
        return pane

    def _pane_tab_index(self, pane: MainWindow) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is pane:
                return i
        return -1

    def _is_generic_chatgpt_title(self, title: str) -> bool:
        t = (title or "").strip().lower()
        return t.startswith("chatgpt.com")

    def tab_display_title_for_pane(self, pane: MainWindow) -> str:
        idx = self._pane_tab_index(pane)
        if idx < 0:
            return ""
        try:
            return self.tabs.tabText(idx) or ""
        except Exception:
            return ""

    def _focus_or_open_snapshot_db(self, db_file_name: str) -> bool:
        db_file_name = Path(db_file_name or "").name
        if not db_file_name:
            return False
        for i in range(self.tabs.count()):
            pane = self.tabs.widget(i)
            if isinstance(pane, MainWindow) and (pane.storage_db_file or "") == db_file_name:
                self.tabs.setCurrentIndex(i)
                return True
        try:
            snapshot = self._offline_store.load_tab_snapshot(uuid.uuid4().hex[:8], db_file_name)
        except Exception:
            snapshot = None
        if not isinstance(snapshot, dict):
            return False
        page_state = snapshot.get("page_state") if isinstance(snapshot.get("page_state"), dict) else {}
        url = str((page_state or {}).get("url") or "https://chatgpt.com")
        pane = self.create_mirror_tab(
            url=url,
            switch=True,
            tab_id=uuid.uuid4().hex[:12],
            storage_db_file=db_file_name,
            initial_snapshot=snapshot,
        )
        return pane is not None

    @Slot()
    def _open_snapshot_dialog(self) -> None:
        start_dir = str(self._offline_store.tabs_dir)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Apri snapshot locale",
            start_dir,
            "SQLite (*.sqlite);;All files (*)",
        )
        if not path:
            return
        chosen = Path(path)
        if chosen.suffix.lower() != ".sqlite":
            QMessageBox.information(self, "Apri snapshot", "Seleziona un file .sqlite.")
            return
        # MVP: support guaranteed for snapshots in data/tabs/.
        if chosen.parent.resolve() != self._offline_store.tabs_dir.resolve():
            QMessageBox.information(
                self,
                "Apri snapshot",
                f"Per ora seleziona un file dentro:\n{self._offline_store.tabs_dir}",
            )
            return
        if not self._focus_or_open_snapshot_db(chosen.name):
            QMessageBox.warning(self, "Apri snapshot", "Impossibile aprire lo snapshot selezionato.")

    def _update_tab_title_for_pane(self, pane: MainWindow, title: str) -> None:
        idx = self._pane_tab_index(pane)
        if idx < 0:
            return
        current_text = (self.tabs.tabText(idx) or "").strip()
        incoming_raw = (title or "").strip()
        if self._is_generic_chatgpt_title(incoming_raw):
            if current_text and current_text not in {"Nuovo tab", "ChatGPT"}:
                return
        t = (title or "").strip()
        if " - " in t:
            left, right = t.rsplit(" - ", 1)
            if right.strip().lower() == "chatgpt" and left.strip():
                t = left.strip()
        if not t:
            try:
                url = pane.web_view.url().toString()
            except Exception:
                url = ""
            t = "ChatGPT" if "chatgpt.com" in url else "Nuovo tab"
        if len(t) > 28:
            t = t[:27].rstrip() + "…"
        self.tabs.setTabText(idx, t)
        self.tabs.setTabToolTip(idx, title or t)

    def _on_tab_close_requested(self, index: int) -> None:
        if self.tabs.count() <= 1:
            return
        w = self.tabs.widget(index)
        if isinstance(w, MainWindow):
            try:
                w._persist_offline_snapshot_now()
            except Exception:
                pass
        self.tabs.removeTab(index)
        if w is not None:
            w.deleteLater()
        self.schedule_manifest_save()

    def schedule_manifest_save(self, delay_ms: int = 300) -> None:
        self._manifest_save_timer.start(max(100, int(delay_ms)))

    def schedule_persist_all(self, delay_ms: int = 900) -> None:
        self._snapshot_save_timer.start(max(250, int(delay_ms)))
        self.schedule_manifest_save()

    def _save_manifest_now(self) -> None:
        """Persist open-tab order/current tab and each tab's current SQLite filename."""
        tabs_payload = []
        for i in range(self.tabs.count()):
            pane = self.tabs.widget(i)
            if not isinstance(pane, MainWindow):
                continue
            try:
                url = pane.web_view.url().toString() or pane.offline_snapshot_url_guess()
            except Exception:
                url = ""
            tabs_payload.append(
                {
                    "tab_id": pane.tab_id,
                    "url": url,
                    "db_file": pane.storage_db_file or "",
                }
            )
        try:
            self._offline_store.save_manifest(
                {"current_index": max(0, self.tabs.currentIndex()), "tabs": tabs_payload}
            )
        except Exception:
            pass

    def _persist_all_tabs_now(self) -> None:
        """Force-save offline snapshots for all open tabs (used on close and debounced checkpoints)."""
        for i in range(self.tabs.count()):
            pane = self.tabs.widget(i)
            if isinstance(pane, MainWindow):
                try:
                    pane._persist_offline_snapshot_now()
                except Exception:
                    pass

    def _restore_tabs_from_disk(self) -> None:
        """Recreate tabs from the saved manifest and restore their native snapshots."""
        try:
            manifest = self._offline_store.load_manifest()
        except Exception:
            manifest = {"tabs": [], "current_index": 0}
        tabs = manifest.get("tabs") or []
        restored = 0
        for item in tabs:
            if not isinstance(item, dict):
                continue
            tab_id = str(item.get("tab_id") or "").strip()
            if not tab_id:
                continue
            db_file = str(item.get("db_file") or "").strip() or None
            snapshot = None
            try:
                snapshot = self._offline_store.load_tab_snapshot(tab_id, db_file)
            except Exception:
                snapshot = None
            page_state = (snapshot or {}).get("page_state") if isinstance(snapshot, dict) else {}
            url = str((page_state or {}).get("url") or item.get("url") or "https://chatgpt.com")
            if not url:
                url = "https://chatgpt.com"
            pane = self.create_mirror_tab(
                url=url,
                switch=False,
                tab_id=tab_id,
                storage_db_file=db_file,
                initial_snapshot=snapshot,
            )
            if pane is not None:
                restored += 1
        if restored:
            try:
                idx = int(manifest.get("current_index") or 0)
            except Exception:
                idx = 0
            idx = max(0, min(idx, self.tabs.count() - 1))
            self.tabs.setCurrentIndex(idx)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._persist_all_tabs_now()
            self._save_manifest_now()
        except Exception:
            pass
        super().closeEvent(event)


def main() -> int:
    """Application entry point."""
    app = QApplication(sys.argv)
    app.setApplicationName("chatgpt_mirror")
    app.setStyleSheet(
        """
        QWidget { font-family: "Noto Sans", "Ubuntu", sans-serif; font-size: 13px; }
        QPushButton {
            background: #f8fafc;
            border: 1px solid #cbd5e1;
            border-radius: 8px;
            padding: 4px 10px;
        }
        QPushButton:hover { background: #eef2f7; }
        QPushButton:pressed { background: #e2e8f0; }
        """
    )
    window = TabbedMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
