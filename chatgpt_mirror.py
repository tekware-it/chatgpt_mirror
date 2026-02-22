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
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPoint,
    QUrl,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
    QStandardPaths,
)
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMainWindow,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView


JS_INJECTOR = r"""
(function() {
  if (window.__chatgptMirror && window.__chatgptMirror.started) {
    return "chatgpt_mirror_already_started";
  }

  var CONSOLE_DELTA_PREFIX = "__CGM_DELTA__";
  var CONSOLE_EVENT_PREFIX = "__CGM_EVT__";

  function simpleHash(str) {
    var h = 5381;
    for (var i = 0; i < str.length; i++) {
      h = ((h << 5) + h) ^ str.charCodeAt(i);
    }
    return (h >>> 0).toString(16);
  }

  function normalizeWhitespace(text) {
    if (!text) return "";
    return text
      .replace(/\r\n/g, "\n")
      .replace(/\t/g, "  ")
      .replace(/[ \u00a0]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  function stripUiSpeechPrefixes(text) {
    if (!text) return "";
    var lines = text.split('\n');
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
      ['data-message-author-role', 'data-testid', 'aria-label', 'id', 'class'].forEach(function(attr) {
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

    var ariaCandidates = node.querySelectorAll('[aria-label], [alt]');
    for (var k = 0; k < Math.min(ariaCandidates.length, 10); k++) {
      var t = ((ariaCandidates[k].getAttribute('aria-label') || '') + ' ' +
               (ariaCandidates[k].getAttribute('alt') || '')).toLowerCase();
      if (t.includes('assistant') || t.includes('chatgpt')) return 'assistant';
      if (t.includes('you') || t.includes('user')) return 'user';
    }

    var labelText = (node.innerText || "").slice(0, 200).toLowerCase();
    if (labelText.startsWith('you\n') || labelText.startsWith('you ')) return 'user';
    if (labelText.startsWith('chatgpt\n') || labelText.startsWith('assistant\n')) return 'assistant';

    return (index % 2 === 0) ? 'user' : 'assistant';
  }

  function bestEffortLang(preEl) {
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
          return low;
        }
      }
      var cls = (c.className || '').toString();
      var m = cls.match(/language-([a-zA-Z0-9_+-]+)/);
      if (m) return m[1].toLowerCase();
    }

    var code = preEl.querySelector('code');
    if (code) {
      var className = code.className || '';
      var m2 = className.match(/language-([a-zA-Z0-9_+-]+)/);
      if (m2) return m2[1].toLowerCase();
    }
    return '';
  }

  function extractParts(node) {
    var parts = [];
    var textBuf = '';
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

    function ensureLineBreak() {
      if (!textBuf.endsWith('\n')) textBuf += '\n';
    }

    function flushText() {
      var norm = stripUiSpeechPrefixes(normalizeWhitespace(textBuf));
      if (norm) {
        if (parts.length && parts[parts.length - 1].type === 'text') {
          parts[parts.length - 1].text = normalizeWhitespace(parts[parts.length - 1].text + "\n" + norm);
        } else {
          parts.push({ type: 'text', text: norm });
        }
      }
      textBuf = '';
    }

    function visit(n) {
      if (!n) return;
      if (n.nodeType === Node.TEXT_NODE) {
        appendText(n.textContent || '');
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

      if (tag === 'pre') {
        var codeEl = el.querySelector('code');
        if (codeEl) {
          flushText();
          parts.push({
            type: 'code',
            lang: bestEffortLang(el),
            code: (codeEl.textContent || '').replace(/\r\n/g, '\n')
          });
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

  function pruneDom(messageNodes, keepDomCount) {
    if (!Array.isArray(messageNodes) || messageNodes.length <= keepDomCount) return;
    var keep = new Set(messageNodes.slice(-keepDomCount));
    messageNodes.forEach(function(node) {
      if (keep.has(node)) return;
      if (!(node instanceof Element)) return;
      if (node.matches('[data-cgm-pruned-placeholder="1"]')) return;
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
      ph.textContent = 'Mirrored in native viewer (DOM pruned)';
      try {
        node.replaceWith(ph);
      } catch (e) {}
    });
  }

  function startExtractor(bridgeObj) {
    window.__chatgptMirror = window.__chatgptMirror || {};
    var state = window.__chatgptMirror;
    if (state.started) return;

    state.started = true;
    state.bridge = bridgeObj;
    state.hashByKey = new Map();
    state.keepDom = 30;
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
      pruneDom(nodes, state.keepDom);
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
      if (!node) return false;
      state.programmaticScrollUntil = Date.now() + 400;
      var scroller = getScrollContainer(node);
      if (scroller === document.body || scroller === document.documentElement || scroller === document.scrollingElement) {
        var top = node.getBoundingClientRect().top + (window.scrollY || window.pageYOffset || 0);
        window.scrollTo({ top: Math.max(0, top - 8), behavior: 'auto' });
      } else {
        var scRect = scroller.getBoundingClientRect();
        var target = node.getBoundingClientRect().top - scRect.top + scroller.scrollTop - 8;
        scroller.scrollTop = Math.max(0, target);
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


@dataclass
class MessagePart:
    type: str
    text: str = ""
    code: str = ""
    lang: str = ""


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
                chunks.append(part.text.strip())
            elif part.type == "code" and part.code.strip():
                lang = part.lang.strip()
                head = f"[code:{lang}] " if lang else "[code] "
                chunks.append(head + part.code.strip().splitlines()[0])
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


class CodeBlockWidget(QWidget):
    copyRequested = Signal(str)

    def __init__(self, code: str, lang: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.code = code
        self.lang = lang
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        lang_label = QLabel(self.lang or "code")
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

        outer.addLayout(header)

        editor = QPlainTextEdit()
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

        line_count = max(1, min(12, self.code.count("\n") + 1))
        fm = editor.fontMetrics()
        height = (fm.lineSpacing() * line_count) + 24
        editor.setMinimumHeight(height)
        editor.setMaximumHeight(max(height, 60))
        outer.addWidget(editor)


class MessageRowWidget(QFrame):
    relayoutRequested = Signal(str, QSize)

    def __init__(
        self,
        key: str,
        copy_message_cb,
        toggle_collapse_cb,
        copy_code_cb,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self._copy_message_cb = copy_message_cb
        self._toggle_collapse_cb = toggle_collapse_cb
        self._copy_code_cb = copy_code_cb
        self._message: Optional[Message] = None
        self._index_display = 0
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
                    label = QLabel(part.text)
                    label.setWordWrap(True)
                    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    label.setStyleSheet("QLabel { color: #111827; line-height: 1.3; }")
                    self.expanded_layout.addWidget(label)
                elif part.type == "code":
                    code_widget = CodeBlockWidget(part.code, part.lang)
                    code_widget.copyRequested.connect(self._copy_code_cb)
                    self.expanded_layout.addWidget(code_widget)
            self.expanded_layout.addStretch(0)

        self._schedule_relayout()

    def _schedule_relayout(self) -> None:
        QTimer.singleShot(0, self._emit_size_hint)

    def _emit_size_hint(self) -> None:
        self.updateGeometry()
        self.adjustSize()
        self.relayoutRequested.emit(self.key, self.sizeHint())


class MessageListPane(QWidget):
    def __init__(self, model: MessageListModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = model
        self._widgets_by_key: Dict[str, MessageRowWidget] = {}
        self._build_ui()
        self._connect_model()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.header = QLabel("Native Mirror")
        self.header.setStyleSheet("QLabel { font-size: 14px; font-weight: 700; color: #111827; }")
        layout.addWidget(self.header)

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
        )
        widget.relayoutRequested.connect(self._on_row_relayout_requested)
        self._widgets_by_key[msg.key] = widget

        index = self.model.index(row, 0)
        self.list_view.setIndexWidget(index, widget)
        widget.set_message(msg, row + 1)

    @Slot(str, QSize)
    def _on_row_relayout_requested(self, key: str, size_hint: QSize) -> None:
        padded = QSize(max(360, size_hint.width() + 8), max(70, size_hint.height() + 8))
        self.model.update_size_hint(key, padded)
        self.list_view.doItemsLayout()

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

    def javaScriptConsoleMessage(self, level, message, line_number, source_id) -> None:  # type: ignore[override]
        if isinstance(message, str) and message.startswith(self.CONSOLE_DELTA_PREFIX):
            self.consoleDeltaReceived.emit(message[len(self.CONSOLE_DELTA_PREFIX) :])
            return
        if isinstance(message, str) and message.startswith(self.CONSOLE_EVENT_PREFIX):
            self.consoleEventReceived.emit(message[len(self.CONSOLE_EVENT_PREFIX) :])
            return
        super().javaScriptConsoleMessage(level, message, line_number, source_id)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ChatGPT Mirror (PySide6 MVP)")
        self.resize(1600, 900)

        self.model = MessageListModel(self)
        self.left_pane = MessageListPane(self.model)
        self.web_view = QWebEngineView()

        # Use a disk-backed WebEngine profile so chatgpt.com cookies/session survive app restarts.
        app_data_root = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not app_data_root:
            app_data_root = str(Path.home() / ".local" / "share" / "chatgpt_mirror")
        profile_root = Path(app_data_root)
        profile_root.mkdir(parents=True, exist_ok=True)
        (profile_root / "qtwebengine").mkdir(parents=True, exist_ok=True)
        (profile_root / "qtwebengine-cache").mkdir(parents=True, exist_ok=True)

        self.web_profile = QWebEngineProfile("chatgpt_mirror_profile", self)
        self.web_profile.setPersistentStoragePath(str(profile_root / "qtwebengine"))
        self.web_profile.setCachePath(str(profile_root / "qtwebengine-cache"))
        self.web_profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self.web_profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        self.web_page = MirrorWebPage(self.web_profile, self.web_view)
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
        self.left_pane.list_view.verticalScrollBar().valueChanged.connect(self._on_native_scroll_value_changed)

        splitter = QSplitter(Qt.Horizontal)  # Horizontal splitter => left/right panes.
        splitter.addWidget(self.left_pane)
        splitter.addWidget(self.web_view)
        splitter.setSizes([700, 900])
        self.setCentralWidget(splitter)

        self.web_view.setUrl(QUrl("https://chatgpt.com"))

    @Slot(bool)
    def on_load_finished(self, ok: bool) -> None:
        if not ok:
            return
        # Inject bootstrap + extractor after each page load/navigation.
        self.web_view.page().runJavaScript(JS_INJECTOR)

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
        if self.model.rowCount() > 0:
            QTimer.singleShot(0, self._scroll_to_bottom)

    @Slot(str)
    def on_web_event_received(self, json_string: str) -> None:
        try:
            evt = json.loads(json_string)
        except json.JSONDecodeError:
            return
        if not isinstance(evt, dict):
            return
        if evt.get("type") != "scroll_top_key":
            return
        if time.monotonic() < self._ignore_web_scroll_events_until:
            return
        key = str(evt.get("key") or "")
        if not key:
            return
        self._suppress_native_scroll_until = time.monotonic() + 0.35
        self.left_pane.scroll_key_to_top(key)

    def _scroll_to_bottom(self) -> None:
        idx = self.model.index(max(0, self.model.rowCount() - 1), 0)
        if idx.isValid():
            self.left_pane.list_view.scrollTo(idx, QListView.PositionAtBottom)

    def _on_native_scroll_value_changed(self, _value: int) -> None:
        if time.monotonic() < self._suppress_native_scroll_until:
            return
        self._native_scroll_sync_timer.start(80)

    def _send_native_top_key_to_web(self) -> None:
        key = self.left_pane.top_visible_key()
        if not key:
            return
        self._ignore_web_scroll_events_until = time.monotonic() + 0.45
        script = (
            "(function(){"
            "if(window.__chatgptMirror && typeof window.__chatgptMirror.scrollToKey==='function'){"
            f"window.__chatgptMirror.scrollToKey({json.dumps(key)});"
            "}"
            "})();"
        )
        self.web_view.page().runJavaScript(script)


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
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
