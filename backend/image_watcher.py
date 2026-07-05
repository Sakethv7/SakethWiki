"""
Folder watcher for screenshot inbox.

Watches ~/SakethVault/inbox/images/ for new image files.
When images land (after a 5s debounce), base64-encodes them and
submits them to the /ingest pipeline just like a manual upload.

Start via image_watcher.start() — runs as a daemon thread.
"""

import base64
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sakethwiki.watcher")

VAULT_PATH   = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
WATCH_DIR    = VAULT_PATH / "inbox" / "images"
DEBOUNCE_SEC = 5
SUPPORTED    = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MEDIA_TYPES  = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}


def _encode(path: Path) -> dict:
    media_type = MEDIA_TYPES.get(path.suffix.lower(), "image/png")
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"data": data, "mediaType": media_type}


class _ScreenshotHandler:
    def __init__(self):
        self._pending: set = set()
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def on_new_file(self, path: Path):
        if path.suffix.lower() not in SUPPORTED:
            return
        with self._lock:
            self._pending.add(str(path))
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SEC, self._flush)
            self._timer.start()
        logger.info(f"[watcher] queued {path.name}, flushing in {DEBOUNCE_SEC}s")

    def _flush(self):
        with self._lock:
            batch = list(self._pending)
            self._pending.clear()
            self._timer = None

        if not batch:
            return

        # Import here to avoid circular import at module load time
        from main import _extract_image_uncertainties, _tavily_search, _extract_with_sonnet
        import queue_manager, vault_reader, uuid
        from datetime import datetime

        images = []
        for p in sorted(batch):
            try:
                images.append(_encode(Path(p)))
            except Exception as e:
                logger.warning(f"[watcher] failed to encode {p}: {e}")

        if not images:
            return

        logger.info(f"[watcher] processing {len(images)} image(s) from inbox")
        try:
            existing_pages = [p["name"] for p in vault_reader.list_concept_pages()]
            uncertainties = _extract_image_uncertainties(images, "")
            search_context = _tavily_search(uncertainties) if uncertainties else ""
            extraction = _extract_with_sonnet(
                search_context, images, "", existing_pages
            )

            item_id = str(uuid.uuid4())
            item = {
                "id": item_id,
                "url": "",
                "source_type": "lecture",
                "user_notes": "",
                "title": extraction["title"],
                "key_concepts": extraction["key_concepts"],
                "summary": extraction["summary"],
                "suggested_page": extraction["suggested_page"],
                "suggested_wikilinks": extraction["suggested_wikilinks"],
                "tags": extraction["tags"],
                "references": extraction.get("references", []),
                "diagram": extraction.get("diagram", ""),
                "staged_at": datetime.now().isoformat(),
                "status": "pending",
                "inbox_source": [Path(p).name for p in batch],
            }
            queue_manager.enqueue(item)
            logger.info(f"[watcher] queued item {item_id}: {item['title']}")
        except Exception as e:
            logger.error(f"[watcher] pipeline failed: {e}")


_handler = _ScreenshotHandler()
_watch_thread: Optional[threading.Thread] = None


def _watch_loop():
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler

    class _FSHandler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                _handler.on_new_file(Path(event.src_path))

        def on_moved(self, event):
            # Catches files moved/saved into the folder (e.g. from Downloads)
            if not event.is_directory:
                _handler.on_new_file(Path(event.dest_path))

    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    observer = Observer()
    observer.schedule(_FSHandler(), str(WATCH_DIR), recursive=False)
    observer.start()
    logger.info(f"[watcher] watching {WATCH_DIR}")
    try:
        while True:
            time.sleep(1)
    except Exception:
        observer.stop()
    observer.join()


def start():
    global _watch_thread
    if _watch_thread and _watch_thread.is_alive():
        return
    _watch_thread = threading.Thread(target=_watch_loop, daemon=True, name="image-watcher")
    _watch_thread.start()
