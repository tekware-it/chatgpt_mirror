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
import argparse
import tempfile
import time
import uuid
from html import escape as html_escape
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


def _normalize_fontconfig_env() -> None:
    """Ensure Linux Fontconfig env vars are valid before Qt initializes.

    Some environments (especially after AppImage runs) can leak invalid values
    like FONTCONFIG_FILE="(null)", which causes repeated runtime warnings.
    """
    if not sys.platform.startswith("linux"):
        return
    invalid_tokens = {"", "null", "(null)"}
    fc_path = (os.environ.get("FONTCONFIG_PATH") or "").strip()
    fc_file = (os.environ.get("FONTCONFIG_FILE") or "").strip()

    if fc_file.lower() in invalid_tokens or (fc_file and not os.path.exists(fc_file)):
        os.environ.pop("FONTCONFIG_FILE", None)

    if fc_path.lower() in invalid_tokens or (fc_path and not os.path.exists(fc_path)):
        etc_fonts = "/etc/fonts"
        if os.path.isdir(etc_fonts):
            os.environ["FONTCONFIG_PATH"] = etc_fonts

    if "FONTCONFIG_FILE" not in os.environ:
        default_fc_file = "/etc/fonts/fonts.conf"
        if os.path.isfile(default_fc_file):
            os.environ["FONTCONFIG_FILE"] = default_fc_file


_normalize_fontconfig_env()

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
    QSplashScreen,
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
    import psutil

    PSUTIL_AVAILABLE = True
    PSUTIL_IMPORT_ERROR = ""
except Exception:
    psutil = None  # type: ignore[assignment]
    PSUTIL_AVAILABLE = False
    PSUTIL_IMPORT_ERROR = "import error"

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
PERF_LOG_ENABLED = False
JS_MEM_LOG_ENABLED = False


def perf_log(msg: str) -> None:
    """Emit lightweight perf logs when enabled via CLI/env."""
    if not PERF_LOG_ENABLED:
        return
    try:
        now = time.strftime("%H:%M:%S")
        ms = int((time.time() % 1.0) * 1000)
        print(f"[perf {now}.{ms:03d}] {msg}")
    except Exception:
        pass


class DeferredTabPlaceholder(QWidget):
    """Lightweight tab placeholder used to keep startup fast with many saved tabs."""
    def __init__(self, tab_id: str, url: str, db_file: str, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.tab_id = (tab_id or "").strip()
        self.url = (url or "").strip()
        self.db_file = (db_file or "").strip()
        self.title = (title or "").strip()

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
        defer_web_load: bool = False,
        restore_snapshot_on_create: bool = True,
        js_mem_log: bool = False,
    ) -> None:
        super().__init__()
        self._tabs_host = tabs_host
        self._offline_store = offline_store
        self.tab_id = (tab_id or uuid.uuid4().hex[:12]).strip()
        self.storage_db_file = (storage_db_file or "").strip() or None
        self._defer_web_load = bool(defer_web_load)
        self._pending_initial_url = initial_url or ""
        self._snapshot_restored = False
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_snapshot_async_step)
        self._restore_pending_messages: List[dict] = []
        self._restore_total_messages = 0
        self._restore_in_progress = False
        self._restore_progress_cb: Optional[Callable[[int, int], None]] = None
        self._restore_done_cb: Optional[Callable[[], None]] = None
        self._restore_chunk_size = 80
        self._restore_tick_delay_ms = 8
        self._js_mem_log_enabled = bool(js_mem_log)
        self._js_mem_log_timer = QTimer(self)
        self._js_mem_log_timer.setSingleShot(False)
        self._js_mem_log_timer.timeout.connect(self._log_web_js_memory_snapshot)
        # Tab title updates stay locked until page load succeeds.
        self._tab_title_locked = True
        self._tab_title_lock_seq = 0
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
        # Live DOM deltas can arrive in large bursts after page hydration.
        # Process them incrementally to keep the UI responsive.
        self._dom_delta_queue_order: List[str] = []
        self._dom_delta_queue_map: Dict[str, dict] = {}
        self._dom_delta_timer = QTimer(self)
        self._dom_delta_timer.setSingleShot(True)
        self._dom_delta_timer.timeout.connect(self._process_dom_delta_chunk)
        self._dom_delta_chunk_size = 12
        self._dom_delta_autoscroll_pending = False
        self.left_pane.list_view.verticalScrollBar().valueChanged.connect(self._on_native_scroll_value_changed)
        self.left_pane.autoScrollChanged.connect(self._on_auto_scroll_changed)
        self.left_pane.webToNativeSyncChanged.connect(self._on_web_to_native_sync_changed)
        self.left_pane.nativeToWebSyncChanged.connect(self._on_native_to_web_sync_changed)
        self.left_pane.keepDomChanged.connect(self._on_keep_dom_changed)
        self.left_pane.restorePrunedOnViewChanged.connect(self._on_restore_pruned_on_view_changed)
        self.left_pane.scrollSyncDebugChanged.connect(self._on_scroll_sync_debug_changed)
        self.left_pane.nativeImageFirefoxHeadersChanged.connect(self._on_native_image_firefox_headers_changed)
        self.left_pane.backgroundTabsPolicyChanged.connect(self._on_background_tabs_policy_changed)
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

        if initial_snapshot and restore_snapshot_on_create:
            self._restore_offline_snapshot(initial_snapshot)
            self._snapshot_restored = True
        elif not self.storage_db_file:
            self._snapshot_restored = True

        self._apply_browser_language_setting()
        if self._tabs_host is not None and hasattr(self._tabs_host, "background_tabs_policy"):
            try:
                self.left_pane.set_background_tabs_policy(self._tabs_host.background_tabs_policy())  # type: ignore[attr-defined]
            except Exception:
                pass
        self.web_view.urlChanged.connect(lambda _u: self._schedule_persist_offline_snapshot())
        self.web_view.titleChanged.connect(lambda _t: self._schedule_persist_offline_snapshot())
        if (not self._defer_web_load) and initial_url:
            self.web_view.setUrl(QUrl(initial_url))

    def ensure_activated(self) -> None:
        """Lazily trigger only WebView load when this tab is first shown."""
        try:
            self.left_pane.trigger_viewport_hydration()
        except Exception:
            pass
        if self._js_mem_log_enabled and not self._js_mem_log_timer.isActive():
            self._js_mem_log_timer.start(5000)
            QTimer.singleShot(800, self._log_web_js_memory_snapshot)
        if self._defer_web_load and self._pending_initial_url:
            perf_log(f"ensure_activated tab={self.tab_id} load_url_start")
            self._defer_web_load = False
            self.web_view.setUrl(QUrl(self._pending_initial_url))
            perf_log(f"ensure_activated tab={self.tab_id} load_url_done")

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
        if self._js_mem_log_enabled and not self._js_mem_log_timer.isActive():
            self._js_mem_log_timer.start(5000)
            QTimer.singleShot(800, self._log_web_js_memory_snapshot)

    def _log_web_js_memory_snapshot(self) -> None:
        """Print periodic JS memory/DOM stats for leak diagnostics."""
        if not self._js_mem_log_enabled:
            return
        print(f"[js-mem] tick tab={self.tab_id}", flush=True)
        js = (
            "(function(){"
            "  var out={};"
            "  try {"
            "    var pm=(window.performance&&window.performance.memory)?window.performance.memory:null;"
            "    out.used=pm&&pm.usedJSHeapSize?pm.usedJSHeapSize:0;"
            "    out.total=pm&&pm.totalJSHeapSize?pm.totalJSHeapSize:0;"
            "    out.limit=pm&&pm.jsHeapSizeLimit?pm.jsHeapSizeLimit:0;"
            "  } catch(e) { out.used=0; out.total=0; out.limit=0; }"
            "  try { out.domNodes=document.getElementsByTagName('*').length||0; } catch(e){ out.domNodes=0; }"
            "  try {"
            "    var s=window.__chatgptMirror||{};"
            "    out.hashSize=s.hashByKey&&typeof s.hashByKey.size==='number'?s.hashByKey.size:0;"
            "    out.prunedCache=s.prunedNodeCache&&typeof s.prunedNodeCache.size==='number'?s.prunedNodeCache.size:0;"
            "    out.placeholders=s.placeholderByKey&&typeof s.placeholderByKey.size==='number'?s.placeholderByKey.size:0;"
            "    out.keepDom=s.keepDom||0;"
            "  } catch(e){ out.hashSize=0; out.prunedCache=0; out.placeholders=0; out.keepDom=0; }"
            "  try { out.url=String(location.href||''); } catch(e){ out.url=''; }"
            "  try { return JSON.stringify(out); } catch(e) { return ''; }"
            "})();"
        )

        def _cb(obj) -> None:
            payload = None
            if isinstance(obj, dict):
                payload = obj
            elif isinstance(obj, str):
                s = obj.strip()
                if s:
                    try:
                        parsed = json.loads(s)
                        if isinstance(parsed, dict):
                            payload = parsed
                    except Exception:
                        payload = None
            if not isinstance(payload, dict):
                print(f"[js-mem] callback type={type(obj).__name__} value={repr(obj)[:180]}", flush=True)
                return
            used = int(payload.get("used") or 0) // (1024 * 1024)
            total = int(payload.get("total") or 0) // (1024 * 1024)
            limit = int(payload.get("limit") or 0) // (1024 * 1024)
            dom_nodes = int(payload.get("domNodes") or 0)
            hash_size = int(payload.get("hashSize") or 0)
            pruned = int(payload.get("prunedCache") or 0)
            placeholders = int(payload.get("placeholders") or 0)
            keep_dom = int(payload.get("keepDom") or 0)
            url = str(payload.get("url") or "")
            print(
                "[js-mem] "
                f"tab={self.tab_id} heap={used}/{total}MB limit={limit}MB "
                f"dom={dom_nodes} hash={hash_size} pruned={pruned} "
                f"ph={placeholders} keepDom={keep_dom} url={url}"
            , flush=True)

        try:
            self.web_view.page().runJavaScript(js, _cb)
        except Exception:
            pass

    @Slot(str)
    def on_delta_received(self, json_string: str) -> None:
        """Apply JS extractor deltas and schedule a debounced offline snapshot save."""
        t0 = time.perf_counter()
        try:
            t_parse0 = time.perf_counter()
            payload = json.loads(json_string)
            parse_ms = int((time.perf_counter() - t_parse0) * 1000)
        except json.JSONDecodeError:
            perf_log("dom_delta parse_error")
            return
        if not isinstance(payload, list):
            perf_log("dom_delta ignored_non_list")
            return
        self._enqueue_dom_deltas(payload)
        total_ms = int((time.perf_counter() - t0) * 1000)
        perf_log(
            f"dom_delta size={len(payload)} parse_ms={parse_ms} "
            f"queued={len(self._dom_delta_queue_order)} total_ms={total_ms}"
        )
        if not self._dom_delta_timer.isActive():
            self._dom_delta_timer.start(0)

    def _enqueue_dom_deltas(self, payload: List[dict]) -> None:
        """Coalesce live deltas by key and preserve first-seen order."""
        if not isinstance(payload, list):
            return
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            if key not in self._dom_delta_queue_map:
                self._dom_delta_queue_order.append(key)
            # Keep only the latest state for each key to avoid redundant work.
            self._dom_delta_queue_map[key] = item
        if payload:
            self._dom_delta_autoscroll_pending = True

    def _process_dom_delta_chunk(self) -> None:
        if not self._dom_delta_queue_order:
            if self._dom_delta_autoscroll_pending and self._auto_scroll_enabled and self.model.rowCount() > 0:
                QTimer.singleShot(0, self._scroll_to_bottom)
            self._dom_delta_autoscroll_pending = False
            return

        chunk_size = max(4, int(self._dom_delta_chunk_size))
        take = min(chunk_size, len(self._dom_delta_queue_order))
        keys = self._dom_delta_queue_order[:take]
        self._dom_delta_queue_order = self._dom_delta_queue_order[take:]
        chunk: List[dict] = []
        for key in keys:
            item = self._dom_delta_queue_map.pop(key, None)
            if isinstance(item, dict):
                chunk.append(item)

        if not chunk:
            if self._dom_delta_queue_order:
                self._dom_delta_timer.start(1)
            return

        t_apply0 = time.perf_counter()
        self.model.apply_deltas(chunk)
        apply_ms = int((time.perf_counter() - t_apply0) * 1000)
        perf_log(
            f"dom_delta_apply chunk={len(chunk)} remain={len(self._dom_delta_queue_order)} "
            f"apply_ms={apply_ms} chunk_size={self._dom_delta_chunk_size}"
        )

        # Adaptive chunk size: target short UI steps.
        if apply_ms > 80 and self._dom_delta_chunk_size > 4:
            self._dom_delta_chunk_size = max(4, self._dom_delta_chunk_size // 2)
            perf_log(f"dom_delta_apply adapt chunk_down={self._dom_delta_chunk_size}")
        elif apply_ms < 20 and self._dom_delta_chunk_size < 24:
            self._dom_delta_chunk_size = min(24, self._dom_delta_chunk_size + 2)
            perf_log(f"dom_delta_apply adapt chunk_up={self._dom_delta_chunk_size}")

        self._schedule_persist_offline_snapshot()
        if self._dom_delta_queue_order:
            self._dom_delta_timer.start(2)
            return
        if self._dom_delta_autoscroll_pending and self._auto_scroll_enabled and self.model.rowCount() > 0:
            QTimer.singleShot(0, self._scroll_to_bottom)
        self._dom_delta_autoscroll_pending = False

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
        # Avoid creating snapshot files for empty/generic tabs (fresh "new tab").
        title_norm = (title or "").strip().lower()
        tab_title_norm = (tab_title or "").strip().lower()
        is_generic_title = (
            (not tab_title_norm or tab_title_norm in {"chat", "chatgpt", "chatgpt.com", "new tab", "nuovo tab"})
            and (not title_norm or title_norm in {"chatgpt", "chatgpt.com", "new tab", "nuovo tab"})
        )
        try:
            messages = self.model.messages_in_order()
            if is_generic_title and not messages:
                return
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

    def restore_offline_snapshot_async(
        self,
        snapshot: dict,
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        done_cb: Optional[Callable[[], None]] = None,
        chunk_size: int = 80,
    ) -> None:
        """Restore a snapshot incrementally to keep UI responsive on long chats."""
        if self._restore_in_progress:
            perf_log(f"restore_async tab={self.tab_id} skipped_already_in_progress")
            return
        if not isinstance(snapshot, dict):
            if done_cb:
                done_cb()
            return
        payload = snapshot.get("messages")
        if not isinstance(payload, list) or not payload:
            self._snapshot_restored = True
            if done_cb:
                done_cb()
            return
        perf_log(f"restore_async tab={self.tab_id} start total_msgs={len(payload)} chunk={chunk_size}")
        self._restore_pending_messages = payload
        self._restore_total_messages = len(payload)
        self._restore_progress_cb = progress_cb
        self._restore_done_cb = done_cb
        self._restore_chunk_size = max(8, int(chunk_size))
        self._snapshot_restored = False
        self._restore_in_progress = True
        if self._restore_progress_cb:
            try:
                self._restore_progress_cb(0, self._restore_total_messages)
            except Exception:
                pass
        self._restore_timer.start(0)

    def _restore_snapshot_async_step(self) -> None:
        t0 = time.perf_counter()
        pending = self._restore_pending_messages
        if not pending:
            self._snapshot_restored = True
            self._restore_total_messages = 0
            self._restore_in_progress = False
            cb = self._restore_done_cb
            self._restore_done_cb = None
            self._restore_progress_cb = None
            try:
                self.left_pane._refresh_indices_from(0)
            except Exception:
                pass
            if cb:
                cb()
            perf_log(f"restore_async tab={self.tab_id} done total_ms={int((time.perf_counter()-t0)*1000)}")
            return
        current_chunk_size = max(8, int(self._restore_chunk_size))
        chunk = pending[:current_chunk_size]
        self._restore_pending_messages = pending[current_chunk_size:]
        t_apply0 = time.perf_counter()
        try:
            self.model.apply_deltas(chunk)
            for item in chunk:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not key or not item.get("collapsed"):
                    continue
                row = self.model.row_for_key(key)
                msg = self.model.message_at_row(row) if row >= 0 else None
                if msg:
                    msg.collapsed = True
        except Exception:
            pass
        apply_ms = int((time.perf_counter() - t_apply0) * 1000)
        # Adaptive chunking: keep each UI-thread step short to avoid visible freezes.
        if apply_ms > 120 and self._restore_chunk_size > 8:
            self._restore_chunk_size = max(8, self._restore_chunk_size // 2)
            perf_log(f"restore_async tab={self.tab_id} adapt chunk_down={self._restore_chunk_size} apply_ms={apply_ms}")
        elif apply_ms < 35 and self._restore_chunk_size < 24:
            self._restore_chunk_size = min(24, self._restore_chunk_size + 2)
            perf_log(f"restore_async tab={self.tab_id} adapt chunk_up={self._restore_chunk_size} apply_ms={apply_ms}")
        total = max(1, int(self._restore_total_messages or len(pending)))
        done = max(0, total - len(self._restore_pending_messages))
        perf_log(
            f"restore_async tab={self.tab_id} step done={done}/{total} "
            f"chunk={len(chunk)} apply_ms={apply_ms}"
        )
        if self._restore_progress_cb:
            try:
                self._restore_progress_cb(done, total)
            except Exception:
                pass
        # Small delay yields the GUI thread between chunks (tab switching feels smoother).
        self._restore_timer.start(max(1, int(self._restore_tick_delay_ms)))

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
                page_state = self._offline_store.load_tab_page_state(self.tab_id, self.storage_db_file)
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
        if evt.get("type") == "ui_ready":
            host = self._tabs_host
            if host is not None and hasattr(host, "_on_pane_ui_ready"):
                try:
                    host._on_pane_ui_ready(self, evt)  # type: ignore[attr-defined]
                except Exception:
                    pass
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

    def _on_background_tabs_policy_changed(self, policy: str) -> None:
        host = self._tabs_host
        if host is None or not hasattr(host, "set_background_tabs_policy"):
            return
        try:
            host.set_background_tabs_policy(policy)  # type: ignore[attr-defined]
        except Exception:
            pass
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
    def __init__(
        self,
        startup_progress_cb: Optional[Callable[[int, int, str], None]] = None,
        mem_accurate: bool = False,
        js_mem_log: bool = False,
    ) -> None:
        super().__init__()
        self._startup_progress_cb = startup_progress_cb
        self._mem_accurate_requested = bool(mem_accurate)
        self._js_mem_log_enabled = bool(js_mem_log)
        self._background_tabs_policy = "frozen"
        self._mem_use_pss = False
        self._mem_proc = None
        self._mem_label: Optional[QLabel] = None
        self._mem_timer = QTimer(self)
        self._mem_timer.setSingleShot(False)
        self._mem_timer.timeout.connect(self._update_memory_status)
        self._last_mem_breakdown_log_at = 0.0
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
        self.tabs.currentChanged.connect(self._on_current_tab_changed)
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
        self._init_memory_monitor()

        self._emit_startup_progress(1, 5, "Initializing profile...")
        self._restore_tabs_from_disk()
        if self.tabs.count() == 0:
            self.create_mirror_tab(url="https://chatgpt.com", switch=True)
        self._apply_tab_lifecycle_states()
        self._emit_startup_progress(5, 5, "Ready")

    def _init_memory_monitor(self) -> None:
        """Create a permanent status-bar widget showing native/web memory usage."""
        global PSUTIL_AVAILABLE, PSUTIL_IMPORT_ERROR, psutil
        if not PSUTIL_AVAILABLE:
            # Retry import at runtime in case environment changed after module import.
            try:
                import importlib

                psutil = importlib.import_module("psutil")  # type: ignore[assignment]
                PSUTIL_AVAILABLE = True
                PSUTIL_IMPORT_ERROR = ""
            except Exception as exc:
                PSUTIL_IMPORT_ERROR = str(exc) or "psutil unavailable"
        if not PSUTIL_AVAILABLE:
            self._mem_label = QLabel("Native: N/A | Web: N/A | Total: N/A")
            self._mem_label.setToolTip(f"psutil not available: {PSUTIL_IMPORT_ERROR}")
            self.statusBar().addPermanentWidget(self._mem_label)
            return
        try:
            self._mem_proc = psutil.Process(os.getpid())  # type: ignore[union-attr]
        except Exception:
            self._mem_proc = None
        self._mem_use_pss = bool(self._mem_accurate_requested and sys.platform.startswith("linux"))
        mode = "PSS" if self._mem_use_pss else "RSS"
        self._mem_label = QLabel("Native: -- MB | Web: -- MB | Total: -- MB")
        self._mem_label.setToolTip(f"Memory monitor mode: {mode}")
        self.statusBar().addPermanentWidget(self._mem_label)
        self._update_memory_status()
        self._mem_timer.start(1000)

    def _pss_bytes_for_pid(self, pid: int) -> Optional[int]:
        """Best-effort Linux PSS reader from /proc/<pid>/smaps_rollup."""
        if not sys.platform.startswith("linux"):
            return None
        try:
            path = Path(f"/proc/{int(pid)}/smaps_rollup")
            txt = path.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"^Pss:\s+(\d+)\s+kB$", txt, flags=re.MULTILINE)
            if not m:
                return None
            return int(m.group(1)) * 1024
        except Exception:
            return None

    def _format_mb(self, value_bytes: int) -> str:
        return f"{(max(0, int(value_bytes)) / (1024.0 * 1024.0)):.0f}"

    def _update_memory_status(self) -> None:
        label = self._mem_label
        proc = self._mem_proc
        if label is None or proc is None or not PSUTIL_AVAILABLE:
            return
        try:
            children = proc.children(recursive=True)
        except Exception:
            children = []

        web_children = []
        for child in children:
            try:
                name = (child.name() or "").lower()
            except Exception:
                name = ""
            cmdline = ""
            if not name:
                try:
                    cmdline = " ".join(child.cmdline()).lower()
                except Exception:
                    cmdline = ""
            if "qtwebengineprocess" in name or "qtwebengineprocess" in cmdline:
                web_children.append(child)

        native_bytes = 0
        web_bytes = 0
        mode = "RSS"

        if self._mem_use_pss:
            native_pss = self._pss_bytes_for_pid(proc.pid)
            web_pss_vals = [self._pss_bytes_for_pid(ch.pid) for ch in web_children]
            if native_pss is not None and all(v is not None for v in web_pss_vals):
                native_bytes = int(native_pss)
                web_bytes = int(sum(int(v or 0) for v in web_pss_vals))
                mode = "PSS"
            else:
                self._mem_use_pss = False

        if mode != "PSS":
            try:
                native_bytes = int(proc.memory_info().rss)
            except Exception:
                native_bytes = 0
            total_web = 0
            for child in web_children:
                try:
                    total_web += int(child.memory_info().rss)
                except Exception:
                    pass
            web_bytes = total_web
            mode = "RSS"

        total_bytes = native_bytes + web_bytes
        label.setText(
            f"Native: {self._format_mb(native_bytes)} MB | "
            f"Web: {self._format_mb(web_bytes)} MB | "
            f"Total: {self._format_mb(total_bytes)} MB"
        )
        label.setToolTip(f"Memory monitor mode: {mode}")
        if self._js_mem_log_enabled:
            print(
                f"[mem] mode={mode} "
                f"Native: {self._format_mb(native_bytes)} MB | "
                f"Web: {self._format_mb(web_bytes)} MB | "
                f"Total: {self._format_mb(total_bytes)} MB",
                flush=True,
            )
            now = time.time()
            if now - self._last_mem_breakdown_log_at >= 5.0:
                self._last_mem_breakdown_log_at = now
                self._log_web_process_breakdown(web_children, mode)

    def _web_process_kind(self, child) -> str:
        """Classify a QtWebEngine child process by Chromium --type."""
        try:
            cmd = " ".join(child.cmdline())
        except Exception:
            cmd = ""
        m = re.search(r"--type=([a-zA-Z0-9_-]+)", cmd)
        if m:
            return m.group(1).lower()
        return "unknown"

    def background_tabs_policy(self) -> str:
        return self._background_tabs_policy

    def set_background_tabs_policy(self, policy: str) -> None:
        policy = (policy or "frozen").strip().lower()
        if policy not in {"active", "frozen", "discarded"}:
            policy = "frozen"
        if policy == self._background_tabs_policy:
            return
        self._background_tabs_policy = policy
        for i in range(self.tabs.count()):
            pane = self.tabs.widget(i)
            if isinstance(pane, MainWindow):
                try:
                    pane.left_pane.set_background_tabs_policy(policy)
                except Exception:
                    pass
        self._apply_tab_lifecycle_states()
        self.schedule_manifest_save()

    def _log_web_process_breakdown(self, web_children: List[object], mode: str) -> None:
        """Emit per-process-type memory usage to explain WebEngine growth."""
        if not web_children:
            return
        buckets: Dict[str, Dict[str, int]] = {}
        for child in web_children:
            kind = self._web_process_kind(child)
            try:
                pid = int(child.pid)
            except Exception:
                pid = 0
            if mode == "PSS":
                b = self._pss_bytes_for_pid(pid) if pid else None
                if b is None:
                    try:
                        b = int(child.memory_info().rss)
                    except Exception:
                        b = 0
            else:
                try:
                    b = int(child.memory_info().rss)
                except Exception:
                    b = 0
            slot = buckets.setdefault(kind, {"count": 0, "bytes": 0})
            slot["count"] += 1
            slot["bytes"] += int(b or 0)
        parts = []
        for kind, data in sorted(buckets.items(), key=lambda kv: kv[1]["bytes"], reverse=True):
            mb = self._format_mb(int(data["bytes"]))
            parts.append(f"{kind}:{data['count']}p/{mb}MB")
        if parts:
            print(f"[mem-web] mode={mode} " + " | ".join(parts), flush=True)

    def _apply_tab_lifecycle_states(self) -> None:
        """Keep background WebEngine tabs frozen to limit renderer memory growth."""
        enum = getattr(QWebEnginePage, "LifecycleState", None)
        if enum is None:
            return
        active_state = getattr(enum, "Active", None)
        frozen_state = getattr(enum, "Frozen", None)
        discarded_state = getattr(enum, "Discarded", None)
        if active_state is None or frozen_state is None:
            return
        current = self.tabs.currentIndex()
        policy = (self._background_tabs_policy or "frozen").strip().lower()
        for i in range(self.tabs.count()):
            pane = self.tabs.widget(i)
            if not isinstance(pane, MainWindow):
                continue
            page = pane.web_view.page()
            if page is None or not hasattr(page, "setLifecycleState"):
                continue
            if i == current or policy == "active":
                target = active_state
                target_name = "Active"
            elif policy == "discarded" and discarded_state is not None:
                target = discarded_state
                target_name = "Discarded"
            else:
                target = frozen_state
                target_name = "Frozen"
            try:
                cur_state = page.lifecycleState() if hasattr(page, "lifecycleState") else None
                if cur_state != target:
                    page.setLifecycleState(target)
                    if self._js_mem_log_enabled:
                        print(
                            f"[lifecycle] tab={getattr(pane, 'tab_id', '?')} idx={i} "
                            f"policy={policy} state={target_name}",
                            flush=True,
                        )
            except Exception:
                continue

    def _emit_startup_progress(self, current: int, total: int, message: str) -> None:
        cb = self._startup_progress_cb
        if cb is None:
            return
        try:
            cb(int(current), int(total), str(message or ""))
        except Exception:
            pass

    def create_mirror_tab(
        self,
        url: Optional[str] = "https://chatgpt.com",
        switch: bool = True,
        tab_id: Optional[str] = None,
        storage_db_file: Optional[str] = None,
        initial_snapshot: Optional[dict] = None,
        defer_web_load: bool = False,
        restore_snapshot_on_create: bool = True,
        title_hint: Optional[str] = None,
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
            defer_web_load=defer_web_load,
            restore_snapshot_on_create=restore_snapshot_on_create,
            js_mem_log=self._js_mem_log_enabled,
        )
        idx = self.tabs.addTab(pane, "Nuovo tab")
        saved_tab_title = ""
        if isinstance(initial_snapshot, dict):
            page_state = initial_snapshot.get("page_state")
            if isinstance(page_state, dict):
                saved_tab_title = str(page_state.get("tab_title") or "").strip()
        if not saved_tab_title:
            saved_tab_title = (title_hint or "").strip()
        if saved_tab_title:
            shown = saved_tab_title if len(saved_tab_title) <= 28 else (saved_tab_title[:27].rstrip() + "…")
            self.tabs.setTabText(idx, shown)
            self.tabs.setTabToolTip(idx, saved_tab_title)
        try:
            pane.web_view.titleChanged.connect(lambda title, p=pane: self._on_pane_title_changed(p, title))
            pane.web_view.urlChanged.connect(lambda _u, p=pane: self._update_tab_title_for_pane(p, pane.web_view.title()))
            pane.web_view.urlChanged.connect(lambda _u: self.schedule_manifest_save())
            pane.web_view.loadStarted.connect(lambda p=pane: self._on_pane_load_started(p))
            pane.web_view.loadFinished.connect(lambda ok, p=pane: self._on_pane_load_finished(p, bool(ok)))
        except Exception:
            pass
        if not saved_tab_title:
            self._update_tab_title_for_pane(pane, pane.web_view.title())
        if switch:
            self.tabs.setCurrentIndex(idx)
            pane.ensure_activated()
        self._apply_tab_lifecycle_states()
        self.schedule_manifest_save()
        return pane

    def _on_current_tab_changed(self, _index: int) -> None:
        t0 = time.perf_counter()
        perf_log(f"tab_changed idx={self.tabs.currentIndex()} count={self.tabs.count()} start")
        pane = self._materialize_current_tab_if_deferred()
        if isinstance(pane, MainWindow):
            if not pane._snapshot_restored:
                self._restore_current_tab_with_progress()
            else:
                pane.ensure_activated()
        self._apply_tab_lifecycle_states()
        self.schedule_manifest_save()
        perf_log(f"tab_changed idx={self.tabs.currentIndex()} end elapsed_ms={int((time.perf_counter()-t0)*1000)}")

    def _tab_title_from_manifest_item(self, item: dict) -> str:
        title_hint = str(item.get("title") or "").strip()
        if not title_hint:
            db_file = str(item.get("db_file") or "").strip()
            if db_file:
                title_hint = Path(db_file).stem
        return title_hint or "ChatGPT"

    def _add_deferred_tab_from_manifest_item(self, item: dict, switch: bool = False) -> Optional[DeferredTabPlaceholder]:
        if not isinstance(item, dict):
            return None
        tab_id = str(item.get("tab_id") or "").strip()
        if not tab_id:
            return None
        db_file = str(item.get("db_file") or "").strip()
        url = str(item.get("url") or "https://chatgpt.com").strip() or "https://chatgpt.com"
        title_hint = self._tab_title_from_manifest_item(item)
        placeholder = DeferredTabPlaceholder(tab_id=tab_id, url=url, db_file=db_file, title=title_hint)
        shown = title_hint if len(title_hint) <= 28 else (title_hint[:27].rstrip() + "…")
        idx = self.tabs.addTab(placeholder, shown)
        self.tabs.setTabToolTip(idx, title_hint)
        if switch:
            self.tabs.setCurrentIndex(idx)
        return placeholder

    def _materialize_deferred_tab(self, index: int) -> Optional[MainWindow]:
        t0 = time.perf_counter()
        if index < 0 or index >= self.tabs.count():
            return None
        widget = self.tabs.widget(index)
        if isinstance(widget, MainWindow):
            return widget
        if not isinstance(widget, DeferredTabPlaceholder):
            return None

        pane = MainWindow(
            tabs_host=self,
            shared_profile=self.web_profile,
            profile_root=self._profile_root,
            offline_store=self._offline_store,
            tab_id=widget.tab_id,
            storage_db_file=widget.db_file or None,
            initial_snapshot=None,
            initial_url=widget.url or "https://chatgpt.com",
            defer_web_load=True,
            restore_snapshot_on_create=False,
            js_mem_log=self._js_mem_log_enabled,
        )
        shown = (widget.title or "ChatGPT")
        shown = shown if len(shown) <= 28 else (shown[:27].rstrip() + "…")
        tooltip = widget.title or shown

        was_current = self.tabs.currentIndex() == index
        self.tabs.removeTab(index)
        self.tabs.insertTab(index, pane, shown)
        self.tabs.setTabToolTip(index, tooltip)
        # Preserve the selected tab while replacing placeholder -> real pane.
        # Without this, Qt may temporarily move selection to index 0.
        if was_current:
            self.tabs.setCurrentIndex(index)
        try:
            pane.web_view.titleChanged.connect(lambda title, p=pane: self._on_pane_title_changed(p, title))
            pane.web_view.urlChanged.connect(lambda _u, p=pane: self._update_tab_title_for_pane(p, pane.web_view.title()))
            pane.web_view.urlChanged.connect(lambda _u: self.schedule_manifest_save())
            pane.web_view.loadStarted.connect(lambda p=pane: self._on_pane_load_started(p))
            pane.web_view.loadFinished.connect(lambda ok, p=pane: self._on_pane_load_finished(p, bool(ok)))
        except Exception:
            pass
        perf_log(
            f"materialize_tab idx={index} tab_id={widget.tab_id} "
            f"db={widget.db_file or '-'} elapsed_ms={int((time.perf_counter()-t0)*1000)}"
        )
        return pane

    def _materialize_current_tab_if_deferred(self) -> Optional[MainWindow]:
        idx = self.tabs.currentIndex()
        if idx < 0:
            return None
        return self._materialize_deferred_tab(idx)

    def _pane_tab_index(self, pane: MainWindow) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is pane:
                return i
        return -1

    def _sanitize_tab_title_for_storage(self, title: str) -> str:
        t = (title or "").strip()
        if not t:
            return ""
        # Remove transient progress suffix from restore/hydration UI.
        t = re.sub(r"\s+\(\d{1,3}%\)$", "", t).strip()
        # Remove visual truncation marker used in tab text.
        t = t.rstrip("…").strip()
        low = t.lower()
        if low in {"chatgpt", "chatgpt.com", "new tab", "nuovo tab"}:
            return ""
        if low.startswith("chatgpt.com"):
            return ""
        return t

    def _is_generic_chatgpt_title(self, title: str) -> bool:
        t = (title or "").strip().lower()
        return t.startswith("chatgpt.com")

    def _is_meaningful_page_title(self, title: str) -> bool:
        t = (title or "").strip()
        if not t:
            return False
        if self._is_generic_chatgpt_title(t):
            return False
        if t.lower() in {"chatgpt", "nuovo tab", "new tab"}:
            return False
        return True

    def _on_pane_load_started(self, pane: MainWindow) -> None:
        pane._tab_title_lock_seq = int(getattr(pane, "_tab_title_lock_seq", 0)) + 1
        pane._tab_title_locked = True

    def _on_pane_title_changed(self, pane: MainWindow, title: str) -> None:
        if getattr(pane, "_tab_title_locked", False):
            if self._is_meaningful_page_title(title):
                pane._tab_title_locked = False
                self._update_tab_title_for_pane(pane, title)
                self.schedule_manifest_save()
            return
        self._update_tab_title_for_pane(pane, title)

    def _on_pane_load_finished(self, pane: MainWindow, ok: bool) -> None:
        # Title lock is intentionally NOT released on loadFinished.
        # For ChatGPT SPA pages, loadFinished can happen before redirects/hydration settle.
        # We unlock only when JS emits `ui_ready` (composer detected).
        return

    def _on_pane_ui_ready(self, pane: MainWindow, evt: dict) -> None:
        """Unlock tab title when JS confirms chat UI/composer is actually ready."""
        if not isinstance(evt, dict):
            return
        title = str(evt.get("title") or pane.web_view.title() or "").strip()
        pane._tab_title_locked = False
        # If title is still generic, keep current tab text; otherwise apply it now.
        if self._is_meaningful_page_title(title):
            self._update_tab_title_for_pane(pane, title)
        self.schedule_manifest_save()
        perf_log(
            f"ui_ready tab={pane.tab_id} "
            f"title={'yes' if self._is_meaningful_page_title(title) else 'generic'}"
        )

    def tab_display_title_for_pane(self, pane: MainWindow) -> str:
        idx = self._pane_tab_index(pane)
        if idx < 0:
            return ""
        try:
            # Prefer tooltip (usually full title), fallback to visible tab text.
            tooltip = self.tabs.tabToolTip(idx) or ""
            cleaned = self._sanitize_tab_title_for_storage(tooltip)
            if cleaned:
                return cleaned
            return self._sanitize_tab_title_for_storage(self.tabs.tabText(idx) or "")
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
            if isinstance(pane, DeferredTabPlaceholder) and (pane.db_file or "") == db_file_name:
                self.tabs.setCurrentIndex(i)
                return True
        # Do not load the whole snapshot synchronously here: for large files it can freeze.
        # Load only page metadata and let normal async restore path show per-tab progress.
        try:
            page_state = self._offline_store.load_tab_page_state(uuid.uuid4().hex[:8], db_file_name)
        except Exception:
            page_state = None
        if not isinstance(page_state, dict):
            return False
        url = str(page_state.get("url") or "https://chatgpt.com")
        pane = self.create_mirror_tab(
            url=url,
            switch=True,
            tab_id=uuid.uuid4().hex[:12],
            storage_db_file=db_file_name,
            initial_snapshot=None,
            restore_snapshot_on_create=False,
            title_hint=str(page_state.get("tab_title") or page_state.get("title") or "").strip(),
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
        if getattr(pane, "_tab_title_locked", False):
            return
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
        self._apply_tab_lifecycle_states()
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
            if isinstance(pane, MainWindow):
                try:
                    url = pane.web_view.url().toString() or pane.offline_snapshot_url_guess()
                except Exception:
                    url = ""
                tabs_payload.append(
                    {
                        "tab_id": pane.tab_id,
                        "url": url,
                        "db_file": pane.storage_db_file or "",
                        "title": self._sanitize_tab_title_for_storage(self.tabs.tabToolTip(i) or self.tabs.tabText(i) or ""),
                    }
                )
            elif isinstance(pane, DeferredTabPlaceholder):
                tabs_payload.append(
                    {
                        "tab_id": pane.tab_id,
                        "url": pane.url,
                        "db_file": pane.db_file,
                        "title": self._sanitize_tab_title_for_storage(
                            pane.title or self.tabs.tabToolTip(i) or self.tabs.tabText(i) or ""
                        ),
                    }
                )
        try:
            self._offline_store.save_manifest(
                {
                    "current_index": max(0, self.tabs.currentIndex()),
                    "background_tabs_policy": self._background_tabs_policy,
                    "tabs": tabs_payload,
                }
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
        loaded_policy = str(manifest.get("background_tabs_policy") or "frozen").strip().lower()
        if loaded_policy in {"active", "frozen", "discarded"}:
            self._background_tabs_policy = loaded_policy
        tabs = manifest.get("tabs") or []
        try:
            current_idx = int(manifest.get("current_index") or 0)
        except Exception:
            current_idx = 0
        if tabs:
            current_idx = max(0, min(current_idx, len(tabs) - 1))
        restored = 0
        for item in tabs:
            placeholder = self._add_deferred_tab_from_manifest_item(item, switch=False)
            if placeholder is not None:
                restored += 1
        if restored:
            idx = max(0, min(current_idx, self.tabs.count() - 1))
            self.tabs.setCurrentIndex(idx)
            QTimer.singleShot(0, lambda: self._on_current_tab_changed(self.tabs.currentIndex()))

    def _restore_current_tab_with_progress(self) -> None:
        pane = self._materialize_current_tab_if_deferred()
        if not isinstance(pane, MainWindow):
            return
        if pane._snapshot_restored or pane._restore_in_progress:
            pane.ensure_activated()
            return
        if not pane.storage_db_file:
            pane.ensure_activated()
            return
        try:
            t_load0 = time.perf_counter()
            snapshot = self._offline_store.load_tab_snapshot(
                pane.tab_id,
                pane.storage_db_file,
                preload_images=True,
                preload_image_limit=24,
            )
            perf_log(
                f"load_snapshot tab={pane.tab_id} db={pane.storage_db_file or '-'} "
                f"elapsed_ms={int((time.perf_counter()-t_load0)*1000)} "
                f"msgs={(len(snapshot.get('messages', [])) if isinstance(snapshot, dict) else 0)}"
            )
        except Exception:
            snapshot = None
            perf_log(f"load_snapshot tab={pane.tab_id} failed")
        if not isinstance(snapshot, dict):
            pane.ensure_activated()
            return

        idx = self._pane_tab_index(pane)
        base_text = self.tabs.tabText(idx) if idx >= 0 else "Tab"
        if not base_text:
            base_text = "Tab"
        base_text = re.sub(r"\s+\(\d{1,3}%\)$", "", base_text).strip() or "Tab"
        self.statusBar().showMessage("Hydrating selected tab viewport... 0%")

        def _progress(done: int, total: int) -> None:
            # Show what the user perceives: hydrated visible rows, not raw DB/model load.
            h_done, h_total = pane.left_pane.viewport_hydration_progress()
            pct = int(round((max(0, h_done) / max(1, h_total)) * 100)) if h_total > 0 else 0
            if idx >= 0:
                self.tabs.setTabText(idx, f"{base_text} ({pct}%)")
            self.statusBar().showMessage(
                f"Hydrating selected tab viewport... {pct}% (rows {h_done}/{max(1, h_total)})"
            )

        def _done() -> None:
            h_done, h_total = pane.left_pane.viewport_hydration_progress()
            pct = int(round((max(0, h_done) / max(1, h_total)) * 100)) if h_total > 0 else 100
            if idx >= 0:
                self.tabs.setTabText(idx, f"{base_text} ({pct}%)")
            self.statusBar().showMessage(
                f"Hydrating selected tab viewport... {pct}% (rows {h_done}/{max(1, h_total)})"
            )
            try:
                pane.left_pane.trigger_viewport_hydration()
            except Exception:
                pass
            # Final pass once viewport hydration has settled.
            def _finish_ui() -> None:
                h_done2, h_total2 = pane.left_pane.viewport_hydration_progress()
                pct2 = int(round((max(0, h_done2) / max(1, h_total2)) * 100)) if h_total2 > 0 else 100
                if idx >= 0:
                    self.tabs.setTabText(idx, base_text if pct2 >= 100 else f"{base_text} ({pct2}%)")
                if pct2 >= 100:
                    self.statusBar().clearMessage()
            QTimer.singleShot(220, _finish_ui)
            pane.ensure_activated()

        pane.restore_offline_snapshot_async(snapshot, progress_cb=_progress, done_cb=_done, chunk_size=12)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._persist_all_tabs_now()
            self._save_manifest_now()
        except Exception:
            pass
        super().closeEvent(event)


def main() -> int:
    """Application entry point."""
    global PERF_LOG_ENABLED
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--perf-log",
        action="store_true",
        help="Enable performance logs for tab switch/restore debugging.",
    )
    parser.add_argument(
        "--mem-accurate",
        action="store_true",
        help="Use Linux PSS (if available) for memory monitor; otherwise automatic RSS fallback.",
    )
    parser.add_argument(
        "--js-mem-log",
        action="store_true",
        help="Print periodic JS/WebView memory diagnostics to stdout.",
    )
    args, qt_args = parser.parse_known_args(sys.argv[1:])
    PERF_LOG_ENABLED = bool(args.perf_log or os.environ.get("CGM_PERF_LOG", "").strip() == "1")
    mem_accurate = bool(args.mem_accurate or os.environ.get("CGM_MEM_ACCURATE", "").strip() == "1")
    js_mem_log = bool(args.js_mem_log or os.environ.get("CGM_JS_MEM_LOG", "").strip() == "1")
    if js_mem_log:
        print("[js-mem] enabled", flush=True)

    app = QApplication([sys.argv[0], *qt_args])
    perf_log("app_start")
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

    window = TabbedMainWindow(mem_accurate=mem_accurate, js_mem_log=js_mem_log)
    window.show()
    perf_log("main_window_shown")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
