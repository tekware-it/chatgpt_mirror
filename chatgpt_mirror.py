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
import re
import sys
import tempfile
import time
from html import escape as html_escape
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QAbstractListModel,
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
    QStandardPaths,
)
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPageLayout, QPageSize, QPixmap
from PySide6.QtGui import QAction, QActionGroup, QTextCharFormat, QTextDocument, QSyntaxHighlighter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QMenu,
    QSizePolicy,
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
IMAGE_BYTES_CACHE: Dict[str, bytes] = {}


def ensure_profile_root() -> Path:
    app_data_root = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not app_data_root:
        app_data_root = str(Path.home() / ".local" / "share" / "chatgpt_mirror")
    profile_root = Path(app_data_root)
    profile_root.mkdir(parents=True, exist_ok=True)
    (profile_root / "qtwebengine").mkdir(parents=True, exist_ok=True)
    (profile_root / "qtwebengine-cache").mkdir(parents=True, exist_ok=True)
    return profile_root


JS_INJECTOR = r"""
(function() {
  if (window.__chatgptMirror && window.__chatgptMirror.started) {
    return "chatgpt_mirror_already_started";
  }

  var CONSOLE_DELTA_PREFIX = "__CGM_DELTA__";
  var CONSOLE_EVENT_PREFIX = "__CGM_EVT__";
  var LIST_MARKER_PREFIX = "__CGM_LI__";

  function simpleHash(str) {
    var h = 5381;
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) + h) ^ str.charCodeAt(i);
    }
    return (h >>> 0).toString(16);
  }

  function normalizeWhitespace(text) {
    if (!text) return "";
    text = text
      .replace(/\r\n/g, "\n")
      .replace(/\u00a0/g, " ")
      .replace(/\t/g, "    ");

    var lines = text.split("\n").map(function(line) {
      var raw = String(line || "").replace(/\s+$/g, "");
      if (!raw.trim()) return "";
      var trimmed = raw.trim();

      // Deterministic list item marker emitted by extractor (avoids DOM whitespace chaos).
      if (trimmed.startsWith(LIST_MARKER_PREFIX)) {
        var m = trimmed.match(/^__CGM_LI__(\d+)__(.*)$/);
        if (m) {
          var level = Math.max(0, Math.min(6, parseInt(m[1], 10) || 0));
          var rest = (m[2] || "").trim().replace(/[ ]{2,}/g, " ");
          return "    ".repeat(level) + "- " + rest;
        }
      }

      // ChatGPT sometimes renders bullets as plain text lines ("• ...") with visual indentation.
      var bulletMatch = raw.match(/^(\s*)([•◦▪●])\s+(.*)$/);
      if (bulletMatch) {
        var leadingSpaces = (bulletMatch[1] || "").replace(/\t/g, "    ").length;
        var levelFromIndent = Math.max(0, Math.min(6, Math.floor(leadingSpaces / 2)));
        var bulletRest = String(bulletMatch[3] || "").trim().replace(/[ ]{2,}/g, " ");
        return "    ".repeat(levelFromIndent) + "- " + bulletRest;
      }

      // Preserve user-visible markdown structures but normalize spacing.
      if (/^(>\s+|[-*+]\s+|\d+\.\s+)/.test(trimmed)) {
        return trimmed.replace(/[ ]{2,}/g, " ");
      }

      return trimmed.replace(/[ ]{2,}/g, " ");
    });

    return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  }

  function escapeMarkdownText(text) {
    if (!text) return "";
    return String(text)
      .replace(/\\/g, "\\\\")
      .replace(/([`*_{}\[\]()#+!<>|])/g, "\\$1");
  }

  function stripUiSpeechPrefixes(text) {
    if (!text) return "";
    var lines = text.split('\n');

    // Generic UI label removal: if the first line is short and ends with ":", drop it.
    // Examples: "Hai detto:", "ChatGPT ha detto:", "You said:"
    if (lines.length > 1) {
      var first = (lines[0] || '').trim();
      if (first && first.endsWith(':')) {
        var firstNoColon = first.slice(0, -1).trim();
        var wordCount = firstNoColon ? firstNoColon.split(/\s+/).length : 0;
        if (first.length <= 48 && wordCount <= 6) {
          lines = lines.slice(1);
        }
      }
    }

    var cleaned = lines.map(function(line) {
      return line.replace(
        /^\s*(you|chatgpt|assistant)\s+said:\s*/i,
        ''
      ).replace(
        /^\s*(hai detto|chatgpt ha detto|assistente)\s*:\s*/i,
        ''
      );
    });
    return cleaned.join('\n').trim();
  }

  function isVisibleish(el) {
    if (!el || !(el instanceof Element)) return false;
    var style = window.getComputedStyle(el);
    if (!style) return true;
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return true;
  }

  function hasMessageContent(el) {
    if (!el || !(el instanceof Element)) return false;
    if (!isVisibleish(el)) return false;
    if (el.matches && el.matches('[data-cgm-pruned-placeholder="1"]')) return false;
    var tag = (el.tagName || '').toLowerCase();
    if (tag !== 'article') {
      try {
        if (el.querySelectorAll('article').length > 0) return false;
        if (el.querySelectorAll('[data-message-id]').length > 1) return false;
      } catch (e) {}
    }
    var code = el.querySelector && el.querySelector('pre code');
    if (code) return true;
    var txt = (el.innerText || "").trim();
    return txt.length > 0;
  }

  function domDepth(node) {
    var d = 0;
    while (node && node.parentElement) {
      d++;
      node = node.parentElement;
    }
    return d;
  }

  function collectMessageNodes() {
    var selectors = [
      'article',
      '[data-message-id]',
      '[data-testid*="conversation-turn"]',
      '[data-testid*="conversation"] article',
      '[data-testid*="message"]'
    ];
    var raw = [];
    selectors.forEach(function(sel) {
      try {
        document.querySelectorAll(sel).forEach(function(n) { raw.push(n); });
      } catch (e) {}
    });

    if (!raw.length) {
      document.querySelectorAll('pre code').forEach(function(codeEl) {
        var p = codeEl.closest('article, [data-message-id], [role="group"], div');
        if (p) raw.push(p);
      });
    }

    var seen = new Set();
    var unique = [];
    raw.forEach(function(n) {
      if (!(n instanceof Element)) return;
      if (seen.has(n)) return;
      seen.add(n);
      unique.push(n);
    });

    unique.sort(function(a, b) {
      var pos = a.compareDocumentPosition(b);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return -1;
      if (pos & Node.DOCUMENT_POSITION_PRECEDING) return 1;
      return domDepth(a) - domDepth(b);
    });

    var out = [];
    unique.forEach(function(n) {
      if (!hasMessageContent(n)) return;
      if (out.some(function(p) { return p.contains(n); })) return;
      out = out.filter(function(p) { return !n.contains(p); });
      out.push(n);
    });

    return out;
  }

  function detectRole(node, index) {
    var checks = [];
    var cur = node;
    for (var i = 0; i < 4 && cur; i++, cur = cur.parentElement) {
      checks.push(cur);
    }

    function hay(el) {
      var pieces = [];
      ['data-message-author-role', 'data-turn', 'data-testid', 'aria-label', 'id', 'class'].forEach(function(attr) {
        var v = el.getAttribute && el.getAttribute(attr);
        if (v) pieces.push(v);
      });
      return pieces.join(' ').toLowerCase();
    }

    for (var j = 0; j < checks.length; j++) {
      var txt = hay(checks[j]);
      if (txt.includes('assistant')) return 'assistant';
      if (txt.includes('user')) return 'user';
      if (txt.includes('human')) return 'user';
    }

    var authorNode = node.querySelector('[data-message-author-role]');
    if (authorNode) {
      var authorRole = (authorNode.getAttribute('data-message-author-role') || '').toLowerCase();
      if (authorRole.includes('assistant')) return 'assistant';
      if (authorRole.includes('user') || authorRole.includes('human')) return 'user';
    }

    var ariaCandidates = node.querySelectorAll('[aria-label], [alt]');
    for (var k = 0; k < Math.min(ariaCandidates.length, 10); k++) {
      var t = ((ariaCandidates[k].getAttribute('aria-label') || '') + ' ' +
               (ariaCandidates[k].getAttribute('alt') || '')).toLowerCase();
      if (t.includes('assistant') || t.includes('chatgpt')) return 'assistant';
      if (t.includes('you') || t.includes('user')) return 'user';
    }

    // Assistant replies usually contain the markdown/prose renderer; user messages are
    // typically plain bubbles and often miss stable role markers in newer layouts.
    if (node.querySelector('.markdown.prose, .markdown-new-styling')) return 'assistant';

    var labelText = (node.innerText || "").slice(0, 200).toLowerCase();
    if (labelText.startsWith('you\n') || labelText.startsWith('you ')) return 'user';
    if (labelText.startsWith('chatgpt\n') || labelText.startsWith('assistant\n')) return 'assistant';

    return (index % 2 === 0) ? 'user' : 'assistant';
  }

  function bestEffortLang(preEl) {
    function normalizeLangJs(v) {
      v = (v || '').trim().toLowerCase();
      var map = {
        py: 'python',
        python3: 'python',
        shell: 'bash',
        sh: 'bash',
        zsh: 'bash',
        shellscript: 'bash',
        js: 'javascript',
        ts: 'typescript',
        yml: 'yaml'
      };
      return map[v] || v;
    }

    // ChatGPT code blocks often show language in the first header line inside <pre>.
    try {
      var preText = (preEl && (preEl.innerText || preEl.textContent) || '').replace(/\r\n/g, '\n');
      var firstLine = (preText.split('\n')[0] || '').trim();
      if (firstLine && firstLine.length <= 20 && /^[A-Za-z0-9_+#.-]+$/.test(firstLine)) {
        var low0 = firstLine.toLowerCase();
        if (!['copy', 'copia', 'copy code'].includes(low0)) {
          return normalizeLangJs(low0);
        }
      }
    } catch (e) {}

    var candidates = [];
    if (preEl.parentElement) candidates.push(preEl.parentElement);
    if (preEl.parentElement && preEl.parentElement.parentElement) candidates.push(preEl.parentElement.parentElement);
    if (preEl.previousElementSibling) candidates.push(preEl.previousElementSibling);

    for (var i = 0; i < candidates.length; i++) {
      var c = candidates[i];
      if (!(c instanceof Element)) continue;
      var nodes = c.querySelectorAll ? c.querySelectorAll('span, div, button') : [];
      for (var j = 0; j < Math.min(nodes.length, 12); j++) {
        var t = (nodes[j].innerText || '').trim();
        if (!t || t.length > 24) continue;
        if (/^[A-Za-z0-9_+#.-]+$/.test(t)) {
          var low = t.toLowerCase();
          if (['copy code', 'copy'].includes(low)) continue;
          if (low.includes('language')) continue;
          return normalizeLangJs(low);
        }
      }
      var cls = (c.className || '').toString();
      var m = cls.match(/language-([a-zA-Z0-9_+-]+)/);
      if (m) return normalizeLangJs(m[1]);
    }

    var code = preEl.querySelector('code');
    if (code) {
      var className = code.className || '';
      var m2 = className.match(/language-([a-zA-Z0-9_+-]+)/);
      if (m2) return normalizeLangJs(m2[1]);
    }
    return '';
  }

  function extractParts(node) {
    var parts = [];
    var textBuf = '';
    var handledCodeContainers = new WeakSet();
    var blockTags = new Set([
      'p','div','section','article','header','footer','main','aside','blockquote',
      'ul','ol','li','table','thead','tbody','tr','td','th','h1','h2','h3','h4','h5','h6'
    ]);

    function shouldSkipUI(el) {
      if (!(el instanceof Element)) return false;
      var tag = el.tagName.toLowerCase();
      if (['script', 'style', 'noscript', 'svg', 'path'].includes(tag)) return true;
      if (tag === 'button') return true;
      if (el.matches('[data-testid*="copy"], [data-testid*="toolbar"], [role="toolbar"]')) return true;
      if (el.getAttribute('aria-hidden') === 'true' && !el.querySelector('pre code')) return true;
      if (!isVisibleish(el) && !el.querySelector('pre code')) return true;
      return false;
    }

    function appendText(t) {
      if (!t) return;
      textBuf += t;
    }

    function appendPlainText(t) {
      if (!t) return;
      textBuf += escapeMarkdownText(t);
    }

    function visitChildren(el) {
      var kids = el.childNodes || [];
      for (var i = 0; i < kids.length; i++) visit(kids[i]);
    }

    function classStr(el) {
      try { return ((el.className || '') + '').toLowerCase(); } catch (e) { return ''; }
    }

    function isLikelyBlockCodeContainer(el) {
      if (!(el instanceof Element)) return false;
      if (handledCodeContainers.has(el)) return false;
      var tag = (el.tagName || '').toLowerCase();
      if (tag === 'pre') return true;

      // Common custom code wrappers in modern UIs (including ChatGPT variants).
      var dt = ((el.getAttribute && el.getAttribute('data-testid')) || '').toLowerCase();
      var cls = classStr(el);
      var hasCodeDesc = !!(el.querySelector && el.querySelector('code'));
      if (!hasCodeDesc) return false;

      if (dt.includes('code')) return true;
      if (cls.includes('code-block') || cls.includes('codeblock')) return true;

      // Some renderers use a div wrapper with a single multiline <code> child.
      var directCode = el.children && el.children.length === 1 && el.firstElementChild &&
        el.firstElementChild.tagName && el.firstElementChild.tagName.toLowerCase() === 'code';
      if (directCode) {
        var codeTxt = (el.firstElementChild.textContent || '');
        if (codeTxt.includes('\n')) return true;
      }

      return false;
    }

    function codePartFromContainer(el) {
      if (!(el instanceof Element)) return null;
      var tag = (el.tagName || '').toLowerCase();
      var codeNode = null;
      var codeText = '';
      if (tag === 'pre') {
        // ChatGPT often renders code blocks as CodeMirror DOM inside <pre>, not <code>.
        var cm = el.querySelector('.cm-content, [class*="cm-content"]');
        if (cm) {
          codeNode = cm;
          codeText = (cm.innerText || cm.textContent || '').replace(/\r\n/g, '\n');
        } else {
          codeNode = el.querySelector('code');
          if (codeNode) {
            codeText = (codeNode.textContent || '').replace(/\r\n/g, '\n');
          } else {
            // Fallback for unknown <pre> variants: clone and strip obvious UI chrome.
            var clonePre = el.cloneNode(true);
            if (clonePre.querySelectorAll) {
              clonePre.querySelectorAll('button, svg, [aria-label*="copia" i], [aria-label*="copy" i]').forEach(function(n) {
                try { n.remove(); } catch (e) {}
              });
              clonePre.querySelectorAll('.sticky, [class*="sticky"]').forEach(function(n) {
                try { n.remove(); } catch (e) {}
              });
            }
            codeNode = clonePre;
            codeText = (clonePre.innerText || clonePre.textContent || '').replace(/\r\n/g, '\n');
          }
        }
      } else {
        codeNode = el.querySelector('pre code') || el.querySelector('code');
        if (codeNode) {
          codeText = (codeNode.textContent || '').replace(/\r\n/g, '\n');
        }
      }
      if (!codeNode) return null;
      if (!codeText.trim()) return null;

      // Guard against grabbing inline code wrappers as blocks.
      if (!codeText.includes('\n') && codeText.length < 120 && tag !== 'pre') {
        return null;
      }

      return {
        type: 'code',
        lang: bestEffortLang(tag === 'pre' ? el : (codeNode.closest('pre') || el)),
        code: codeText
      };
    }

    function imagePartFromImg(imgEl) {
      if (!(imgEl instanceof Element)) return null;
      // Images embedded inside inline rich-entity text (often inside <strong> in list items)
      // break markdown markers if extracted as standalone parts. Skip them for now.
      var inlineAnc = imgEl.closest && imgEl.closest('strong, b, em, i, a, li, p');
      if (inlineAnc) return null;
      var src = (imgEl.currentSrc || imgEl.getAttribute('src') || '').trim();
      if (!src) return null;
      if (/^data:image\//i.test(src) && src.length > 2_000_000) return null;
      if (imgEl.closest && imgEl.closest('[data-testid*="webpage-citation-pill"]')) return null;
      if (imgEl.closest && imgEl.closest('button,[role="button"]')) return null;
      var cls = classStr(imgEl);
      if (cls.includes('icon-sm') || cls.includes('favicon')) return null;
      var host = '';
      try { host = (new URL(src, location.href)).hostname.toLowerCase(); } catch (e) { host = ''; }
      if (host.includes('google.com') && src.includes('/favicons?')) return null;
      var w = Number(imgEl.getAttribute('width') || imgEl.width || 0);
      var h = Number(imgEl.getAttribute('height') || imgEl.height || 0);
      var inRichEntityImage = !!(imgEl.closest && imgEl.closest('[data-rich-entity-image="true"]'));
      if (!inRichEntityImage && w > 0 && h > 0 && w <= 48 && h <= 48) return null;
      return {
        type: 'image',
        src: src,
        alt: (imgEl.getAttribute('alt') || '').trim()
      };
    }

    function imagePartFromListItemRichEntity(liEl) {
      if (!(liEl instanceof Element)) return null;
      var rich = liEl.querySelector('[data-rich-entity-image="true"] img');
      if (!(rich instanceof Element)) return null;
      var src = (rich.currentSrc || rich.getAttribute('src') || '').trim();
      if (!src) return null;
      var alt = (rich.getAttribute('alt') || '').trim();
      return { type: 'image', src: src, alt: alt };
    }

    function ensureLineBreak() {
      if (!textBuf.endsWith('\n')) textBuf += '\n';
    }

    function flushText() {
      var norm = stripUiSpeechPrefixes(normalizeWhitespace(textBuf));
      if (norm) {
        if (parts.length && parts[parts.length - 1].type === 'text') {
          parts[parts.length - 1].text = stripUiSpeechPrefixes(
            normalizeWhitespace(parts[parts.length - 1].text + "\n" + norm)
          );
        } else {
          parts.push({ type: 'text', text: norm });
        }
      }
      textBuf = '';
    }

    function visit(n) {
      if (!n) return;
      if (n.nodeType === Node.TEXT_NODE) {
        var tnode = n.textContent || '';
        var parentTag = (n.parentElement && n.parentElement.tagName || '').toLowerCase();
        // DOM formatting whitespace inside list containers (<li>\n  <p>...</p>\n</li>)
        // must not become real newlines in markdown, otherwise "-" is split from item text.
        if ((parentTag === 'li' || parentTag === 'ul' || parentTag === 'ol') && !tnode.trim()) {
          return;
        }
        appendPlainText(tnode);
        return;
      }
      if (n.nodeType !== Node.ELEMENT_NODE) return;
      var el = n;
      if (shouldSkipUI(el)) return;
      var tag = el.tagName.toLowerCase();

      if (tag === 'br') {
        appendText('\n');
        return;
      }

      if (tag === 'img') {
        var imgPart = imagePartFromImg(el);
        if (imgPart) {
          flushText();
          parts.push(imgPart);
        }
        return;
      }

      if (isLikelyBlockCodeContainer(el)) {
        var codePart = codePartFromContainer(el);
        if (codePart) {
          flushText();
          parts.push(codePart);
          handledCodeContainers.add(el);
          return;
        }
      }

      if (el.matches && el.matches('[data-testid*="webpage-citation-pill"]')) {
        var a = el.querySelector('a[href]');
        if (a) {
          var hrefC = (a.getAttribute('href') || '').trim();
          var labelC = '';
          var labelNode = a.querySelector('span');
          if (labelNode) labelC = (labelNode.innerText || '').trim();
          if (!labelC) labelC = (a.innerText || '').replace(/\+\d+\s*$/,'').trim();
          if (!labelC && hrefC) {
            try { labelC = new URL(hrefC).hostname; } catch (e) { labelC = hrefC; }
          }
          if (hrefC && labelC) {
            appendText(' [' + escapeMarkdownText(labelC) + '](' + hrefC + ')');
          }
        }
        return;
      }

      // ChatGPT often uses <li><p>...</p></li>. If we treat <p> as a generic block here,
      // the list marker and the text get split across lines, which breaks markdown nesting.
      if (tag === 'p' && el.parentElement && el.parentElement.tagName && el.parentElement.tagName.toLowerCase() === 'li') {
        visitChildren(el);
        return;
      }

      if (tag === 'strong' || tag === 'b') {
        appendText('**');
        visitChildren(el);
        appendText('**');
        return;
      }

      if (tag === 'em' || tag === 'i') {
        appendText('*');
        visitChildren(el);
        appendText('*');
        return;
      }

      if (tag === 'a') {
        var href = (el.getAttribute('href') || '').trim();
        if (href) {
          appendText('[');
          visitChildren(el);
          appendText('](' + href + ')');
        } else {
          visitChildren(el);
        }
        return;
      }

      if (tag === 'code') {
        // Inline code only (block code is handled by <pre> below)
        if (!el.closest('pre') && !el.closest('[data-testid*="code"], .code-block, [class*="codeblock"], [class*="code-block"]')) {
          var inlineCode = (el.textContent || '').replace(/\r\n/g, ' ').trim();
          appendText('`' + inlineCode.replace(/`/g, '\\`') + '`');
          return;
        }
      }

      if (/^h[1-6]$/.test(tag)) {
        var level = parseInt(tag.slice(1), 10) || 1;
        ensureLineBreak();
        appendText('#'.repeat(level) + ' ');
        visitChildren(el);
        ensureLineBreak();
        ensureLineBreak();
        return;
      }

      if (tag === 'li') {
        var depth = 0;
        var p = el.parentElement;
        while (p) {
          var pt = (p.tagName || '').toLowerCase();
          if (pt === 'ul' || pt === 'ol') depth++;
          p = p.parentElement;
        }
        depth = Math.max(0, depth - 1);
        var liImage = imagePartFromListItemRichEntity(el);
        if (liImage) {
          flushText();
          parts.push(liImage);
        }
        ensureLineBreak();
        appendText(LIST_MARKER_PREFIX + depth + '__');
        visitChildren(el);
        ensureLineBreak();
        return;
      }

      if (tag === 'blockquote') {
        ensureLineBreak();
        var quoteText = normalizeWhitespace(el.innerText || '');
        if (quoteText) {
          quoteText.split('\n').forEach(function(line) {
            appendText('> ' + escapeMarkdownText(line));
            ensureLineBreak();
          });
          ensureLineBreak();
          return;
        }
      }

      var isBlock = blockTags.has(tag);
      if (isBlock) ensureLineBreak();
      var kids = el.childNodes;
      for (var i = 0; i < kids.length; i++) visit(kids[i]);
      if (isBlock) ensureLineBreak();
    }

    visit(node);
    flushText();

    var cleaned = [];
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (p.type === 'text') {
        var t = stripUiSpeechPrefixes(normalizeWhitespace(p.text || ''));
        if (t) cleaned.push({ type: 'text', text: t });
      } else if (p.type === 'code') {
        var c = (p.code || '').replace(/\u00a0/g, ' ');
        if (c) cleaned.push({ type: 'code', lang: p.lang || '', code: c });
      } else if (p.type === 'image') {
        var srcI = (p.src || '').trim();
        if (srcI) cleaned.push({ type: 'image', src: srcI, alt: (p.alt || '') });
      }
    }

    if (!cleaned.length) {
      var clone = node.cloneNode(true);
      clone.querySelectorAll && clone.querySelectorAll('pre').forEach(function(pre) { pre.remove(); });
      var fallbackText = stripUiSpeechPrefixes(normalizeWhitespace(clone.innerText || ''));
      if (fallbackText) cleaned.push({ type: 'text', text: fallbackText });
    }

    return cleaned;
  }

  function messageKey(node, index, parts) {
    var existingMirrorKey = node.getAttribute && node.getAttribute('data-cgm-message-key');
    if (existingMirrorKey) return existingMirrorKey;
    var attrs = ['data-message-id', 'id'];
    for (var i = 0; i < attrs.length; i++) {
      var v = node.getAttribute && node.getAttribute(attrs[i]);
      if (v) return v;
    }
    if (node.dataset) {
      if (node.dataset.messageId) return node.dataset.messageId;
      if (node.dataset.testid) return node.dataset.testid + ':' + index;
    }
    var firstText = '';
    for (var j = 0; j < parts.length; j++) {
      if (parts[j].type === 'text' && parts[j].text) {
        firstText = parts[j].text.slice(0, 120);
        break;
      }
      if (parts[j].type === 'code' && parts[j].code) {
        firstText = parts[j].code.slice(0, 120);
        break;
      }
    }
    return 'msg:' + index + ':' + simpleHash(firstText);
  }

  function contentHash(role, parts) {
    return simpleHash(JSON.stringify({ role: role, parts: parts }));
  }

  function isScrollable(el) {
    if (!el || !(el instanceof Element)) return false;
    var cs = getComputedStyle(el);
    if (!cs) return false;
    var oy = cs.overflowY;
    if (!/(auto|scroll|overlay)/.test(oy)) return false;
    return el.scrollHeight > el.clientHeight + 20;
  }

  function getScrollableAncestor(node) {
    var cur = node;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      if (isScrollable(cur)) return cur;
      cur = cur.parentElement;
    }
    return document.scrollingElement || document.documentElement || document.body;
  }

  function getScrollContainer(preferNode) {
    // Prefer the target node's own scrollable ancestor to avoid picking sidebars.
    if (preferNode && preferNode instanceof Element) {
      return getScrollableAncestor(preferNode);
    }
    // ChatGPT often scrolls an inner container rather than window.
    var candidates = [
      document.querySelector('[data-cgm-message-key]'),
      document.querySelector('article'),
      document.querySelector('main'),
      document.querySelector('[data-testid*="conversation"]'),
      document.querySelector('body')
    ].filter(Boolean);

    for (var i = 0; i < candidates.length; i++) {
      var root = candidates[i];
      if (root instanceof Element && root.hasAttribute && root.hasAttribute('data-cgm-message-key')) {
        return getScrollableAncestor(root);
      }
      if (isScrollable(root)) return root;
      var all = root.querySelectorAll ? root.querySelectorAll('div, section') : [];
      for (var j = 0; j < Math.min(all.length, 120); j++) {
        if (isScrollable(all[j])) return all[j];
      }
    }
    return document.scrollingElement || document.documentElement || document.body;
  }

  function getTopVisibleKey() {
    var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-cgm-message-key]'));
    if (!nodes.length) return '';
    var scroller = getScrollContainer(nodes[0] || null);
    var scrollerRect = (scroller && scroller.getBoundingClientRect) ? scroller.getBoundingClientRect() : null;
    var topBound = scrollerRect ? scrollerRect.top : 0;
    var bottomBound = scrollerRect ? scrollerRect.bottom : window.innerHeight;
    var best = null;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!(n instanceof Element)) continue;
      var rect = n.getBoundingClientRect();
      if (rect.height <= 0) continue;
      if (rect.bottom <= topBound) continue;
      if (rect.top >= bottomBound) continue;
      best = n;
      if (rect.top >= topBound) break;
    }
    if (!best) return '';
    return best.getAttribute('data-cgm-message-key') || '';
  }

  function pruneDom(messageNodes, keepDomCount, state) {
    if (!Array.isArray(messageNodes) || messageNodes.length <= keepDomCount) return;
    var now = Date.now();
    var keep = new Set(messageNodes.slice(-keepDomCount));
    messageNodes.forEach(function(node) {
      if (keep.has(node)) return;
      if (!(node instanceof Element)) return;
      if (node.matches('[data-cgm-pruned-placeholder="1"]')) return;
      var nodeKey = (node.getAttribute && node.getAttribute('data-cgm-message-key')) || '';
      if (nodeKey) {
        var pinUntil = Number(state.restorePinnedUntilByKey && state.restorePinnedUntilByKey.get(nodeKey) || 0);
        if (pinUntil > now) return;
        if (state.restorePinnedUntilByKey && pinUntil > 0) {
          state.restorePinnedUntilByKey.delete(nodeKey);
        }
      }
      var rect = node.getBoundingClientRect();
      var height = Math.max(24, Math.round(rect.height || 24));
      var ph = document.createElement('div');
      ph.setAttribute('data-cgm-pruned-placeholder', '1');
      var k = node.getAttribute('data-cgm-message-key');
      if (k) ph.setAttribute('data-cgm-message-key', k);
      ph.style.minHeight = height + 'px';
      ph.style.height = height + 'px';
      ph.style.boxSizing = 'border-box';
      ph.style.border = '1px dashed rgba(128,128,128,0.25)';
      ph.style.borderRadius = '8px';
      ph.style.margin = '4px 0';
      ph.style.background = 'rgba(128,128,128,0.05)';
      ph.style.color = 'rgba(128,128,128,0.7)';
      ph.style.fontSize = '12px';
      ph.style.display = 'flex';
      ph.style.alignItems = 'center';
      ph.style.padding = '6px 10px';
      ph.style.cursor = 'default';
      ph.textContent = 'Mirrored in native viewer (DOM pruned)';
      try {
        if (k) {
          state.prunedNodeCache.set(k, node);  // park the original DOM node (preserves listeners/UI state best-effort)
          state.placeholderByKey.set(k, ph);
          state.prunedKeyQueue.push(k);
          ph.title = 'Double click to restore this pruned DOM block';
          ph.addEventListener('dblclick', function(ev) {
            try {
              ev.preventDefault();
              ev.stopPropagation();
            } catch (e) {}
            if (!state || !state.restorePrunedOnView) return;
            restorePrunedByKey(state, k);
          }, { passive: false });
          // Keep cache bounded.
          while (state.prunedKeyQueue.length > state.maxPrunedCache) {
            var evictKey = state.prunedKeyQueue.shift();
            if (!evictKey) break;
            // Only evict if still pruned and not restored already.
            if (state.placeholderByKey.has(evictKey)) {
              state.prunedNodeCache.delete(evictKey);
              var stalePh = state.placeholderByKey.get(evictKey);
              if (stalePh && stalePh instanceof Element && !document.contains(stalePh)) {
                state.placeholderByKey.delete(evictKey);
              }
            }
          }
        }
        node.replaceWith(ph);
      } catch (e) {}
    });
  }

  function restorePrunedByKey(state, key) {
    if (!state) return 0;
    key = String(key || '');
    if (!key) return 0;
    var ph = state.placeholderByKey.get(key);
    var node = state.prunedNodeCache.get(key);
    if (!(ph instanceof Element) || !(node instanceof Element)) return 0;
    try {
      ph.replaceWith(node);
      state.placeholderByKey.delete(key);
      if (state.restorePinnedUntilByKey) {
        state.restorePinnedUntilByKey.set(key, Date.now() + (state.restorePinMs || 15000));
      }
      return 1;
    } catch (e) {
      return 0;
    }
  }

  function startExtractor(bridgeObj) {
    window.__chatgptMirror = window.__chatgptMirror || {};
    var state = window.__chatgptMirror;
    if (state.started) return;

    state.started = true;
    state.bridge = bridgeObj;
    state.hashByKey = new Map();
    state.keepDom = 30;
    state.restorePrunedOnView = false;
    state.prunedNodeCache = new Map();
    state.placeholderByKey = new Map();
    state.prunedKeyQueue = [];
    state.maxPrunedCache = 120;
    state.restorePinnedUntilByKey = new Map();
    state.restorePinMs = 15000;
    state.scrollSyncDebug = false;
    state.pending = false;
    state.lastScanAt = 0;
    state.lastTopKeySent = '';
    state.scrollEmitPending = false;
    state.programmaticScrollUntil = 0;

    function sendDeltas(deltas) {
      if (!deltas.length) return;
      try {
        bridgeObj.sendDelta(JSON.stringify(deltas));
      } catch (e) {
        console.warn('[chatgpt_mirror] sendDelta failed', e);
      }
    }

    function sendEvent(evt) {
      if (!evt || typeof evt !== 'object') return;
      try {
        if (bridgeObj && typeof bridgeObj.sendEvent === 'function') {
          bridgeObj.sendEvent(JSON.stringify(evt));
          return;
        }
      } catch (e) {}
      try {
        console.log(CONSOLE_EVENT_PREFIX + JSON.stringify(evt));
      } catch (e) {}
    }

    function emitTopKeyIfChanged(reason) {
      if (Date.now() < state.programmaticScrollUntil) return;
      var key = getTopVisibleKey();
      if (!key) return;
      if (key === state.lastTopKeySent) return;
      state.lastTopKeySent = key;
      if (state.scrollSyncDebug) {
        sendEvent({ type: 'scroll_debug', dir: 'web->native', stage: 'emit_top_key', key: key, reason: reason || 'web_scroll' });
      }
      sendEvent({ type: 'scroll_top_key', key: key, reason: reason || 'web_scroll' });
    }

    function scanNow(reason) {
      state.pending = false;
      state.lastScanAt = Date.now();
      var nodes = collectMessageNodes();
      var deltas = [];

      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        var role = detectRole(node, i);
        var parts = extractParts(node);
        if (!parts.length) continue;
        var key = messageKey(node, i, parts);
        try { node.setAttribute('data-cgm-message-key', key); } catch (e) {}
        var hash = contentHash(role, parts);
        var old = state.hashByKey.get(key);
        if (old !== hash) {
          state.hashByKey.set(key, hash);
          deltas.push({ key: key, role: role, parts: parts });
        }
      }

      sendDeltas(deltas);
      pruneDom(nodes, state.keepDom, state);
      emitTopKeyIfChanged('scan');
      return 'scan:' + reason + ':nodes=' + nodes.length + ':deltas=' + deltas.length;
    }

    function scheduleScan(reason) {
      if (state.pending) return;
      state.pending = true;
      setTimeout(function() { scanNow(reason || 'throttled'); }, 250);
    }

    state.scheduleScan = scheduleScan;
    state.scanNow = scanNow;
    state.scrollToKey = function(key) {
      if (!key) return false;
      var nodes = document.querySelectorAll('[data-cgm-message-key]');
      var node = null;
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].getAttribute('data-cgm-message-key') === String(key)) {
          node = nodes[i];
          break;
        }
      }
      if (!node) {
        if (state.scrollSyncDebug) {
          sendEvent({ type: 'scroll_debug', dir: 'native->web', stage: 'scroll_to_key', targetKey: String(key), found: false });
        }
        return false;
      }
      state.programmaticScrollUntil = Date.now() + 400;
      var isPlaceholder = false;
      try { isPlaceholder = node.matches && node.matches('[data-cgm-pruned-placeholder="1"]'); } catch (e) {}
      var topKeyBefore = '';
      try { topKeyBefore = getTopVisibleKey() || ''; } catch (e) {}
      var scroller = getScrollContainer(node);
      if (scroller === document.body || scroller === document.documentElement || scroller === document.scrollingElement) {
        var top = node.getBoundingClientRect().top + (window.scrollY || window.pageYOffset || 0);
        window.scrollTo({ top: Math.max(0, top - 8), behavior: 'auto' });
      } else {
        var scRect = scroller.getBoundingClientRect();
        var target = node.getBoundingClientRect().top - scRect.top + scroller.scrollTop - 8;
        scroller.scrollTop = Math.max(0, target);
      }
      if (state.scrollSyncDebug) {
        var topKeyAfter = '';
        try { topKeyAfter = getTopVisibleKey() || ''; } catch (e) {}
        sendEvent({
          type: 'scroll_debug',
          dir: 'native->web',
          stage: 'scroll_to_key',
          targetKey: String(key),
          found: true,
          placeholder: !!isPlaceholder,
          topKeyBefore: topKeyBefore,
          topKeyAfter: topKeyAfter
        });
      }
      setTimeout(function() { emitTopKeyIfChanged('programmatic'); }, 120);
      return true;
    };

    try {
      state.observer = new MutationObserver(function() {
        scheduleScan('mutation');
      });
      state.observer.observe(document.documentElement || document.body, {
        subtree: true,
        childList: true,
        characterData: true
      });
    } catch (e) {
      console.warn('[chatgpt_mirror] observer setup failed', e);
    }

    state.interval = setInterval(function() {
      scanNow('interval');
    }, 2000);

    window.addEventListener('scroll', function() {
      if (state.scrollEmitPending) return;
      state.scrollEmitPending = true;
      setTimeout(function() {
        state.scrollEmitPending = false;
        emitTopKeyIfChanged('scroll');
      }, 100);
    }, { passive: true, capture: true });

    scheduleScan('startup');
    return 'chatgpt_mirror_started';
  }

  function initBridgeAndStart() {
    if (window.__chatgptMirror && window.__chatgptMirror.started) return;
    if (window.qt && window.qt.webChannelTransport && window.QWebChannel) {
      try {
        new QWebChannel(window.qt.webChannelTransport, function(channel) {
          if (!channel || !channel.objects || !channel.objects.bridge) {
            console.warn('[chatgpt_mirror] bridge object missing');
            return;
          }
          startExtractor(channel.objects.bridge);
        });
        return;
      } catch (e) {
        console.warn('[chatgpt_mirror] QWebChannel init failed, using console fallback', e);
      }
    }

    // Fallback path for pages with CSP blocking qrc:///qtwebchannel/qwebchannel.js.
    startExtractor({
      sendDelta: function(jsonString) {
        try {
          console.log(CONSOLE_DELTA_PREFIX + jsonString);
        } catch (e) {}
      },
      sendEvent: function(jsonString) {
        try {
          console.log(CONSOLE_EVENT_PREFIX + jsonString);
        } catch (e) {}
      }
    });
  }

  initBridgeAndStart();
  return "chatgpt_mirror_bootstrap_requested";
})();
"""


def monospace_font() -> QFont:
    font = QFont("DejaVu Sans Mono")
    font.setStyleHint(QFont.Monospace)
    return font


def normalize_code_lang(lang: str) -> str:
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


def preview_from_markdown(markdown_text: str) -> str:
    text = markdown_text or ""
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[(.*?)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\n+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class MessagePart:
    type: str
    text: str = ""
    code: str = ""
    lang: str = ""
    image_url: str = ""
    alt: str = ""


@dataclass
class Message:
    key: str
    role: str
    parts: List[MessagePart]
    collapsed: bool = False
    size_hint: QSize = field(default_factory=lambda: QSize(640, 120))

    def role_label(self) -> str:
        return "You" if self.role == "user" else "Assistant"

    def preview_text(self, limit: int = 120) -> str:
        chunks: List[str] = []
        for part in self.parts:
            if part.type == "text" and part.text.strip():
                chunks.append(preview_from_markdown(part.text.strip()))
            elif part.type == "code" and part.code.strip():
                lang = part.lang.strip()
                head = f"[code:{lang}] " if lang else "[code] "
                chunks.append(head + part.code.strip().splitlines()[0])
            elif part.type == "image" and part.image_url.strip():
                chunks.append("[image] " + (part.alt.strip() or part.image_url.strip()))
        text = " ".join(chunks).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."

    def as_copy_text(self) -> str:
        out: List[str] = []
        for part in self.parts:
            if part.type == "text":
                text = part.text.strip()
                if text:
                    out.append(text)
            elif part.type == "code":
                code = part.code.rstrip("\n")
                lang = (part.lang or "").strip()
                out.append(f"```{lang}\n{code}\n```")
            elif part.type == "image":
                url = (part.image_url or "").strip()
                if url:
                    alt = (part.alt or "").strip()
                    out.append(f"![{alt}]({url})" if alt else f"![]({url})")
        return "\n\n".join(out).strip()


class MessageListModel(QAbstractListModel):
    MessageRole = Qt.UserRole + 1
    KeyRole = Qt.UserRole + 2

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._order: List[str] = []
        self._messages: Dict[str, Message] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._order)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._order):
            return None
        key = self._order[row]
        msg = self._messages[key]
        if role == self.MessageRole:
            return msg
        if role == self.KeyRole:
            return key
        if role == Qt.DisplayRole:
            return msg.preview_text()
        if role == Qt.SizeHintRole:
            return msg.size_hint
        return None

    def roleNames(self) -> Dict[int, bytes]:
        roles = super().roleNames()
        roles[self.MessageRole] = b"message"
        roles[self.KeyRole] = b"key"
        return roles

    def message_at_row(self, row: int) -> Optional[Message]:
        if 0 <= row < len(self._order):
            return self._messages[self._order[row]]
        return None

    def row_for_key(self, key: str) -> int:
        try:
            return self._order.index(key)
        except ValueError:
            return -1

    def set_collapsed(self, key: str, collapsed: bool) -> None:
        msg = self._messages.get(key)
        if not msg or msg.collapsed == collapsed:
            return
        msg.collapsed = collapsed
        row = self.row_for_key(key)
        if row >= 0:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [self.MessageRole, Qt.DisplayRole, Qt.SizeHintRole])

    def toggle_collapsed(self, key: str) -> None:
        msg = self._messages.get(key)
        if msg:
            self.set_collapsed(key, not msg.collapsed)

    def update_size_hint(self, key: str, size_hint: QSize) -> None:
        msg = self._messages.get(key)
        if not msg:
            return
        if msg.size_hint == size_hint:
            return
        msg.size_hint = size_hint
        row = self.row_for_key(key)
        if row >= 0:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.SizeHintRole])

    def apply_deltas(self, deltas: List[dict]) -> None:
        for payload in deltas:
            key = str(payload.get("key") or "").strip()
            if not key:
                continue
            role = str(payload.get("role") or "assistant").strip().lower()
            if role not in {"user", "assistant"}:
                role = "assistant"
            raw_parts = payload.get("parts") or []
            parts: List[MessagePart] = []
            for item in raw_parts:
                if not isinstance(item, dict):
                    continue
                ptype = str(item.get("type") or "").strip()
                if ptype == "text":
                    text = str(item.get("text") or "")
                    if text.strip():
                        parts.append(MessagePart(type="text", text=text))
                elif ptype == "code":
                    code = str(item.get("code") or "")
                    if code:
                        parts.append(
                            MessagePart(
                                type="code",
                                code=code,
                                lang=str(item.get("lang") or ""),
                            )
                        )
                elif ptype == "image":
                    src = str(item.get("src") or item.get("url") or "").strip()
                    if src:
                        parts.append(
                            MessagePart(
                                type="image",
                                image_url=src,
                                alt=str(item.get("alt") or ""),
                            )
                        )
            if not parts:
                continue

            if key in self._messages:
                msg = self._messages[key]
                msg.role = role
                msg.parts = parts
                row = self.row_for_key(key)
                if row >= 0:
                    idx = self.index(row, 0)
                    self.dataChanged.emit(idx, idx, [self.MessageRole, Qt.DisplayRole])
            else:
                row = len(self._order)
                self.beginInsertRows(QModelIndex(), row, row)
                self._order.append(key)
                self._messages[key] = Message(key=key, role=role, parts=parts)
                self.endInsertRows()

    def messages_in_order(self) -> List[Message]:
        return [self._messages[k] for k in self._order if k in self._messages]


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
    copyRequested = Signal(str)
    relayoutRequested = Signal()

    _net_mgr: Optional[QNetworkAccessManager] = None

    def __init__(self, image_url: str, alt: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.image_url = (image_url or "").strip()
        self.alt = (alt or "").strip()
        self._pixmap: Optional[QPixmap] = None
        self._reply = None
        self._build_ui()
        self._start_load()

    @classmethod
    def _manager(cls) -> QNetworkAccessManager:
        if cls._net_mgr is None:
            cls._net_mgr = QNetworkAccessManager()
        return cls._net_mgr

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 2, 0, 2)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        chip = QLabel("image")
        chip.setStyleSheet(
            "QLabel { background: #e7edf6; color: #2d3748; padding: 2px 8px; border-radius: 9px; font-size: 11px; }"
        )
        top.addWidget(chip)
        top.addStretch(1)
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
        outer.addWidget(self.preview)

        link_html = f'<a href="{html_escape(self.image_url)}">{html_escape(self.image_url)}</a>'
        self.url_label = QLabel(link_html)
        self.url_label.setOpenExternalLinks(True)
        self.url_label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.url_label.setWordWrap(True)
        self.url_label.setStyleSheet("QLabel { color: #334155; font-size: 12px; }")
        outer.addWidget(self.url_label)

    def _start_load(self) -> None:
        if not self.image_url:
            return
        try:
            req = QNetworkRequest(QUrl(self.image_url))
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
                elif part.type == "image":
                    img_widget = ImagePartWidget(part.image_url, part.alt)
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
    autoScrollChanged = Signal(bool)
    webToNativeSyncChanged = Signal(bool)
    nativeToWebSyncChanged = Signal(bool)
    keepDomChanged = Signal(int)
    restorePrunedOnViewChanged = Signal(bool)
    scrollSyncDebugChanged = Signal(bool)
    browserLanguageChanged = Signal(str)
    resetSessionRequested = Signal()
    exportRequested = Signal(str)
    exportDebugVisibleRequested = Signal()
    exportPdfImagesDebugRequested = Signal()

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
        self._code_block_display_mode = "auto"
        self._browser_language_mode = "system"
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
        self.auto_scroll_action = QAction("Auto-scroll nuovi messaggi", self, checkable=True)
        self.auto_scroll_action.setChecked(self._auto_scroll_enabled)
        self.auto_scroll_action.toggled.connect(self.autoScrollChanged.emit)
        settings_menu.addAction(self.auto_scroll_action)

        self.web_to_native_action = QAction("Sync scroll Web -> Native", self, checkable=True)
        self.web_to_native_action.setChecked(self._web_to_native_sync_enabled)
        self.web_to_native_action.toggled.connect(self.webToNativeSyncChanged.emit)
        settings_menu.addAction(self.web_to_native_action)

        self.native_to_web_action = QAction("Sync scroll Native -> Web", self, checkable=True)
        self.native_to_web_action.setChecked(self._native_to_web_sync_enabled)
        self.native_to_web_action.toggled.connect(self.nativeToWebSyncChanged.emit)
        settings_menu.addAction(self.native_to_web_action)

        settings_menu.addSeparator()

        keep_dom_menu = settings_menu.addMenu("KEEP_DOM (WebView)")
        self.keep_dom_group = QActionGroup(self)
        self.keep_dom_group.setExclusive(True)
        for count in (30, 80, 150):
            action = QAction(str(count), self, checkable=True)
            action.setChecked(count == self._keep_dom_count)
            action.triggered.connect(lambda checked=False, c=count: self.keepDomChanged.emit(c))
            self.keep_dom_group.addAction(action)
            keep_dom_menu.addAction(action)

        code_block_menu = settings_menu.addMenu("Blocchi codice (Native)")
        self.code_block_mode_group = QActionGroup(self)
        self.code_block_mode_group.setExclusive(True)
        code_mode_specs = [
            ("Auto (collassa lunghi)", "auto"),
            ("Espansi", "expanded"),
            ("Espansione totale", "full"),
        ]
        for label, mode in code_mode_specs:
            action = QAction(label, self, checkable=True)
            action.setChecked(mode == self._code_block_display_mode)
            action.triggered.connect(lambda checked=False, m=mode: self._set_code_block_display_mode(m))
            self.code_block_mode_group.addAction(action)
            code_block_menu.addAction(action)

        restore_pruned_action = QAction("Consenti ripristino DOM pruned con doppio click", self, checkable=True)
        restore_pruned_action.setChecked(self._restore_pruned_on_view)
        restore_pruned_action.toggled.connect(self.restorePrunedOnViewChanged.emit)
        settings_menu.addAction(restore_pruned_action)

        scroll_sync_debug_action = QAction("Debug scroll sync (log)", self, checkable=True)
        scroll_sync_debug_action.setChecked(self._scroll_sync_debug_enabled)
        scroll_sync_debug_action.toggled.connect(self.scrollSyncDebugChanged.emit)
        settings_menu.addAction(scroll_sync_debug_action)

        browser_lang_menu = settings_menu.addMenu("Lingua browser")
        self.browser_lang_group = QActionGroup(self)
        self.browser_lang_group.setExclusive(True)

        lang_system = QAction("Sistema", self, checkable=True)
        lang_system.setChecked(self._browser_language_mode == "system")
        lang_system.triggered.connect(lambda checked=False: self.browserLanguageChanged.emit("system"))
        self.browser_lang_group.addAction(lang_system)
        browser_lang_menu.addAction(lang_system)

        lang_en = QAction("English", self, checkable=True)
        lang_en.setChecked(self._browser_language_mode == "en")
        lang_en.triggered.connect(lambda checked=False: self.browserLanguageChanged.emit("en"))
        self.browser_lang_group.addAction(lang_en)
        browser_lang_menu.addAction(lang_en)

        settings_menu.addSeparator()

        reset_action = QAction("Reset sessione", self)
        reset_action.triggered.connect(self.resetSessionRequested.emit)
        settings_menu.addAction(reset_action)

        export_menu = settings_menu.addMenu("Esporta conversazione")
        export_md = QAction("Markdown (.md)", self)
        export_md.triggered.connect(lambda: self.exportRequested.emit("md"))
        export_menu.addAction(export_md)
        export_json = QAction("JSON (.json)", self)
        export_json.triggered.connect(lambda: self.exportRequested.emit("json"))
        export_menu.addAction(export_json)
        export_pdf = QAction("PDF (.pdf)", self)
        export_pdf.triggered.connect(lambda: self.exportRequested.emit("pdf"))
        export_menu.addAction(export_pdf)

        debug_action = QAction("Debug blocco visibile (.txt)", self)
        debug_action.triggered.connect(self.exportDebugVisibleRequested.emit)
        settings_menu.addAction(debug_action)

        pdf_img_debug_action = QAction("Debug PDF immagini (.txt)", self)
        pdf_img_debug_action.triggered.connect(self.exportPdfImagesDebugRequested.emit)
        settings_menu.addAction(pdf_img_debug_action)

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

    def top_visible_key(self) -> Optional[str]:
        viewport = self.list_view.viewport()
        probe_points = [QPoint(12, 12), QPoint(12, 32), QPoint(12, 60)]
        for p in probe_points:
            idx = self.list_view.indexAt(p)
            if idx.isValid():
                msg = self.model.message_at_row(idx.row())
                return msg.key if msg else None
        # Fallback: first visible row by geometry scan.
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            rect = self.list_view.visualRect(idx)
            if rect.isValid() and rect.bottom() >= 0:
                msg = self.model.message_at_row(row)
                return msg.key if msg else None
        return None

    def scroll_key_to_top(self, key: str) -> bool:
        row = self.model.row_for_key(key)
        if row < 0:
            return False
        idx = self.model.index(row, 0)
        if not idx.isValid():
            return False
        self.list_view.scrollTo(idx, QListView.PositionAtTop)
        return True


class WebBridge(QObject):
    deltaReceived = Signal(str)
    eventReceived = Signal(str)

    @Slot(str)
    def sendDelta(self, json_string: str) -> None:
        self.deltaReceived.emit(json_string)

    @Slot(str)
    def sendEvent(self, json_string: str) -> None:
        self.eventReceived.emit(json_string)


class MirrorWebPage(QWebEnginePage):
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


class MainWindow(QMainWindow):
    def __init__(
        self,
        tabs_host: Optional[QWidget] = None,
        shared_profile: Optional[QWebEngineProfile] = None,
        profile_root: Optional[Path] = None,
        initial_url: Optional[str] = "https://chatgpt.com",
    ) -> None:
        super().__init__()
        self._tabs_host = tabs_host
        self.setWindowTitle("ChatGPT Mirror (PySide6 MVP)")
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
        self._profile_root = profile_root
        self.left_pane.list_view.verticalScrollBar().valueChanged.connect(self._on_native_scroll_value_changed)
        self.left_pane.autoScrollChanged.connect(self._on_auto_scroll_changed)
        self.left_pane.webToNativeSyncChanged.connect(self._on_web_to_native_sync_changed)
        self.left_pane.nativeToWebSyncChanged.connect(self._on_native_to_web_sync_changed)
        self.left_pane.keepDomChanged.connect(self._on_keep_dom_changed)
        self.left_pane.restorePrunedOnViewChanged.connect(self._on_restore_pruned_on_view_changed)
        self.left_pane.scrollSyncDebugChanged.connect(self._on_scroll_sync_debug_changed)
        self.left_pane.browserLanguageChanged.connect(self._on_browser_language_changed)
        self.left_pane.resetSessionRequested.connect(self._on_reset_session_requested)
        self.left_pane.exportRequested.connect(self._on_export_requested)
        self.left_pane.exportDebugVisibleRequested.connect(self._on_export_debug_visible_requested)
        self.left_pane.exportPdfImagesDebugRequested.connect(self._on_export_pdf_images_debug_requested)

        splitter = QSplitter(Qt.Horizontal)  # Horizontal splitter => left/right panes.
        splitter.addWidget(self.left_pane)
        splitter.addWidget(self.web_view)
        splitter.setSizes([700, 900])
        self.setCentralWidget(splitter)

        self._apply_browser_language_setting()
        if initial_url:
            self.web_view.setUrl(QUrl(initial_url))

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
        try:
            payload = json.loads(json_string)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, list):
            return
        self.model.apply_deltas(payload)
        # Scroll to latest when new messages arrive; avoid jerky behavior by doing it async.
        if self._auto_scroll_enabled and self.model.rowCount() > 0:
            QTimer.singleShot(0, self._scroll_to_bottom)

    @Slot(str)
    def on_web_event_received(self, json_string: str) -> None:
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
        self._suppress_native_scroll_until = time.monotonic() + 0.35
        ok = self.left_pane.scroll_key_to_top(key)
        if self._scroll_sync_debug_enabled:
            reason = str(evt.get("reason") or "")
            print(f"[scroll-sync] web->native key={key} reason={reason or '-'} ok={ok}")

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
        key = self.left_pane.top_visible_key()
        if not key:
            return
        if self._scroll_sync_debug_enabled:
            print(f"[scroll-sync] native->web request key={key}")
        self._ignore_web_scroll_events_until = time.monotonic() + 0.45
        script = (
            "(function(){"
            "if(window.__chatgptMirror && typeof window.__chatgptMirror.scrollToKey==='function'){"
            f"window.__chatgptMirror.scrollToKey({json.dumps(key)});"
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
        messages = self.model.messages_in_order()
        if not messages:
            QMessageBox.information(self, "Esporta", "Nessun messaggio da esportare.")
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
            self._export_pdf_from_messages(messages, path)
            return

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

    def _markdown_fragment_to_html(self, markdown_text: str) -> str:
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
        return m.group(1) if m else html

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
            print(f"[pdf-img] cache miss src={src}")
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
            print(f"[pdf-img] cache hit src={src} file={fpath} bytes={len(data)}")
            return fpath.resolve().as_uri()
        except Exception:
            print(f"[pdf-img] cache hit but file write failed src={src}")
            return src

    def _conversation_as_html_for_pdf(self, messages: List[Message]) -> str:
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
                        blocks.append(f'<div class="text-part">{self._markdown_fragment_to_html(txt)}</div>')
                elif part.type == "code":
                    blocks.append(self._code_html_for_pdf(part.code, part.lang))
                elif part.type == "image":
                    src = (part.image_url or "").strip()
                    if src:
                        alt = html_escape((part.alt or "").strip() or "image")
                        pdf_src = self._pdf_local_thumb_url(src)
                        blocks.append(
                            f'<div class="image-part"><a href="{html_escape(src)}">{alt}</a><br>'
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

    def _export_pdf_from_messages(self, messages: List[Message], path: str) -> None:
        html = self._conversation_as_html_for_pdf(messages)

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
                print("[pdf-export] timeout during WebEngine printToPdf, falling back")
                _finish_loop()

            def _on_load(ok: bool):
                state["loaded"] = True
                if not ok:
                    print("[pdf-export] WebEngine load failed, falling back")
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
                    print(f"[pdf-export] printToPdf start failed: {exc}")
                    _finish_loop()

            def _on_pdf_finished(file_path: str, success: bool):
                state["printed"] = True
                state["ok"] = bool(success)
                print(f"[pdf-export] pdfPrintingFinished success={success} path={file_path}")
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
            print(f"[pdf-export] WebEngine PDF export failed, falling back: {exc}")

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
                        else {"type": "image", "src": p.image_url, "alt": p.alt}
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
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ChatGPT Mirror (PySide6 MVP)")
        self.resize(1600, 900)
        self._profile_root = ensure_profile_root()
        self.web_profile = QWebEngineProfile("chatgpt_mirror_profile", self)
        self.web_profile.setPersistentStoragePath(str(self._profile_root / "qtwebengine"))
        self.web_profile.setCachePath(str(self._profile_root / "qtwebengine-cache"))
        self.web_profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self.web_profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.setCentralWidget(self.tabs)

        self.create_mirror_tab(url="https://chatgpt.com", switch=True)

    def create_mirror_tab(self, url: Optional[str] = "https://chatgpt.com", switch: bool = True) -> Optional[MainWindow]:
        pane = MainWindow(
            tabs_host=self,
            shared_profile=self.web_profile,
            profile_root=self._profile_root,
            initial_url=url,
        )
        idx = self.tabs.addTab(pane, "Nuovo tab")
        try:
            pane.web_view.titleChanged.connect(lambda title, p=pane: self._update_tab_title_for_pane(p, title))
            pane.web_view.urlChanged.connect(lambda _u, p=pane: self._update_tab_title_for_pane(p, pane.web_view.title()))
        except Exception:
            pass
        self._update_tab_title_for_pane(pane, pane.web_view.title())
        if switch:
            self.tabs.setCurrentIndex(idx)
        return pane

    def _pane_tab_index(self, pane: MainWindow) -> int:
        for i in range(self.tabs.count()):
            if self.tabs.widget(i) is pane:
                return i
        return -1

    def _update_tab_title_for_pane(self, pane: MainWindow, title: str) -> None:
        idx = self._pane_tab_index(pane)
        if idx < 0:
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
        self.tabs.removeTab(index)
        if w is not None:
            w.deleteLater()


def main() -> int:
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
