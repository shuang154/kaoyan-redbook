import gzip
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

HOST = "127.0.0.1"  # 仅本机监听，由 Nginx 反向代理对外暴露
PORT = 8888
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "vocab_shuffled.json")
DB_FILE = os.path.join(BASE_DIR, "redbook.db")

ALLOWED_STATIC_EXTS = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".ico", ".woff2"}
COMPRESS_EXTS = {".html", ".css", ".js", ".svg", ".json"}
MAX_POST_BYTES = 2 * 1024 * 1024


def natural_sort_key(text: str):
    """自然排序：确保 Unit2 排在 Unit10 前面"""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", text)]


# 例句翻译的内存缓存：同一句只请求一次谷歌翻译
TRANSLATE_CACHE = {}
TRANSLATE_CACHE_LOCK = threading.Lock()


def google_translate(text: str, sl: str = "en", tl: str = "zh-CN") -> str:
    """调用谷歌免费翻译接口（无密钥），返回译文"""
    key = (text, sl, tl)
    with TRANSLATE_CACHE_LOCK:
        cached = TRANSLATE_CACHE.get(key)
        if cached is not None:
            return cached

    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl={quote(sl)}&tl={quote(tl)}&dt=t&q={quote(text)}"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})
    with urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", "replace")

    data = json.loads(raw)
    segments = data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []
    translation = "".join(str(seg[0]) for seg in segments if seg and seg[0])

    with TRANSLATE_CACHE_LOCK:
        if len(TRANSLATE_CACHE) >= 4096:
            TRANSLATE_CACHE.clear()
        TRANSLATE_CACHE[key] = translation
    return translation


class ProgressDB:
    """单用户 SQLite 数据库：持久化掌握状态、生词本和偏好设置"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.write_lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS word_progress (
                    word TEXT PRIMARY KEY,
                    mastered INTEGER NOT NULL DEFAULT 0,
                    notebook INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        print(f"[ProgressDB] 数据库就绪: {self.db_path}", flush=True)

    def get_all(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT word, mastered, notebook FROM word_progress WHERE mastered = 1 OR notebook = 1"
            ).fetchall()
            setting_rows = conn.execute("SELECT key, value FROM app_settings").fetchall()

        mastered = [r["word"] for r in rows if r["mastered"] == 1]
        notebook = [r["word"] for r in rows if r["notebook"] == 1]
        settings = {}
        for r in setting_rows:
            try:
                settings[r["key"]] = json.loads(r["value"])
            except Exception:
                settings[r["key"]] = r["value"]

        return {
            "mastered": mastered,
            "notebook": notebook,
            "settings": settings,
        }

    def toggle_word(self, word: str, field: str, value: bool):
        if field not in ("mastered", "notebook"):
            raise ValueError("field 只能是 mastered 或 notebook")
        word = word.strip()
        if not word:
            raise ValueError("word 不能为空")

        int_val = 1 if value else 0
        with self.write_lock, self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO word_progress (word, {field}, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(word) DO UPDATE SET
                    {field} = excluded.{field},
                    updated_at = CURRENT_TIMESTAMP
                """,
                (word, int_val),
            )
            # 清理两个状态都为 0 的空记录，保持数据库精简
            conn.execute(
                "DELETE FROM word_progress WHERE word = ? AND mastered = 0 AND notebook = 0",
                (word,),
            )
            conn.commit()

    def batch_import(self, mastered_list, notebook_list, mode: str = "merge"):
        """批量导入进度：mode='merge' 合并，mode='replace' 覆盖"""
        with self.write_lock, self._connect() as conn:
            if mode == "replace":
                conn.execute("DELETE FROM word_progress")

            for w in mastered_list:
                w = str(w).strip()
                if w:
                    conn.execute("""
                        INSERT INTO word_progress (word, mastered, updated_at)
                        VALUES (?, 1, CURRENT_TIMESTAMP)
                        ON CONFLICT(word) DO UPDATE SET
                            mastered = 1,
                            updated_at = CURRENT_TIMESTAMP
                    """, (w,))

            for w in notebook_list:
                w = str(w).strip()
                if w:
                    conn.execute("""
                        INSERT INTO word_progress (word, notebook, updated_at)
                        VALUES (?, 1, CURRENT_TIMESTAMP)
                        ON CONFLICT(word) DO UPDATE SET
                            notebook = 1,
                            updated_at = CURRENT_TIMESTAMP
                    """, (w,))
            conn.commit()

    def save_settings(self, updates: dict):
        with self.write_lock, self._connect() as conn:
            for k, v in updates.items():
                conn.execute(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (str(k), json.dumps(v, ensure_ascii=False)),
                )
            conn.commit()


class VocabStore:
    """词库内存索引，支持按单元异步加载 + JSON 变动自动热重载"""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.lock = threading.RLock()
        self.mtime = 0.0
        self.etag = ""
        self.all_words = []
        self.unit_cache = {}
        self.word_map = {}
        self.meta_raw = b"{}"
        self.meta_gzip = b""
        self.reload_if_needed(force=True)

    def reload_if_needed(self, force: bool = False):
        if not os.path.isfile(self.filepath):
            raise FileNotFoundError(f"找不到词库文件: {self.filepath}")

        current_mtime = os.path.getmtime(self.filepath)
        if not force and current_mtime == self.mtime:
            return

        with self.lock:
            current_mtime = os.path.getmtime(self.filepath)
            if not force and current_mtime == self.mtime:
                return

            with open(self.filepath, "rb") as f:
                raw_content = f.read()

            data = json.loads(raw_content.decode("utf-8"))
            file_hash = hashlib.md5(raw_content).hexdigest()[:12]

            units_dict = {}
            category_order = {}
            word_map = {}
            for item in data:
                page = item.get("page", "未分类")
                cat = re.split(r"unit|\d", page, maxsplit=1, flags=re.IGNORECASE)[0] or page
                if cat not in category_order:
                    category_order[cat] = len(category_order)
                units_dict.setdefault(page, []).append(item)
                w = str(item.get("word", "")).strip().lower()
                if w and w not in word_map:
                    word_map[w] = item

            def page_sort_key(p: str):
                cat = re.split(r"unit|\d", p, maxsplit=1, flags=re.IGNORECASE)[0] or p
                return (category_order.get(cat, 999), natural_sort_key(p))

            sorted_pages = sorted(units_dict.keys(), key=page_sort_key)

            meta_units = []
            unit_cache = {}
            for page in sorted_pages:
                words_in_unit = units_dict[page]
                meta_units.append({
                    "page": page,
                    "count": len(words_in_unit),
                    "words": [w.get("word", "") for w in words_in_unit],
                })
                raw_u = json.dumps(words_in_unit, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                gz_u = gzip.compress(raw_u, compresslevel=6)
                unit_cache[page] = (raw_u, gz_u)

            meta_payload = {
                "version": file_hash,
                "total_words": len(data),
                "total_units": len(sorted_pages),
                "units": meta_units,
            }
            meta_raw = json.dumps(meta_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            meta_gzip = gzip.compress(meta_raw, compresslevel=6)

            self.mtime = current_mtime
            self.etag = f'"{file_hash}"'
            self.all_words = data
            self.unit_cache = unit_cache
            self.word_map = word_map
            self.meta_raw = meta_raw
            self.meta_gzip = meta_gzip

            print(
                f"[VocabStore] 词库已加载: {len(data)} 词 / {len(sorted_pages)} 单元 | "
                f"Meta 压缩后 {len(meta_gzip)/1024:.1f} KB",
                flush=True,
            )

    def search(self, query: str, limit: int = 60):
        q = query.strip().lower()
        if not q:
            return {"query": query, "total": 0, "results": []}

        exact, prefix, contains = [], [], []
        for item in self.all_words:
            w = str(item.get("word", "")).lower()
            m = str(item.get("meaning", "")).lower()
            if w == q:
                exact.append(item)
            elif w.startswith(q):
                prefix.append(item)
            elif q in w or q in m:
                contains.append(item)

        merged = exact + prefix + contains
        return {"query": query, "total": len(merged), "results": merged[:limit]}

    def get_words_by_list(self, words_list):
        results = []
        seen = set()
        for w in words_list:
            key = str(w).strip().lower()
            if key and key not in seen and key in self.word_map:
                seen.add(key)
                results.append(self.word_map[key])
        return results


DB = ProgressDB(DB_FILE)
STORE = VocabStore(DATA_FILE)
STATIC_CACHE = {}


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class RedBookHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        print(f"[HTTP] {self.address_string()} - {format % args}", flush=True)

    def _accepts_gzip(self) -> bool:
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if length <= 0 or length > MAX_POST_BYTES:
            raise ValueError("请求体大小不合法")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_bytes(
        self,
        payload: bytes,
        content_type: str,
        status: int = 200,
        is_gzip: bool = False,
        cache_control: str = "no-cache",
        etag: str = None,
    ):
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        if is_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, obj, status: int = 200, cache_control: str = "no-store"):
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        use_gz = self._accepts_gzip() and len(raw) > 512
        payload = gzip.compress(raw, compresslevel=6) if use_gz else raw
        self._send_bytes(
            payload,
            content_type="application/json; charset=utf-8",
            status=status,
            is_gzip=use_gz,
            cache_control=cache_control,
        )

    def do_GET(self):
        try:
            STORE.reload_if_needed()
        except Exception as e:
            self._send_json({"error": f"词库加载失败: {e}"}, status=500)
            return

        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        # 1. 目录元数据（首屏秒开）
        if path == "/api/meta":
            use_gz = self._accepts_gzip()
            self._send_bytes(
                STORE.meta_gzip if use_gz else STORE.meta_raw,
                content_type="application/json; charset=utf-8",
                is_gzip=use_gz,
                cache_control="public, max-age=300",
                etag=STORE.etag,
            )
            return

        # 2. 按单元按需加载
        if path.startswith("/api/unit/"):
            unit_name = path[len("/api/unit/"):]
            if unit_name not in STORE.unit_cache:
                self._send_json({"error": f"未找到单元: {unit_name}"}, status=404)
                return
            raw_u, gz_u = STORE.unit_cache[unit_name]
            use_gz = self._accepts_gzip()
            self._send_bytes(
                gz_u if use_gz else raw_u,
                content_type="application/json; charset=utf-8",
                is_gzip=use_gz,
                cache_control="public, max-age=86400",
                etag=STORE.etag,
            )
            return

        # 3. 全局搜索
        if path == "/api/search":
            q = query.get("q", [""])[0]
            try:
                limit = min(max(int(query.get("limit", ["60"])[0]), 1), 200)
            except ValueError:
                limit = 60
            self._send_json(STORE.search(q, limit=limit), cache_control="public, max-age=60")
            return

        # 4. 读取数据库里的个人全部进度和设置
        if path == "/api/progress":
            self._send_json(DB.get_all(), cache_control="no-store")
            return

        # 5. 例句翻译代理（前端展开真题例句时才懒加载调用）
        if path == "/api/translate":
            text = (query.get("q", [""])[0] or "").strip()
            if not text:
                self._send_json({"error": "缺少 q 参数"}, status=400)
                return
            if len(text) > 2000:
                self._send_json({"error": "文本过长"}, status=400)
                return
            sl = query.get("sl", ["en"])[0]
            tl = query.get("tl", ["zh-CN"])[0]
            try:
                translation = google_translate(text, sl=sl, tl=tl)
                self._send_json(
                    {"translation": translation},
                    cache_control="public, max-age=86400",
                )
            except Exception as e:
                print(f"[Translate] 失败: {e}", flush=True)
                self._send_json({"error": str(e), "translation": ""}, status=502)
            return

        # 6. 静态文件托管（index.html）
        self._serve_static(path)

    def do_POST(self):
        try:
            STORE.reload_if_needed()
        except Exception as e:
            self._send_json({"error": f"词库加载失败: {e}"}, status=500)
            return

        path = unquote(urlsplit(self.path).path)

        try:
            body = self._read_json_body()
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)
            return

        # 1. 切换单个词的「已掌握 / 生词本」状态并写入数据库
        if path == "/api/progress/toggle":
            try:
                DB.toggle_word(
                    word=str(body.get("word", "")),
                    field=str(body.get("type", "")),
                    value=bool(body.get("value", False)),
                )
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, status=400)
            return

        # 2. 批量导入进度到数据库
        if path == "/api/progress/batch":
            mastered = body.get("mastered", [])
            notebook = body.get("notebook", [])
            mode = body.get("mode", "merge")
            if not isinstance(mastered, list) or not isinstance(notebook, list):
                self._send_json({"error": "mastered 和 notebook 必须是数组"}, status=400)
                return
            DB.batch_import(mastered, notebook, mode=mode)
            self._send_json({"ok": True, **DB.get_all()})
            return

        # 3. 保存偏好设置（如深色模式、上次背到的单元）
        if path == "/api/settings":
            if not isinstance(body, dict):
                self._send_json({"error": "请求体必须是对象"}, status=400)
                return
            DB.save_settings(body)
            self._send_json({"ok": True})
            return

        # 4. 按单词列表批量取完整词条（给生词本/未掌握列表用）
        if path == "/api/words":
            words_list = body.get("words", [])
            if not isinstance(words_list, list):
                self._send_json({"error": "words 必须是数组"}, status=400)
                return
            matched = STORE.get_words_by_list(words_list)
            self._send_json({"total": len(matched), "words": matched})
            return

        self._send_json({"error": "Not Found"}, status=404)

    def _serve_static(self, req_path: str):
        if req_path == "/" or req_path == "":
            req_path = "/index.html"

        rel_path = req_path.lstrip("/")
        full_path = os.path.abspath(os.path.join(BASE_DIR, rel_path))
        ext = os.path.splitext(full_path)[1].lower()

        if (
            not full_path.startswith(BASE_DIR + os.sep)
            or not os.path.isfile(full_path)
            or ext not in ALLOWED_STATIC_EXTS
        ):
            self._send_json({"error": "File Not Found"}, status=404)
            return

        mime_type, _ = mimetypes.guess_type(full_path)
        if not mime_type:
            mime_type = "application/octet-stream"
        if ext in {".html", ".js", ".css"}:
            mime_type += "; charset=utf-8"

        mtime = os.path.getmtime(full_path)
        use_gz = self._accepts_gzip() and ext in COMPRESS_EXTS
        cache_key = (full_path, mtime, use_gz)

        if cache_key in STATIC_CACHE:
            data = STATIC_CACHE[cache_key]
        else:
            with open(full_path, "rb") as f:
                raw = f.read()
            data = gzip.compress(raw, compresslevel=6) if use_gz else raw
            for k in list(STATIC_CACHE.keys()):
                if k[0] == full_path and k[2] == use_gz:
                    del STATIC_CACHE[k]
            STATIC_CACHE[cache_key] = data

        self._send_bytes(
            data,
            content_type=mime_type,
            is_gzip=use_gz,
            cache_control="no-cache",
            etag=f'"{int(mtime)}-{len(data)}"',
        )


if __name__ == "__main__":
    server = ReusableThreadingHTTPServer((HOST, PORT), RedBookHandler)
    print(f"红宝书服务已启动（SQLite + 按需加载）: http://{HOST}:{PORT}", flush=True)
    server.serve_forever()
