"""JavaScript injector used to mirror ChatGPT DOM into structured deltas.

This module is intentionally isolated so selector updates and extractor fixes can be
maintained without scrolling through the desktop UI code.
"""

from __future__ import annotations

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

    function imagePartFromImg(imgEl, opts) {
      opts = opts || {};
      if (!(imgEl instanceof Element)) return null;
      // Images embedded inside inline rich-entity text (often inside <strong> in list items)
      // break markdown markers if extracted as standalone parts. Skip them for now.
      var inlineAnc = imgEl.closest && imgEl.closest('strong, b, em, i, a, li, p');
      if (inlineAnc) return null;
      var src = (imgEl.currentSrc || imgEl.getAttribute('src') || '').trim();
      if (!src) return null;
      if (/^data:image\//i.test(src) && src.length > 2_000_000) return null;
      if (imgEl.closest && imgEl.closest('[data-testid*="webpage-citation-pill"]')) return null;
      if (!opts.allowButtonAncestor && imgEl.closest && imgEl.closest('button,[role="button"]')) return null;
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
        alt: (imgEl.getAttribute('alt') || '').trim(),
        kind: (opts.kind || (inRichEntityImage ? 'rich-entity' : 'image'))
      };
    }

    function imagePartsFromGalleryContainer(el) {
      if (!(el instanceof Element)) return null;
      var tag = (el.tagName || '').toLowerCase();
      if (tag !== 'div') return null;
      var cls = classStr(el);
      // ChatGPT "rich gallery" blocks are usually horizontal flex/no-scrollbar strips with image buttons.
      if (!(cls.includes('no-scrollbar') || cls.includes('overflow-auto') || cls.includes('flex-nowrap'))) {
        return null;
      }
      var imgs = el.querySelectorAll('button img, [role="button"] img');
      if (!imgs || imgs.length < 2) return null;
      var out = [];
      var seenSrc = new Set();
      for (var i = 0; i < Math.min(imgs.length, 12); i++) {
        var part = imagePartFromImg(imgs[i], { allowButtonAncestor: true, kind: 'gallery' });
        if (!part) continue;
        if (seenSrc.has(part.src)) continue;
        seenSrc.add(part.src);
        out.push(part);
      }
      return out.length ? out : null;
    }

    function imagePartFromListItemRichEntity(liEl) {
      if (!(liEl instanceof Element)) return null;
      var rich = liEl.querySelector('[data-rich-entity-image="true"] img');
      if (!(rich instanceof Element)) return null;
      var src = (rich.currentSrc || rich.getAttribute('src') || '').trim();
      if (!src) return null;
      var alt = (rich.getAttribute('alt') || '').trim();
      return { type: 'image', src: src, alt: alt, kind: 'rich-entity' };
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

      var galleryParts = imagePartsFromGalleryContainer(el);
      if (galleryParts) {
        flushText();
        for (var gp = 0; gp < galleryParts.length; gp++) {
          parts.push(galleryParts[gp]);
        }
        ensureLineBreak();
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
        if (srcI) cleaned.push({ type: 'image', src: srcI, alt: (p.alt || ''), kind: (p.kind || '') });
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

  function getTopVisibleInfo() {
    var nodes = Array.prototype.slice.call(document.querySelectorAll('[data-cgm-message-key]'));
    if (!nodes.length) return { key: '', progress: 0 };
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
    if (!best) return { key: '', progress: 0 };
    var key = best.getAttribute('data-cgm-message-key') || '';
    var br = best.getBoundingClientRect();
    var h = Math.max(1, Math.round(br.height || 1));
    var progress = Math.max(0, Math.min(1, (topBound - br.top) / h));
    return { key: key, progress: progress };
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
    state.uiReadySent = false;
    state.pending = false;
    state.lastScanAt = 0;
    state.lastTopKeySent = '';
    state.lastTopProgressSent = -1;
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
      var info = getTopVisibleInfo();
      var key = info && info.key ? info.key : '';
      var progress = info && typeof info.progress === 'number' ? info.progress : 0;
      if (!key) return;
      var progressChanged = Math.abs(progress - (state.lastTopProgressSent || 0)) >= 0.04;
      if (key === state.lastTopKeySent && !progressChanged) return;
      state.lastTopKeySent = key;
      state.lastTopProgressSent = progress;
      if (state.scrollSyncDebug) {
        sendEvent({
          type: 'scroll_debug',
          dir: 'web->native',
          stage: 'emit_top_key',
          key: key,
          progress: progress,
          reason: reason || 'web_scroll'
        });
      }
      sendEvent({ type: 'scroll_top_key', key: key, progress: progress, reason: reason || 'web_scroll' });
    }

    function isUiReady() {
      var selectors = [
        'textarea',
        '[contenteditable="true"][data-testid*="composer"]',
        '[data-testid*="composer"] textarea',
        'form textarea',
        '#prompt-textarea'
      ];
      for (var i = 0; i < selectors.length; i++) {
        try {
          var n = document.querySelector(selectors[i]);
          if (n && isVisibleish(n)) return true;
        } catch (e) {}
      }
      return false;
    }

    function emitUiReadyIfNeeded(reason) {
      if (state.uiReadySent) return;
      if (!isUiReady()) return;
      state.uiReadySent = true;
      sendEvent({
        type: 'ui_ready',
        reason: reason || 'scan',
        title: (document && document.title) ? String(document.title) : ''
      });
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
      emitUiReadyIfNeeded(reason);
      return 'scan:' + reason + ':nodes=' + nodes.length + ':deltas=' + deltas.length;
    }

    function scheduleScan(reason) {
      if (state.pending) return;
      state.pending = true;
      setTimeout(function() { scanNow(reason || 'throttled'); }, 250);
    }

    state.scheduleScan = scheduleScan;
    state.scanNow = scanNow;
    state.scrollToKey = function(key, progress) {
      if (!key) return false;
      progress = Number(progress || 0);
      if (!isFinite(progress)) progress = 0;
      progress = Math.max(0, Math.min(1, progress));
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
      try { topKeyBefore = (getTopVisibleInfo().key || ''); } catch (e) {}
      var scroller = getScrollContainer(node);
      var nodeRect0 = node.getBoundingClientRect();
      var nodeH = Math.max(1, Math.round(nodeRect0.height || 1));
      var offsetInsideNode = Math.round(progress * nodeH);
      if (scroller === document.body || scroller === document.documentElement || scroller === document.scrollingElement) {
        var top = node.getBoundingClientRect().top + (window.scrollY || window.pageYOffset || 0) + offsetInsideNode;
        window.scrollTo({ top: Math.max(0, top - 8), behavior: 'auto' });
      } else {
        var scRect = scroller.getBoundingClientRect();
        var target = node.getBoundingClientRect().top - scRect.top + scroller.scrollTop + offsetInsideNode - 8;
        scroller.scrollTop = Math.max(0, target);
      }
      if (state.scrollSyncDebug) {
        var topKeyAfter = '';
        try { topKeyAfter = (getTopVisibleInfo().key || ''); } catch (e) {}
        sendEvent({
          type: 'scroll_debug',
          dir: 'native->web',
          stage: 'scroll_to_key',
          targetKey: String(key),
          targetProgress: progress,
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
