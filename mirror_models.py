"""Qt model + data classes for the native mirrored conversation view."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QSize, Qt


def preview_from_markdown(markdown_text: str) -> str:
    """Generate a compact single-line preview from markdown text."""
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
    """One structured message part extracted from the ChatGPT DOM."""
    type: str
    text: str = ""
    code: str = ""
    lang: str = ""
    image_url: str = ""
    alt: str = ""
    image_kind: str = ""


@dataclass
class Message:
    """Native message representation used by the left pane and exports."""
    key: str
    role: str
    parts: List[MessagePart]
    collapsed: bool = False
    size_hint: QSize = field(default_factory=lambda: QSize(640, 120))

    def role_label(self) -> str:
        """Return the user-facing role label used in badges and exports."""
        return "You" if self.role == "user" else "Assistant"

    def preview_text(self, limit: int = 120) -> str:
        """Build a short preview mixing text/code/image markers."""
        chunks: List[str] = []
        for part in self.parts:
            if part.type == "text" and part.text.strip():
                chunks.append(preview_from_markdown(part.text.strip()))
            elif part.type == "code" and part.code.strip():
                lang = part.lang.strip()
                head = f"[code:{lang}] " if lang else "[code] "
                chunks.append(head + part.code.strip().splitlines()[0])
            elif part.type == "image" and part.image_url.strip():
                kind = (part.image_kind or "").strip()
                prefix = "[gallery image]" if kind == "gallery" else "[image]"
                chunks.append(prefix + " " + (part.alt.strip() or part.image_url.strip()))
        text = " ".join(chunks).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."

    def as_copy_text(self) -> str:
        """Serialize a message to clipboard-friendly markdown-like text."""
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
    """Ordered message store backing the virtualized native list view."""
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
        """Apply incremental JS extractor updates without resetting the whole model."""
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
                        parts.append(MessagePart(type="code", code=code, lang=str(item.get("lang") or "")))
                elif ptype == "image":
                    src = str(item.get("src") or item.get("url") or "").strip()
                    if src:
                        parts.append(
                            MessagePart(
                                type="image",
                                image_url=src,
                                alt=str(item.get("alt") or ""),
                                image_kind=str(item.get("kind") or ""),
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
