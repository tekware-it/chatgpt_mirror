"""Native viewer widgets and rendering helpers for the left pane.

This module contains Qt widgets only (text/code/image parts, message rows, and the
message list pane). Keeping these classes separate makes the application window logic
much easier to maintain.
"""

from __future__ import annotations

import os
import re
from html import escape as html_escape
from typing import Dict, Optional

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPixmap
from PySide6.QtGui import QAction, QActionGroup, QTextCharFormat, QTextDocument, QSyntaxHighlighter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtCore import QUrl

from mirror_models import Message, MessageListModel, MessagePart
from mirror_storage import IMAGE_BYTES_CACHE


def _env_bool(name: str, default: bool = True) -> bool:
    """Parse a boolean environment variable with a safe default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default

def monospace_font() -> QFont:
    """Return the monospace font used for native code rendering."""
    font = QFont("DejaVu Sans Mono")
    font.setStyleHint(QFont.Monospace)
    return font


def normalize_code_lang(lang: str) -> str:
    """Normalize language aliases extracted from ChatGPT code block headers."""
    v = (lang or "").strip().lower()
    aliases = {
        "py": "python",
        "python3": "python",
        "shell": "bash",
        "sh": "bash",
        "zsh": "bash",
        "shellscript": "bash",
        "js": "javascript",
        "ts": "typescript",
        "yml": "yaml",
    }
    return aliases.get(v, v)


class SimpleCodeHighlighter(QSyntaxHighlighter):
    """Lightweight syntax highlighter for the native code widgets."""
    def __init__(self, document, lang: str) -> None:
        super().__init__(document)
        self.lang = normalize_code_lang(lang)
        self._rules = []
        self._comment_patterns = []
        self._build_rules()

    def _fmt(self, color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.Bold)
        if italic:
            f.setFontItalic(True)
        return f

    def _add_keywords(self, keywords, fmt: QTextCharFormat) -> None:
        for kw in keywords:
            self._rules.append((re.compile(rf"\b{re.escape(kw)}\b"), fmt))

    def _build_rules(self) -> None:
        kw_fmt = self._fmt("#93c5fd", bold=True)
        str_fmt = self._fmt("#86efac")
        num_fmt = self._fmt("#fca5a5")
        cmt_fmt = self._fmt("#94a3b8", italic=True)
        fn_fmt = self._fmt("#fcd34d")
        op_fmt = self._fmt("#c4b5fd")

        common_string_patterns = [
            re.compile(r'"(?:\\.|[^"\\])*"'),
            re.compile(r"'(?:\\.|[^'\\])*'"),
        ]
        for p in common_string_patterns:
            self._rules.append((p, str_fmt))

        self._rules.append((re.compile(r"\b\d+(\.\d+)?\b"), num_fmt))

        if self.lang in {"python"}:
            self._add_keywords(
                [
                    "def", "class", "import", "from", "as", "return", "if", "elif", "else",
                    "for", "while", "try", "except", "finally", "with", "async", "await",
                    "pass", "break", "continue", "raise", "in", "is", "and", "or", "not",
                    "None", "True", "False",
                ],
                kw_fmt,
            )
            self._rules.append((re.compile(r"\bdef\s+([A-Za-z_]\w*)"), fn_fmt))
            self._rules.append((re.compile(r"\bclass\s+([A-Za-z_]\w*)"), fn_fmt))
            self._comment_patterns = [re.compile(r"#.*$")]
        elif self.lang in {"bash"}:
            self._add_keywords(
                [
                    "if", "then", "else", "fi", "for", "in", "do", "done", "case", "esac",
                    "while", "function", "local", "export", "return", "exit",
                ],
                kw_fmt,
            )
            self._rules.append((re.compile(r"\$[A-Za-z_]\w*"), op_fmt))
            self._rules.append((re.compile(r"\$\{[^}]+\}"), op_fmt))
            self._comment_patterns = [re.compile(r"#.*$")]
        elif self.lang in {"javascript", "typescript"}:
            self._add_keywords(
                [
                    "function", "return", "const", "let", "var", "if", "else", "for", "while",
                    "switch", "case", "break", "continue", "try", "catch", "finally", "class",
                    "extends", "new", "import", "from", "export", "default", "async", "await",
                    "true", "false", "null", "undefined",
                ],
                kw_fmt,
            )
            self._comment_patterns = [re.compile(r"//.*$")]
        elif self.lang in {"json"}:
            self._rules.append((re.compile(r'"[^"]+"\s*:'), self._fmt("#7dd3fc")))
            self._add_keywords(["true", "false", "null"], kw_fmt)
        elif self.lang in {"yaml"}:
            self._rules.append((re.compile(r"^[ \t-]*[A-Za-z0-9_.-]+\s*:"), self._fmt("#7dd3fc")))
            self._comment_patterns = [re.compile(r"#.*$")]
        else:
            # Generic fallback
            self._comment_patterns = [re.compile(r"#.*$"), re.compile(r"//.*$")]

        # Operators/punctuation (light touch)
        self._rules.append((re.compile(r"[-=+*/<>!|&]+"), op_fmt))

        self._keyword_fmt = kw_fmt
        self._comment_fmt = cmt_fmt

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)
        for pattern in self._comment_patterns:
            m = pattern.search(text)
            if m:
                self.setFormat(m.start(), len(text) - m.start(), self._comment_fmt)


def markdown_to_html(markdown_text: str) -> str:
    """Render markdown to HTML using Qt so QLabel can display rich text consistently."""
    doc = QTextDocument()
    app_font = QApplication.font() if QApplication.instance() else QFont("Sans Serif", 10)
    doc.setDefaultFont(app_font)
    # Keep typography consistent across labels while preserving heading hierarchy.
    doc.setDefaultStyleSheet(
        """
        body { color: #111827; font-size: 13px; line-height: 1.35; }
        p { margin: 0 0 8px 0; }
        h1 { font-size: 22px; margin: 10px 0 8px 0; font-weight: 700; }
        h2 { font-size: 19px; margin: 10px 0 7px 0; font-weight: 700; }
        h3 { font-size: 17px; margin: 8px 0 6px 0; font-weight: 700; }
        h4 { font-size: 15px; margin: 8px 0 6px 0; font-weight: 700; }
        h5 { font-size: 14px; margin: 6px 0 4px 0; font-weight: 700; }
        h6 { font-size: 13px; margin: 6px 0 4px 0; font-weight: 700; }
        ul, ol { margin: 4px 0 8px 20px; }
        li { margin: 2px 0; }
        blockquote { color: #374151; border-left: 3px solid #cbd5e1; margin: 8px 0; padding-left: 10px; }
        a { color: #1d4ed8; text-decoration: none; }
        code { font-family: "DejaVu Sans Mono"; }
        """
    )
    try:
        doc.setMarkdown(markdown_text or "")
    except Exception:
        doc.setPlainText(markdown_text or "")
    return doc.toHtml()


class MarkdownTextWidget(QTextBrowser):
    """Read-only markdown renderer with auto height for message text parts."""

    relayoutRequested = Signal()

    def __init__(self, markdown_text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setStyleSheet(
            """
            QTextBrowser {
                background: transparent;
                border: none;
                color: #111827;
                padding: 0;
                margin: 0;
            }
            """
        )

        doc = self.document()
        app_font = QApplication.font() if QApplication.instance() else QFont("Sans Serif", 10)
        doc.setDefaultFont(app_font)
        doc.setDefaultStyleSheet(
            """
            body { color: #111827; font-size: 13px; line-height: 1.35; }
            p { margin: 0 0 8px 0; }
            h1 { font-size: 22px; margin: 10px 0 8px 0; font-weight: 700; }
            h2 { font-size: 19px; margin: 10px 0 7px 0; font-weight: 700; }
            h3 { font-size: 17px; margin: 8px 0 6px 0; font-weight: 700; }
            h4 { font-size: 15px; margin: 8px 0 6px 0; font-weight: 700; }
            h5 { font-size: 14px; margin: 6px 0 4px 0; font-weight: 700; }
            h6 { font-size: 13px; margin: 6px 0 4px 0; font-weight: 700; }
            ul, ol { margin: 4px 0 8px 22px; }
            li { margin: 2px 0; }
            blockquote { color: #374151; border-left: 3px solid #cbd5e1; margin: 8px 0; padding-left: 10px; }
            a { color: #1d4ed8; text-decoration: none; }
            code { font-family: "DejaVu Sans Mono"; }
            """
        )
        try:
            self.setMarkdown(markdown_text or "")
        except Exception:
            self.setPlainText(markdown_text or "")
        self._sync_height()
        doc.contentsChanged.connect(self._on_contents_changed)

    def _on_contents_changed(self) -> None:
        self._sync_height()
        self.relayoutRequested.emit()

    def _sync_height(self) -> None:
        self.document().setTextWidth(max(100, self.viewport().width()))
        doc_h = self.document().size().height()
        self.setMinimumHeight(int(doc_h) + 6)
        self.setMaximumHeight(int(doc_h) + 12)
        try:
            self.verticalScrollBar().setValue(0)
            self.horizontalScrollBar().setValue(0)
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_height()

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        # Text parts must not scroll internally; let the outer message list consume the wheel.
        try:
            self.verticalScrollBar().setValue(0)
            self.horizontalScrollBar().setValue(0)
        except Exception:
            pass
        event.ignore()


class CodePlainTextEdit(QPlainTextEdit):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._lock_vertical_wheel = False

    def set_vertical_wheel_locked(self, locked: bool) -> None:
        self._lock_vertical_wheel = bool(locked)
        if self._lock_vertical_wheel:
            try:
                self.verticalScrollBar().setValue(0)
            except Exception:
                pass

    def wheelEvent(self, event):  # type: ignore[override]
        if self._lock_vertical_wheel:
            try:
                self.verticalScrollBar().setValue(0)
            except Exception:
                pass
            event.ignore()
            return
        super().wheelEvent(event)


class ImagePartWidget(QWidget):
    """Native renderer for image parts with cached/offline thumbnail loading."""
    copyRequested = Signal(str)
    relayoutRequested = Signal()

    _net_mgr: Optional[QNetworkAccessManager] = None
    _use_firefox_headers: bool = True

    def __init__(
        self,
        image_url: str,
        alt: str = "",
        image_kind: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.image_url = (image_url or "").strip()
        self.alt = (alt or "").strip()
        self.image_kind = (image_kind or "").strip().lower()
        self._pixmap: Optional[QPixmap] = None
        self._reply = None
        self._show_badge = True
        self._show_copy_url_btn = True
        self._show_preview = True
        if self.image_kind == "rich-entity":
            self._show_copy_url_btn = _env_bool("CGM_RICH_ENTITY_SHOW_COPY_IMAGE_URL", True)
            self._show_badge = _env_bool("CGM_RICH_ENTITY_SHOW_IMAGE_BADGE", True)
            self._show_preview = _env_bool("CGM_RICH_ENTITY_SHOW_IMAGE_PREVIEW", True)
        self._build_ui()
        self._start_load()

    @classmethod
    def _manager(cls) -> QNetworkAccessManager:
        if cls._net_mgr is None:
            cls._net_mgr = QNetworkAccessManager()
        return cls._net_mgr

    @classmethod
    def set_use_firefox_headers(cls, enabled: bool) -> None:
        cls._use_firefox_headers = bool(enabled)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        if self._show_badge:
            chip = QLabel(f'<a href="{html_escape(self.image_url)}">image</a>')
            chip.setOpenExternalLinks(True)
            chip.setTextInteractionFlags(Qt.TextBrowserInteraction)
            chip.setStyleSheet(
                "QLabel { background: #e7edf6; color: #2d3748; padding: 2px 8px; border-radius: 9px; font-size: 11px; }"
                "QLabel a { color: #2d3748; text-decoration: none; }"
            )
            top.addWidget(chip)
        top.addStretch(1)
        if self._show_copy_url_btn:
            copy_btn = QPushButton("Copy image URL")
            copy_btn.setCursor(Qt.PointingHandCursor)
            copy_btn.clicked.connect(lambda: self.copyRequested.emit(self.image_url))
            top.addWidget(copy_btn)
        outer.addLayout(top)

        self.preview = QLabel(self.alt or "Image")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(80)
        self.preview.setMaximumHeight(220)
        self.preview.setStyleSheet(
            "QLabel { background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; color: #475569; }"
        )
        self.preview.setWordWrap(True)
        if self._show_preview:
            outer.addWidget(self.preview)

        self.url_label: Optional[QLabel] = None

    def _start_load(self) -> None:
        if not self.image_url:
            return
        cached = IMAGE_BYTES_CACHE.get(self.image_url)
        if cached:
            pix = QPixmap()
            if pix.loadFromData(cached):
                self._pixmap = pix
                self._render_pixmap()
                QTimer.singleShot(0, self.relayoutRequested.emit)
                return
        try:
            req = QNetworkRequest(QUrl(self.image_url))
            if self._use_firefox_headers:
                req.setRawHeader(
                    b"User-Agent",
                    b"Mozilla/5.0 (X11; Linux x86_64; rv:145.0) Gecko/20100101 Firefox/145.0",
                )
                req.setRawHeader(
                    b"Accept",
                    b"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                )
                req.setRawHeader(b"Accept-Language", b"it-IT,it;q=0.8,en-US;q=0.5,en;q=0.3")
                req.setRawHeader(b"Sec-GPC", b"1")
                req.setRawHeader(b"Upgrade-Insecure-Requests", b"1")
                req.setRawHeader(b"Sec-Fetch-Dest", b"document")
                req.setRawHeader(b"Sec-Fetch-Mode", b"navigate")
                req.setRawHeader(b"Sec-Fetch-Site", b"none")
                req.setRawHeader(b"Sec-Fetch-User", b"?1")
                req.setRawHeader(b"Priority", b"u=0, i")
            self._reply = self._manager().get(req)
            self._reply.finished.connect(self._on_reply_finished)
        except Exception:
            self._reply = None

    def _on_reply_finished(self) -> None:
        reply = self._reply
        self._reply = None
        if reply is None:
            return
        try:
            data = bytes(reply.readAll())
        except Exception:
            data = b""
        try:
            reply.deleteLater()
        except Exception:
            pass
        if not data:
            return
        if self.image_url:
            try:
                IMAGE_BYTES_CACHE[self.image_url] = data
            except Exception:
                pass
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        self._pixmap = pix
        self._render_pixmap()
        self.relayoutRequested.emit()

    def _render_pixmap(self) -> None:
        if self._pixmap is None:
            return
        if not self._show_preview:
            return
        target_w = max(120, min(320, self.width() - 4))
        scaled = self._pixmap.scaledToWidth(target_w, Qt.SmoothTransformation)
        if scaled.height() > 220:
            scaled = self._pixmap.scaled(target_w, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview.setPixmap(scaled)
        self.preview.setMinimumHeight(max(80, scaled.height() + 8))
        self.preview.setMaximumHeight(max(90, scaled.height() + 8))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._pixmap is not None:
            self._render_pixmap()

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()


class CodeFullTextWidget(QTextBrowser):
    relayoutRequested = Signal()

    def __init__(self, code: str, lang: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._code = code or ""
        self._lang = normalize_code_lang(lang or "")
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setLineWrapMode(QTextBrowser.WidgetWidth)
        self.setStyleSheet(
            """
            QTextBrowser {
                background: #0f172a;
                color: #e5e7eb;
                border: 1px solid #cbd5e1;
                border-radius: 8px;
                padding: 8px;
                margin: 0;
            }
            """
        )
        doc = self.document()
        doc.setDefaultFont(monospace_font())
        doc.setDocumentMargin(0)
        self.setPlainText(self._code)
        self._highlighter = SimpleCodeHighlighter(doc, self._lang)
        self._sync_height()
        doc.contentsChanged.connect(self._on_contents_changed)

    def _on_contents_changed(self) -> None:
        self._sync_height()
        self.relayoutRequested.emit()

    def _sync_height(self) -> None:
        self.document().setTextWidth(max(100, self.viewport().width()))
        doc_h = self.document().size().height()
        # Extra bottom slack avoids clipping the descenders/last line in some Qt layouts.
        height = int(doc_h) + 20
        self.setMinimumHeight(max(60, height))
        self.setMaximumHeight(max(60, height))
        try:
            self.verticalScrollBar().setValue(0)
            self.horizontalScrollBar().setValue(0)
        except Exception:
            pass

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_height()

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        try:
            self.verticalScrollBar().setValue(0)
            self.horizontalScrollBar().setValue(0)
        except Exception:
            pass
        event.ignore()


class CodeBlockWidget(QWidget):
    """Native renderer for code blocks with collapse/expand/full modes."""
    copyRequested = Signal(str)
    relayoutRequested = Signal()

    def __init__(self, code: str, lang: str, display_mode: str = "auto", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.code = code
        self.lang = lang
        self._display_mode = (display_mode or "auto").strip().lower()
        self._editor: Optional[QPlainTextEdit] = None
        self._full_view: Optional[CodeFullTextWidget] = None
        self._toggle_btn: Optional[QPushButton] = None
        self._total_btn: Optional[QPushButton] = None
        self._is_long_block = False
        self._collapsed = True
        self._full_override = False
        self._collapsed_lines = 8
        self._expanded_lines_cap = 24
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        norm_lang = normalize_code_lang(self.lang or "")
        lang_label = QLabel(norm_lang or "code")
        lang_label.setStyleSheet(
            "QLabel { background: #e7edf6; color: #2d3748; padding: 2px 8px; "
            "border-radius: 9px; font-size: 11px; }"
        )
        header.addWidget(lang_label)
        header.addStretch(1)

        copy_btn = QPushButton("Copy code")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.clicked.connect(lambda: self.copyRequested.emit(self.code))
        header.addWidget(copy_btn)

        total_lines = max(1, self.code.count("\n") + 1)
        self._is_long_block = total_lines > self._collapsed_lines
        if self._is_long_block:
            self._toggle_btn = QPushButton("Expand")
            self._toggle_btn.setCursor(Qt.PointingHandCursor)
            self._toggle_btn.clicked.connect(self._toggle_collapsed)
            header.addWidget(self._toggle_btn)
            self._total_btn = QPushButton("Total expand")
            self._total_btn.setCursor(Qt.PointingHandCursor)
            self._total_btn.clicked.connect(self._toggle_total_expand)
            header.addWidget(self._total_btn)
        self._apply_display_mode_defaults()

        outer.addLayout(header)

        editor = CodePlainTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText(self.code)
        editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        editor.setFont(monospace_font())
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        editor.setFrameShape(QFrame.NoFrame)
        editor.setStyleSheet(
            "QPlainTextEdit { background: #0f172a; color: #e5e7eb; "
            "border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px; }"
        )
        # Lightweight syntax highlighting based on extracted language label.
        self._highlighter = SimpleCodeHighlighter(editor.document(), norm_lang)
        self._editor = editor
        outer.addWidget(editor)

        full_view = CodeFullTextWidget(self.code, self.lang)
        full_view.relayoutRequested.connect(self.relayoutRequested.emit)
        self._full_view = full_view
        outer.addWidget(full_view)

        self._apply_editor_height()

    def _apply_display_mode_defaults(self) -> None:
        mode = self._display_mode
        self._full_override = False
        if mode == "full":
            self._collapsed = False
        elif mode == "expanded":
            self._collapsed = False
        else:
            self._collapsed = self._is_long_block
        if self._toggle_btn is not None:
            self._toggle_btn.setVisible(mode != "full" and self._is_long_block)
            self._toggle_btn.setText("Expand" if self._collapsed else "Collapse")
        if self._total_btn is not None:
            self._total_btn.setVisible(mode != "full" and self._is_long_block)
            self._total_btn.setText("Total expand")

    def set_display_mode(self, mode: str) -> None:
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "expanded", "full"}:
            mode = "auto"
        if mode == self._display_mode:
            return
        self._display_mode = mode
        self._apply_display_mode_defaults()
        self._apply_editor_height()
        QTimer.singleShot(0, self.relayoutRequested.emit)

    def _toggle_collapsed(self) -> None:
        if not self._is_long_block:
            return
        if self._display_mode == "full" or self._full_override:
            return
        self._collapsed = not self._collapsed
        if self._toggle_btn is not None:
            self._toggle_btn.setText("Expand" if self._collapsed else "Collapse")
        self._apply_editor_height()
        QTimer.singleShot(0, self.relayoutRequested.emit)

    def _toggle_total_expand(self) -> None:
        if not self._is_long_block or self._display_mode == "full":
            return
        self._full_override = not self._full_override
        if self._total_btn is not None:
            self._total_btn.setText("Exit total" if self._full_override else "Total expand")
        if self._toggle_btn is not None:
            self._toggle_btn.setEnabled(not self._full_override)
        self._apply_editor_height()
        QTimer.singleShot(0, self.relayoutRequested.emit)

    def _apply_editor_height(self) -> None:
        if self._editor is None:
            return
        editor = self._editor
        if self._full_view is not None:
            use_full = (self._display_mode == "full") or self._full_override
            self._full_view.setVisible(use_full)
            editor.setVisible(not use_full)
            if use_full:
                self._full_view._sync_height()
                return
        total_lines = max(1, self.code.count("\n") + 1)
        if self._collapsed and self._is_long_block:
            visible_lines = self._collapsed_lines
            editor.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            editor.set_vertical_wheel_locked(False)
        else:
            visible_lines = min(total_lines, self._expanded_lines_cap)
            editor.setVerticalScrollBarPolicy(
                Qt.ScrollBarAsNeeded if total_lines > self._expanded_lines_cap else Qt.ScrollBarAlwaysOff
            )
            editor.set_vertical_wheel_locked(False)
        fm = editor.fontMetrics()
        height = max(60, (fm.lineSpacing() * visible_lines) + 24)
        editor.setMinimumHeight(height)
        editor.setMaximumHeight(height)
class MessageRowWidget(QFrame):
    """Row widget used by the virtualized native mirror list."""
    relayoutRequested = Signal(str, QSize)

    def __init__(
        self,
        key: str,
        copy_message_cb,
        toggle_collapse_cb,
        copy_code_cb,
        code_block_display_mode: str = "auto",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self._copy_message_cb = copy_message_cb
        self._toggle_collapse_cb = toggle_collapse_cb
        self._copy_code_cb = copy_code_cb
        self._code_block_display_mode = (code_block_display_mode or "auto").strip().lower()
        self._show_rich_entity_images = True
        self._show_gallery_images = True
        self._message: Optional[Message] = None
        self._index_display = 0
        self._last_emitted_size_hint: Optional[QSize] = None
        self._build_ui()

    def _build_ui(self) -> None:
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #ffffff; border: 1px solid #dbe2ea; border-radius: 10px; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(10, 8, 10, 10)
        self.outer.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)

        self.role_badge = QLabel("Assistant")
        self.role_badge.setStyleSheet(
            "QLabel { background: #e8f0ff; color: #1e3a8a; padding: 3px 8px; "
            "border-radius: 9px; font-weight: 600; }"
        )
        top.addWidget(self.role_badge)

        self.index_label = QLabel("#0")
        self.index_label.setStyleSheet("QLabel { color: #4b5563; font-size: 12px; }")
        top.addWidget(self.index_label)
        top.addStretch(1)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(self._on_copy_message)
        top.addWidget(self.copy_btn)

        self.collapse_btn = QPushButton("Collapse")
        self.collapse_btn.setCursor(Qt.PointingHandCursor)
        self.collapse_btn.clicked.connect(self._on_toggle_collapse)
        top.addWidget(self.collapse_btn)

        self.outer.addLayout(top)

        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview_label.setStyleSheet("QLabel { color: #111827; }")
        self.outer.addWidget(self.preview_label)

        self.expanded_container = QWidget()
        self.expanded_layout = QVBoxLayout(self.expanded_container)
        self.expanded_layout.setContentsMargins(0, 0, 0, 0)
        self.expanded_layout.setSpacing(8)
        self.outer.addWidget(self.expanded_container)

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                self._clear_layout(child_layout)  # type: ignore[arg-type]

    def _on_copy_message(self) -> None:
        self._copy_message_cb(self.key)

    def _on_toggle_collapse(self) -> None:
        self._toggle_collapse_cb(self.key)

    def set_message(self, message: Message, display_index: int) -> None:
        self._message = message
        self._index_display = display_index
        self._render()

    def set_code_block_display_mode(self, mode: str) -> None:
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "expanded", "full"}:
            mode = "auto"
        if mode == self._code_block_display_mode:
            return
        self._code_block_display_mode = mode
        if self._message is not None:
            self._render()

    def set_image_visibility(self, rich_entity_enabled: bool, gallery_enabled: bool) -> None:
        rich_entity_enabled = bool(rich_entity_enabled)
        gallery_enabled = bool(gallery_enabled)
        if (
            rich_entity_enabled == self._show_rich_entity_images
            and gallery_enabled == self._show_gallery_images
        ):
            return
        self._show_rich_entity_images = rich_entity_enabled
        self._show_gallery_images = gallery_enabled
        if self._message is not None:
            self._render()

    def _is_image_part_visible(self, part: MessagePart) -> bool:
        kind = (part.image_kind or "").strip().lower()
        if kind == "gallery":
            return self._show_gallery_images
        if kind == "rich-entity":
            return self._show_rich_entity_images
        return self._show_gallery_images or self._show_rich_entity_images

    def _render(self) -> None:
        if self._message is None:
            return

        msg = self._message
        is_user = msg.role == "user"

        self.role_badge.setText(msg.role_label())
        self.role_badge.setStyleSheet(
            "QLabel { background: %s; color: %s; padding: 3px 8px; border-radius: 9px; font-weight: 600; }"
            % (("#ecfdf5", "#065f46") if is_user else ("#e8f0ff", "#1e3a8a"))
        )
        self.index_label.setText(f"#{self._index_display}")
        self.preview_label.setText(msg.preview_text(120))
        self.collapse_btn.setText("Expand" if msg.collapsed else "Collapse")

        self.preview_label.setVisible(msg.collapsed)
        self.expanded_container.setVisible(not msg.collapsed)

        self._clear_layout(self.expanded_layout)
        if not msg.collapsed:
            for part in msg.parts:
                if part.type == "text":
                    text_widget = MarkdownTextWidget(part.text)
                    text_widget.relayoutRequested.connect(self._schedule_relayout)
                    self.expanded_layout.addWidget(text_widget)
                elif part.type == "code":
                    code_widget = CodeBlockWidget(part.code, part.lang, self._code_block_display_mode)
                    code_widget.copyRequested.connect(self._copy_code_cb)
                    code_widget.relayoutRequested.connect(self._schedule_relayout)
                    self.expanded_layout.addWidget(code_widget)
                elif part.type == "image" and self._is_image_part_visible(part):
                    img_widget = ImagePartWidget(part.image_url, part.alt, part.image_kind)
                    img_widget.copyRequested.connect(self._copy_code_cb)
                    img_widget.relayoutRequested.connect(self._schedule_relayout)
                    self.expanded_layout.addWidget(img_widget)
            self.expanded_layout.addStretch(0)

        self._schedule_relayout()

    def _schedule_relayout(self) -> None:
        # First pass catches immediate layout; second pass catches QTextDocument settling.
        QTimer.singleShot(0, self._emit_size_hint)
        QTimer.singleShot(30, self._emit_size_hint)

    def _emit_size_hint(self) -> None:
        self.updateGeometry()
        self.adjustSize()
        size = self.sizeHint()
        if self._last_emitted_size_hint == size:
            return
        self._last_emitted_size_hint = QSize(size)
        self.relayoutRequested.emit(self.key, size)


class MessageListPane(QWidget):
    """Left pane container: header + settings + native message list."""
    autoScrollChanged = Signal(bool)
    webToNativeSyncChanged = Signal(bool)
    nativeToWebSyncChanged = Signal(bool)
    keepDomChanged = Signal(int)
    restorePrunedOnViewChanged = Signal(bool)
    scrollSyncDebugChanged = Signal(bool)
    nativeImageFirefoxHeadersChanged = Signal(bool)
    browserLanguageChanged = Signal(str)
    resetSessionRequested = Signal()
    exportRequested = Signal(str)
    exportDebugVisibleRequested = Signal()
    exportPdfImagesDebugRequested = Signal()
    richEntityImagesVisibleChanged = Signal(bool)
    galleryImagesVisibleChanged = Signal(bool)
    aboutRequested = Signal()

    def __init__(self, model: MessageListModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self._widgets_by_key: Dict[str, MessageRowWidget] = {}
        self._auto_scroll_enabled = True
        self._web_to_native_sync_enabled = True
        self._native_to_web_sync_enabled = True
        self._keep_dom_count = 30
        self._restore_pruned_on_view = False
        self._scroll_sync_debug_enabled = False
        self._use_firefox_headers_for_native_img = True
        self._code_block_display_mode = "auto"
        self._browser_language_mode = "system"
        self._show_rich_entity_images = True
        self._show_gallery_images = True
        self._build_ui()
        self._connect_model()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)

        self.settings_btn = QToolButton()
        self.settings_btn.setText("...")
        self.settings_btn.setPopupMode(QToolButton.InstantPopup)
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setToolTip("Settings")
        self.settings_btn.setStyleSheet(
            "QToolButton { background: #f8fafc; border: 1px solid #cbd5e1; "
            "border-radius: 8px; padding: 2px 8px; font-weight: 700; }"
            "QToolButton:hover { background: #eef2f7; }"
        )

        settings_menu = QMenu(self.settings_btn)

        browser_lang_menu = settings_menu.addMenu("Browser Language")
        self.browser_lang_group = QActionGroup(self)
        self.browser_lang_group.setExclusive(True)

        lang_system = QAction("System", self, checkable=True)
        lang_system.setChecked(self._browser_language_mode == "system")
        lang_system.triggered.connect(lambda checked=False: self.browserLanguageChanged.emit("system"))
        self.browser_lang_group.addAction(lang_system)
        browser_lang_menu.addAction(lang_system)

        lang_en = QAction("English", self, checkable=True)
        lang_en.setChecked(self._browser_language_mode == "en")
        lang_en.triggered.connect(lambda checked=False: self.browserLanguageChanged.emit("en"))
        self.browser_lang_group.addAction(lang_en)
        browser_lang_menu.addAction(lang_en)

        export_menu = settings_menu.addMenu("Export Conversation")
        export_md = QAction("Markdown (.md)", self)
        export_md.triggered.connect(lambda: self.exportRequested.emit("md"))
        export_menu.addAction(export_md)
        export_json = QAction("JSON (.json)", self)
        export_json.triggered.connect(lambda: self.exportRequested.emit("json"))
        export_menu.addAction(export_json)
        export_pdf = QAction("PDF (.pdf)", self)
        export_pdf.triggered.connect(lambda: self.exportRequested.emit("pdf"))
        export_menu.addAction(export_pdf)

        code_block_menu = settings_menu.addMenu("Code Blocks (Native)")
        self.code_block_mode_group = QActionGroup(self)
        self.code_block_mode_group.setExclusive(True)
        code_mode_specs = [
            ("Auto (collapse long blocks)", "auto"),
            ("Expanded", "expanded"),
            ("Full expansion", "full"),
        ]
        for label, mode in code_mode_specs:
            action = QAction(label, self, checkable=True)
            action.setChecked(mode == self._code_block_display_mode)
            action.triggered.connect(lambda checked=False, m=mode: self._set_code_block_display_mode(m))
            self.code_block_mode_group.addAction(action)
            code_block_menu.addAction(action)

        scroll_menu = settings_menu.addMenu("Scroll")
        self.auto_scroll_action = QAction("Auto-scroll new messages", self, checkable=True)
        self.auto_scroll_action.setChecked(self._auto_scroll_enabled)
        self.auto_scroll_action.toggled.connect(self.autoScrollChanged.emit)
        scroll_menu.addAction(self.auto_scroll_action)

        self.web_to_native_action = QAction("Sync Web -> Native", self, checkable=True)
        self.web_to_native_action.setChecked(self._web_to_native_sync_enabled)
        self.web_to_native_action.toggled.connect(self.webToNativeSyncChanged.emit)
        scroll_menu.addAction(self.web_to_native_action)

        self.native_to_web_action = QAction("Sync Native -> Web", self, checkable=True)
        self.native_to_web_action.setChecked(self._native_to_web_sync_enabled)
        self.native_to_web_action.toggled.connect(self.nativeToWebSyncChanged.emit)
        scroll_menu.addAction(self.native_to_web_action)

        rich_entity_images_action = QAction("Show rich-entity images (Native + export)", self, checkable=True)
        rich_entity_images_action.setChecked(self._show_rich_entity_images)
        rich_entity_images_action.toggled.connect(self._set_rich_entity_images_visible)
        settings_menu.addAction(rich_entity_images_action)

        gallery_images_action = QAction("Show gallery images (Native + export)", self, checkable=True)
        gallery_images_action.setChecked(self._show_gallery_images)
        gallery_images_action.toggled.connect(self._set_gallery_images_visible)
        settings_menu.addAction(gallery_images_action)

        reset_action = QAction("Reset Session", self)
        reset_action.triggered.connect(self.resetSessionRequested.emit)
        settings_menu.addAction(reset_action)

        settings_menu.addSeparator()

        advanced_menu = settings_menu.addMenu("Advanced")

        keep_dom_menu = advanced_menu.addMenu("KEEP_DOM (WebView)")
        self.keep_dom_group = QActionGroup(self)
        self.keep_dom_group.setExclusive(True)
        for count in (30, 80, 150):
            action = QAction(str(count), self, checkable=True)
            action.setChecked(count == self._keep_dom_count)
            action.triggered.connect(lambda checked=False, c=count: self.keepDomChanged.emit(c))
            self.keep_dom_group.addAction(action)
            keep_dom_menu.addAction(action)

        restore_pruned_action = QAction("Allow pruned DOM restore on double-click", self, checkable=True)
        restore_pruned_action.setChecked(self._restore_pruned_on_view)
        restore_pruned_action.toggled.connect(self.restorePrunedOnViewChanged.emit)
        advanced_menu.addAction(restore_pruned_action)

        scroll_sync_debug_action = QAction("Debug scroll sync (log)", self, checkable=True)
        scroll_sync_debug_action.setChecked(self._scroll_sync_debug_enabled)
        scroll_sync_debug_action.toggled.connect(self.scrollSyncDebugChanged.emit)
        advanced_menu.addAction(scroll_sync_debug_action)

        firefox_headers_action = QAction("Use Firefox headers for native img", self, checkable=True)
        firefox_headers_action.setChecked(self._use_firefox_headers_for_native_img)
        firefox_headers_action.toggled.connect(self._set_native_image_firefox_headers)
        advanced_menu.addAction(firefox_headers_action)

        debug_action = QAction("Debug visible block (.txt)", self)
        debug_action.triggered.connect(self.exportDebugVisibleRequested.emit)
        advanced_menu.addAction(debug_action)

        pdf_img_debug_action = QAction("Debug PDF images (.txt)", self)
        pdf_img_debug_action.triggered.connect(self.exportPdfImagesDebugRequested.emit)
        advanced_menu.addAction(pdf_img_debug_action)

        settings_menu.addSeparator()
        about_action = QAction("About", self)
        about_action.triggered.connect(self.aboutRequested.emit)
        settings_menu.addAction(about_action)

        self.settings_btn.setMenu(settings_menu)
        header_row.addWidget(self.settings_btn, 0, Qt.AlignVCenter)

        self.header = QLabel("Native Mirror")
        self.header.setStyleSheet("QLabel { font-size: 14px; font-weight: 700; color: #111827; }")
        header_row.addWidget(self.header, 0, Qt.AlignVCenter)
        header_row.addStretch(1)
        layout.addLayout(header_row)

        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setSelectionMode(QListView.NoSelection)
        self.list_view.setEditTriggers(QListView.NoEditTriggers)
        self.list_view.setUniformItemSizes(False)
        self.list_view.setSpacing(8)
        self.list_view.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_view.setStyleSheet(
            "QListView { background: #f5f7fb; border: 1px solid #dbe2ea; border-radius: 10px; padding: 6px; }"
        )
        self.list_view.viewport().installEventFilter(self)
        layout.addWidget(self.list_view, 1)

    def _connect_model(self) -> None:
        self.model.rowsInserted.connect(self._on_rows_inserted)
        self.model.dataChanged.connect(self._on_data_changed)

    def _on_rows_inserted(self, parent: QModelIndex, first: int, last: int) -> None:
        for row in range(first, last + 1):
            self._ensure_row_widget(row)
        self._refresh_indices_from(first)

    def _on_data_changed(self, top_left: QModelIndex, bottom_right: QModelIndex, roles) -> None:
        if roles and all(role == Qt.SizeHintRole for role in roles):
            self.list_view.doItemsLayout()
            return
        for row in range(top_left.row(), bottom_right.row() + 1):
            self._refresh_row_widget(row)

    def _refresh_indices_from(self, first_row: int = 0) -> None:
        for row in range(first_row, self.model.rowCount()):
            msg = self.model.message_at_row(row)
            if not msg:
                continue
            widget = self._widgets_by_key.get(msg.key)
            if widget:
                widget.set_message(msg, row + 1)

    def _refresh_row_widget(self, row: int) -> None:
        msg = self.model.message_at_row(row)
        if not msg:
            return
        widget = self._widgets_by_key.get(msg.key)
        if widget is None:
            self._ensure_row_widget(row)
            widget = self._widgets_by_key.get(msg.key)
        if widget:
            widget.set_message(msg, row + 1)

    def _ensure_row_widget(self, row: int) -> None:
        msg = self.model.message_at_row(row)
        if not msg:
            return
        if msg.key in self._widgets_by_key:
            self._refresh_row_widget(row)
            return

        widget = MessageRowWidget(
            key=msg.key,
            copy_message_cb=self.copy_message,
            toggle_collapse_cb=self.model.toggle_collapsed,
            copy_code_cb=self.copy_code,
            code_block_display_mode=self._code_block_display_mode,
        )
        widget.set_image_visibility(self._show_rich_entity_images, self._show_gallery_images)
        widget.relayoutRequested.connect(self._on_row_relayout_requested)
        self._widgets_by_key[msg.key] = widget

        index = self.model.index(row, 0)
        self.list_view.setIndexWidget(index, widget)
        self._apply_row_width(widget)
        widget.set_message(msg, row + 1)

    def _set_code_block_display_mode(self, mode: str) -> None:
        mode = (mode or "auto").strip().lower()
        if mode not in {"auto", "expanded", "full"}:
            mode = "auto"
        if mode == self._code_block_display_mode:
            return
        self._code_block_display_mode = mode
        for row in range(self.model.rowCount()):
            msg = self.model.message_at_row(row)
            if not msg:
                continue
            widget = self._widgets_by_key.get(msg.key)
            if widget is not None:
                widget.set_code_block_display_mode(mode)

    def _apply_image_visibility_to_rows(self) -> None:
        for row in range(self.model.rowCount()):
            msg = self.model.message_at_row(row)
            if not msg:
                continue
            widget = self._widgets_by_key.get(msg.key)
            if widget is not None:
                widget.set_image_visibility(self._show_rich_entity_images, self._show_gallery_images)

    def _set_rich_entity_images_visible(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._show_rich_entity_images:
            return
        self._show_rich_entity_images = enabled
        self._apply_image_visibility_to_rows()
        self.richEntityImagesVisibleChanged.emit(enabled)

    def _set_gallery_images_visible(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._show_gallery_images:
            return
        self._show_gallery_images = enabled
        self._apply_image_visibility_to_rows()
        self.galleryImagesVisibleChanged.emit(enabled)

    def show_rich_entity_images_enabled(self) -> bool:
        return self._show_rich_entity_images

    def show_gallery_images_enabled(self) -> bool:
        return self._show_gallery_images

    def _set_native_image_firefox_headers(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._use_firefox_headers_for_native_img:
            return
        self._use_firefox_headers_for_native_img = enabled
        self.nativeImageFirefoxHeadersChanged.emit(enabled)

    @Slot(str, QSize)
    def _on_row_relayout_requested(self, key: str, size_hint: QSize) -> None:
        widget = self._widgets_by_key.get(key)
        if widget:
            self._apply_row_width(widget)
        # Keep rows full-width in the list; only the height should track content size.
        viewport_w = max(360, self.list_view.viewport().width() - 12)
        padded = QSize(viewport_w, max(70, size_hint.height() + 8))
        self.model.update_size_hint(key, padded)
        self.list_view.doItemsLayout()

    def _apply_row_width(self, widget: MessageRowWidget) -> None:
        target_w = max(360, self.list_view.viewport().width() - 14)
        if widget.width() != target_w:
            widget.setFixedWidth(target_w)

    def _apply_widths_to_all_rows(self) -> None:
        for widget in self._widgets_by_key.values():
            self._apply_row_width(widget)

    def eventFilter(self, watched: QObject, event) -> bool:  # type: ignore[override]
        if watched is self.list_view.viewport() and event.type() == QEvent.Resize:
            self._apply_widths_to_all_rows()
            # Width changes can change wrapping, so request a lazy relayout on visible rows.
            QTimer.singleShot(0, self.list_view.doItemsLayout)
        return super().eventFilter(watched, event)

    def copy_message(self, key: str) -> None:
        row = self.model.row_for_key(key)
        if row < 0:
            return
        msg = self.model.message_at_row(row)
        if not msg:
            return
        QGuiApplication.clipboard().setText(msg.as_copy_text())

    def copy_code(self, code: str) -> None:
        QGuiApplication.clipboard().setText(code)

    def top_visible_info(self) -> Optional[tuple[str, float]]:
        """Return the top visible message key plus in-row scroll progress [0..1]."""
        viewport = self.list_view.viewport()
        probe_points = [QPoint(12, 12), QPoint(12, 32), QPoint(12, 60)]
        for p in probe_points:
            idx = self.list_view.indexAt(p)
            if idx.isValid():
                msg = self.model.message_at_row(idx.row())
                if not msg:
                    return None
                rect = self.list_view.visualRect(idx)
                h = max(1, rect.height())
                progress = max(0.0, min(1.0, float(-rect.top()) / float(h)))
                return (msg.key, progress)
        # Fallback: first visible row by geometry scan.
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            rect = self.list_view.visualRect(idx)
            if rect.isValid() and rect.bottom() >= 0:
                msg = self.model.message_at_row(row)
                if not msg:
                    return None
                h = max(1, rect.height())
                progress = max(0.0, min(1.0, float(-rect.top()) / float(h)))
                return (msg.key, progress)
        return None

    def top_visible_key(self) -> Optional[str]:
        info = self.top_visible_info()
        return info[0] if info else None

    def scroll_key_with_progress(self, key: str, progress: float = 0.0) -> bool:
        row = self.model.row_for_key(key)
        if row < 0:
            return False
        idx = self.model.index(row, 0)
        if not idx.isValid():
            return False
        progress = max(0.0, min(1.0, float(progress or 0.0)))
        self.list_view.scrollTo(idx, QListView.PositionAtTop)
        if progress > 0.0:
            sb = self.list_view.verticalScrollBar()
            rect = self.list_view.visualRect(idx)
            row_h = rect.height() if rect.isValid() else 0
            if row_h <= 0:
                msg = self.model.message_at_row(row)
                if msg is not None:
                    row_h = max(1, msg.size_hint.height())
            delta = int(round(progress * max(1, row_h)))
            if delta > 0:
                sb.setValue(sb.value() + delta)
        return True

    def scroll_key_to_top(self, key: str) -> bool:
        return self.scroll_key_with_progress(key, 0.0)
