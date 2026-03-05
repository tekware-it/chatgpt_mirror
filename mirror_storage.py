"""Persistence helpers for profile paths, tab manifests, and offline snapshots.

This module owns the on-disk layout used by the app:
- Qt WebEngine profile/cache directories
- `data/tabs.json` manifest (open tabs + active tab)
- one SQLite snapshot per tab containing native messages and cached thumbnails
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QStandardPaths, QUrl

IMAGE_BYTES_CACHE: Dict[str, bytes] = {}


def ensure_profile_root() -> Path:
    """Return a writable directory for the shared QtWebEngine profile and cache."""
    app_data_root = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not app_data_root:
        app_data_root = str(Path.home() / ".local" / "share" / "chatgpt_mirror")
    profile_root = Path(app_data_root)
    profile_root.mkdir(parents=True, exist_ok=True)
    (profile_root / "qtwebengine").mkdir(parents=True, exist_ok=True)
    (profile_root / "qtwebengine-cache").mkdir(parents=True, exist_ok=True)
    return profile_root


def ensure_data_root() -> Path:
    """Return a writable app data directory for manifests and SQLite snapshots.

    This must not live next to the executable/module because packaged apps
    (especially AppImage on Linux and macOS app bundles) are often mounted or
    installed read-only. We therefore use the OS-specific user data directory.

    A best-effort migration from the legacy project-local `./data` directory is
    attempted only when the destination has not been initialized yet.
    """
    app_data_root = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not app_data_root:
        app_data_root = str(Path.home() / ".local" / "share" / "chatgpt_mirror")

    root = Path(app_data_root) / "offline"
    tabs_dir = root / "tabs"
    tabs_dir.mkdir(parents=True, exist_ok=True)

    # Best-effort one-time migration from the old project-local data directory.
    legacy_root = Path(__file__).resolve().parent / "data"
    legacy_manifest = legacy_root / "tabs.json"
    if legacy_root.exists() and legacy_root != root and not (root / "tabs.json").exists():
        try:
            if legacy_manifest.exists():
                (root / "tabs.json").write_bytes(legacy_manifest.read_bytes())
            legacy_tabs_dir = legacy_root / "tabs"
            if legacy_tabs_dir.exists():
                for src in legacy_tabs_dir.glob("*.sqlite*"):
                    dst = tabs_dir / src.name
                    if not dst.exists():
                        dst.write_bytes(src.read_bytes())
        except Exception:
            # Migration is non-critical: the app can start with an empty store.
            pass

    return root


class OfflineStore:
    """Read/write tab manifests and per-tab offline snapshots.

    `tab_id` is the stable identity used by the app.
    `db_file` is the human-readable SQLite filename shown to the user and stored in the manifest.
    """
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tabs_dir = root / "tabs"
        self.manifest_path = root / "tabs.json"
        self.tabs_dir.mkdir(parents=True, exist_ok=True)

    def _safe_db_stem(self, value: str) -> str:
        v = re.sub(r"[^a-zA-Z0-9._ -]+", "_", (value or "").strip())
        v = re.sub(r"\s+", " ", v).strip().strip(". ")
        v = v.replace("/", "_")
        if len(v) > 90:
            v = v[:90].rstrip()
        return v or "chatgpt"

    def _normalize_db_file_name(self, db_file: str) -> str:
        name = Path((db_file or "").strip()).name
        if not name:
            name = "tab.sqlite"
        if not name.lower().endswith(".sqlite"):
            name += ".sqlite"
        return re.sub(r"[^a-zA-Z0-9._ -]+", "_", name)

    def _extract_title_stem_from_db_file(self, db_file: str) -> str:
        """Return canonical title stem from '<title>__<timestamp>[__suffix].sqlite'."""
        stem = Path((db_file or "").strip()).stem
        if not stem:
            return ""
        # Drop optional collision suffix first: __abcdef
        m = re.match(r"^(.*)__([0-9a-f]{6})$", stem, flags=re.IGNORECASE)
        if m:
            stem = (m.group(1) or "").strip()
        # Drop timestamp part: __YYYYMMDD-HHMMSS
        m = re.match(r"^(.*)__([0-9]{8}-[0-9]{6})$", stem)
        if m:
            stem = (m.group(1) or "").strip()
        return self._safe_db_stem(stem)

    def _find_latest_db_name_by_stem(self, stem: str) -> Optional[str]:
        """Find the most recently modified snapshot filename for a given title stem."""
        stem = self._safe_db_stem(stem or "")
        if not stem:
            return None
        matches = []
        for path in self.tabs_dir.glob("*.sqlite"):
            try:
                cur_stem = self._extract_title_stem_from_db_file(path.name)
            except Exception:
                cur_stem = ""
            if cur_stem != stem:
                continue
            try:
                mtime = path.stat().st_mtime
            except Exception:
                mtime = 0.0
            matches.append((mtime, path.name))
        if not matches:
            return None
        matches.sort(key=lambda it: it[0], reverse=True)
        return matches[0][1]

    def db_path_for_tab(self, tab_id: str, db_file: Optional[str] = None) -> Path:
        if db_file:
            return self.tabs_dir / self._normalize_db_file_name(db_file)
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", (tab_id or "").strip()) or "tab"
        return self.tabs_dir / f"{safe}.sqlite"

    def suggest_db_file_name(self, *, title: str, url: str, saved_at: Optional[float] = None) -> str:
        ts = time.localtime(saved_at or time.time())
        stamp = time.strftime("%Y%m%d-%H%M%S", ts)
        raw_title = (title or "").strip()
        if " - " in raw_title:
            left, right = raw_title.rsplit(" - ", 1)
            if right.strip().lower() == "chatgpt" and left.strip():
                raw_title = left.strip()
        if not raw_title or raw_title.lower() == "chatgpt":
            raw_title = "chatgpt"
            try:
                q = QUrl(url or "")
                if q.isValid() and q.host():
                    tail = q.path().strip("/").split("/")[-1] if q.path().strip("/") else ""
                    raw_title = tail or q.host()
            except Exception:
                pass
        stem = self._safe_db_stem(raw_title)
        return f"{stem}__{stamp}.sqlite"

    def _rename_db_files(self, old_path: Path, new_path: Path) -> None:
        if old_path.resolve() == new_path.resolve():
            return
        if new_path.exists():
            suffix = hashlib.sha1(str(time.time()).encode("utf-8")).hexdigest()[:6]
            new_path = new_path.with_name(f"{new_path.stem}__{suffix}{new_path.suffix}")
        for side in ("", "-wal", "-shm"):
            src = Path(str(old_path) + side)
            dst = Path(str(new_path) + side)
            if src.exists():
                try:
                    src.replace(dst)
                except Exception:
                    pass

    def _connect(self, tab_id: str, db_file: Optional[str] = None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path_for_tab(tab_id, db_file)))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("CREATE TABLE IF NOT EXISTS page_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
              order_idx INTEGER NOT NULL,
              msg_key TEXT PRIMARY KEY,
              role TEXT NOT NULL,
              parts_json TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              collapsed INTEGER NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS image_cache (
              url TEXT PRIMARY KEY,
              data BLOB NOT NULL,
              updated_at REAL NOT NULL
            )
            """
        )
        return conn

    def save_manifest(self, payload: dict) -> None:
        """Persist the lightweight tab list/order/current-index manifest."""
        body = {
            "version": 1,
            "current_index": int(payload.get("current_index") or 0),
            "tabs": payload.get("tabs") or [],
            "saved_at": time.time(),
        }
        self.manifest_path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_manifest(self) -> dict:
        """Load the tab manifest with a defensive fallback for malformed data."""
        if not self.manifest_path.exists():
            return {"version": 1, "current_index": 0, "tabs": []}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "current_index": 0, "tabs": []}
        if not isinstance(data, dict):
            return {"version": 1, "current_index": 0, "tabs": []}
        tabs = data.get("tabs")
        return {
            "version": 1,
            "current_index": int(data.get("current_index") or 0),
            "tabs": tabs if isinstance(tabs, list) else [],
        }

    def save_tab_snapshot(
        self,
        tab_id: str,
        *,
        url: str,
        title: str,
        tab_title: str = "",
        messages: List["Message"],
        db_file: Optional[str] = None,
    ) -> str:
        """Persist a full native snapshot for one tab and return the chosen SQLite filename.

        The snapshot stores:
        - page metadata (`url`, page title, tab title)
        - ordered native messages (role + structured parts)
        - cached image bytes used by the native image renderer
        """
        now = time.time()
        # Use only persisted tab title for naming. Avoid URL/title fallbacks that can
        # generate noisy UUID-like filenames for transient ChatGPT routes.
        current_name = self._normalize_db_file_name(db_file or "") if db_file else ""
        current_stem = self._extract_title_stem_from_db_file(current_name) if current_name else ""
        naming_title = (tab_title or "").strip()
        if naming_title.lower() in {"chatgpt", "chatgpt.com", "chat"}:
            naming_title = ""
        if not naming_title:
            # Keep existing stem when title is missing/generic, avoid noisy renames.
            naming_title = current_stem or "chat"

        desired_stem = self._safe_db_stem(naming_title)
        desired_name = self.suggest_db_file_name(title=desired_stem, url="", saved_at=now)

        # Reuse the existing DB for the same title stem (current tab file first,
        # otherwise any latest file found on disk). Create a new DB only for new stems.
        target_name = ""
        if current_name and current_stem and current_stem == desired_stem:
            target_name = current_name
        if not target_name:
            existing_same_stem = self._find_latest_db_name_by_stem(desired_stem)
            if existing_same_stem:
                target_name = existing_same_stem
        if not target_name:
            target_name = desired_name
            target_path = self.db_path_for_tab(tab_id, target_name)
            if target_path.exists():
                suffix = hashlib.sha1(str(time.time()).encode("utf-8")).hexdigest()[:6]
                target_name = f"{target_path.stem}__{suffix}{target_path.suffix}"

        conn = self._connect(tab_id, target_name)
        try:
            with conn:
                conn.execute("DELETE FROM page_state")
                conn.executemany(
                    "INSERT INTO page_state(key, value) VALUES(?, ?)",
                    [
                        ("url", url or ""),
                        ("title", title or ""),
                        ("tab_title", tab_title or title or ""),
                        ("saved_at", str(now)),
                        ("schema", "1"),
                    ],
                )

                conn.execute("DELETE FROM messages")
                msg_rows = []
                image_urls: List[str] = []
                seen_imgs = set()
                for idx, m in enumerate(messages):
                    parts_payload = []
                    for p in m.parts:
                        item = {"type": p.type}
                        if p.type == "text":
                            item["text"] = p.text
                        elif p.type == "code":
                            item["code"] = p.code
                            item["lang"] = p.lang
                        elif p.type == "image":
                            item["src"] = p.image_url
                            item["alt"] = p.alt
                            item["kind"] = p.image_kind
                            u = (p.image_url or "").strip()
                            if u and u not in seen_imgs:
                                seen_imgs.add(u)
                                image_urls.append(u)
                        parts_payload.append(item)
                    parts_json = json.dumps(parts_payload, ensure_ascii=False, separators=(",", ":"))
                    msg_rows.append(
                        (
                            idx,
                            m.key,
                            m.role,
                            parts_json,
                            hashlib.sha1((m.role + "\n" + parts_json).encode("utf-8")).hexdigest(),
                            1 if m.collapsed else 0,
                            now,
                        )
                    )
                if msg_rows:
                    conn.executemany(
                        "INSERT INTO messages(order_idx, msg_key, role, parts_json, content_hash, collapsed, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        msg_rows,
                    )

                conn.execute("DELETE FROM image_cache")
                img_rows = []
                for u in image_urls:
                    data = IMAGE_BYTES_CACHE.get(u)
                    if data:
                        img_rows.append((u, sqlite3.Binary(data), now))
                if img_rows:
                    conn.executemany(
                        "INSERT INTO image_cache(url, data, updated_at) VALUES (?, ?, ?)",
                        img_rows,
                    )
        finally:
            conn.close()
        return target_name

    def load_tab_page_state(self, tab_id: str, db_file: Optional[str] = None) -> Optional[dict]:
        """Load only page metadata for one tab snapshot (no messages, no image blobs)."""
        db_path = self.db_path_for_tab(tab_id, db_file)
        if not db_path.exists():
            return None
        conn = self._connect(tab_id, db_path.name)
        try:
            page = {k: v for k, v in conn.execute("SELECT key, value FROM page_state")}
            return page
        finally:
            conn.close()

    def load_tab_snapshot(
        self,
        tab_id: str,
        db_file: Optional[str] = None,
        *,
        preload_images: bool = True,
        preload_image_limit: Optional[int] = None,
    ) -> Optional[dict]:
        """Load one tab snapshot and optionally warm the shared in-memory image cache.

        Notes:
        - Loading many image BLOBs can block the UI for large chats.
        - `preload_images=False` skips BLOB hydration entirely.
        - `preload_image_limit` bounds the number of image rows copied to memory.
        """
        db_path = self.db_path_for_tab(tab_id, db_file)
        if not db_path.exists():
            return None
        conn = self._connect(tab_id, db_path.name)
        try:
            page = {k: v for k, v in conn.execute("SELECT key, value FROM page_state")}
            messages = []
            for row in conn.execute(
                "SELECT order_idx, msg_key, role, parts_json, collapsed FROM messages ORDER BY order_idx ASC"
            ):
                _, msg_key, role, parts_json, collapsed = row
                try:
                    parts = json.loads(parts_json or "[]")
                except Exception:
                    parts = []
                messages.append(
                    {
                        "key": str(msg_key or ""),
                        "role": str(role or "assistant"),
                        "parts": parts if isinstance(parts, list) else [],
                        "collapsed": bool(collapsed),
                    }
                )
            if preload_images:
                if preload_image_limit is None:
                    img_iter = conn.execute("SELECT url, data FROM image_cache")
                else:
                    img_iter = conn.execute(
                        "SELECT url, data FROM image_cache LIMIT ?",
                        (max(0, int(preload_image_limit)),),
                    )
                for url, data in img_iter:
                    if isinstance(url, str) and data:
                        try:
                            IMAGE_BYTES_CACHE[url] = bytes(data)
                        except Exception:
                            pass
            return {"page_state": page, "messages": messages}
        finally:
            conn.close()
