"""本地知识库（RAG）v1：解析 → 切片 → BM25 检索，并为向量检索预留接口。

设计约束：
- **不碰 `agent/`**：表由 backend 自己建/读写，与 `custom_models` 同一套做法；
- **零新增运行时依赖**：PDF/Word 解析才需要 `pypdf` / `python-docx`，缺库时**懒导入**并给出
  人话报错（`pip install pypdf python-docx`）；`.txt/.md` 开箱即用；
- **检索双模**：配了 `embedding` 就用「0.7 余弦 + 0.3 BM25」混合，没配就纯 BM25；
  BM25 用「中文双字 + 英文单词」分词的纯 Python 实现，几千片完全够；
- 解析/向量化在后台线程跑，`kb_docs.status` 反映阶段，前端轮询。
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from agent.storage import utc_now

#: 切片：500 字符目标长度 + 100 字符重叠（重叠避免答案被切断）
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
#: 单个文件上限（字节）与页数上限
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 500
#: 一次返回的最大片段数与单片段最大字符（喂给模型的上下文预算）
DEFAULT_TOP_K = 4
MAX_CONTEXT_CHARS = 2000

STATUS_PARSING = "parsing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_docs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL DEFAULT 0,
    pages       INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'keyword',
    error       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    TEXT NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,
    page      INTEGER NOT NULL DEFAULT 0,
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);
"""


class KnowledgeError(Exception):
    """知识库业务错误（映射成 400）。"""


@dataclass(frozen=True)
class KnowledgeDoc:
    id: str
    name: str
    size: int
    pages: int
    chunk_count: int
    status: str
    mode: str
    error: str | None
    created_at: str


@dataclass(frozen=True)
class SearchHit:
    doc_name: str
    page: int
    text: str
    score: float


# ---------------------------------------------------------------- 解析
def _require(module: str, hint: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - 取决于是否装了库
        raise KnowledgeError(f"缺少依赖 {module}，请先安装：{hint}") from exc


def parse_pdf(data: bytes) -> list[tuple[int, str]]:
    """返回 [(页码从 1 开始, 该页文本)]。"""
    pypdf = _require("pypdf", "pip install pypdf")
    import io

    reader = pypdf.PdfReader(io.BytesIO(data))
    pages: list[tuple[int, str]] = []
    for index, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - 单页解析失败不该毁掉整篇
            text = ""
        if text.strip():
            pages.append((index, text))
    return pages


def parse_docx(data: bytes) -> list[tuple[int, str]]:
    """Word 没有页的概念，用「段落序号」当页码。"""
    docx = _require("python-docx", "pip install python-docx")
    import io

    document = docx.Document(io.BytesIO(data))
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            blocks.append(paragraph.text)
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                blocks.append(" | ".join(cells))
    if not blocks:
        return []
    # 每 40 个块算一"页"，纯粹为了 metadata 可读
    pages: list[tuple[int, str]] = []
    for start in range(0, len(blocks), 40):
        pages.append((start // 40 + 1, "\n".join(blocks[start : start + 40])))
    return pages


def parse_text(data: bytes) -> list[tuple[int, str]]:
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        return []
    return [(1, text)]


PARSERS: dict[str, Callable[[bytes], list[tuple[int, str]]]] = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".txt": parse_text,
    ".md": parse_text,
    ".markdown": parse_text,
    ".csv": parse_text,
    ".json": parse_text,
    ".py": parse_text,
    ".log": parse_text,
}

#: 允许上传的扩展名（前端 accept 与后端校验共用一份）
SUPPORTED_SUFFIXES = tuple(PARSERS)


# ---------------------------------------------------------------- 切片
def _split_long(text: str, size: int, overlap: int) -> Iterator[str]:
    """把超长段落切开，尽量落在换行/句末。"""
    start = 0
    length = len(text)
    while start < length:
        end = min(start + size, length)
        if end < length:
            window = text[start:end]
            # 在窗口后半段找最近的自然断点
            for separator in ("\n\n", "\n", "。", "！", "？", ". ", "; ", "；"):
                position = window.rfind(separator, size // 2)
                if position != -1:
                    end = start + position + len(separator)
                    break
        piece = text[start:end].strip()
        if piece:
            yield piece
        if end >= length:
            return
        start = max(end - overlap, start + 1)


def chunk_pages(pages: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """按 500/100 切片，返回 [(页码, 片段)]。先按段落聚合，再用重叠切长段。"""
    chunks: list[tuple[int, str]] = []
    for page_number, page_text in pages:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", page_text) if part.strip()]
        buffer = ""
        for paragraph in paragraphs:
            if len(paragraph) > CHUNK_SIZE:
                if buffer:
                    chunks.extend((page_number, piece) for piece in _split_long(buffer, CHUNK_SIZE, CHUNK_OVERLAP))
                    buffer = ""
                chunks.extend((page_number, piece) for piece in _split_long(paragraph, CHUNK_SIZE, CHUNK_OVERLAP))
                continue
            candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            if len(candidate) <= CHUNK_SIZE:
                buffer = candidate
            else:
                chunks.extend((page_number, piece) for piece in _split_long(buffer, CHUNK_SIZE, CHUNK_OVERLAP))
                # 重叠：把上一段尾巴带进新 buffer
                tail = buffer[-CHUNK_OVERLAP:] if len(buffer) > CHUNK_OVERLAP else buffer
                buffer = f"{tail}\n\n{paragraph}"
        if buffer:
            chunks.extend((page_number, piece) for piece in _split_long(buffer, CHUNK_SIZE, CHUNK_OVERLAP))
    return chunks


# ---------------------------------------------------------------- 检索
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """英文/数字用整词，中文用**双字**（bigram）。

    刻意不保留单字：中文里「的」「是」这类高频单字会让任何问题都微弱命中任何文档，
    BM25 的区分度会被噪声淹没（实测「量子色动力学」能命中「报销制度」就是因为「的」）。
    单字查询（只有一个汉字）才退回单字。
    """
    lowered = (text or "").lower()
    tokens: list[str] = [word for word in _WORD_RE.findall(lowered)]
    cjk = _CJK_RE.findall(lowered)
    if len(cjk) == 1:
        tokens.append(cjk[0])
    tokens.extend(first + second for first, second in zip(cjk, cjk[1:]))
    return tokens


def bm25_scores(
    query: str, documents: Sequence[str], *, k1: float = 1.5, b: float = 0.75
) -> list[float]:
    """标准 BM25（文档集 = 当前全部片段）。"""
    query_tokens = tokenize(query)
    if not query_tokens or not documents:
        return [0.0] * len(documents)

    doc_tokens = [tokenize(document) for document in documents]
    doc_lengths = [len(tokens) for tokens in doc_tokens]
    average_length = sum(doc_lengths) / max(len(doc_lengths), 1)

    frequencies: list[dict[str, int]] = []
    for tokens in doc_tokens:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        frequencies.append(counts)

    document_frequency: dict[str, int] = {}
    for counts in frequencies:
        for token in set(counts):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    total_documents = len(documents)

    scores: list[float] = []
    for index, counts in enumerate(frequencies):
        score = 0.0
        length = doc_lengths[index] or 1
        for token in query_tokens:
            tf = counts.get(token, 0)
            if not tf:
                continue
            df = document_frequency.get(token, 0)
            idf = math.log(1 + (total_documents - df + 0.5) / (df + 0.5))
            denominator = tf + k1 * (1 - b + b * length / (average_length or 1))
            score += idf * tf * (k1 + 1) / (denominator or 1)
        scores.append(score)
    return scores


def cosine(first: Sequence[float], second: Sequence[float]) -> float:
    if not first or not second or len(first) != len(second):
        return 0.0
    dot = sum(a * b for a, b in zip(first, second))
    norm_a = math.sqrt(sum(a * a for a in first))
    norm_b = math.sqrt(sum(b * b for b in second))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class KnowledgeStore:
    """知识库的 SQLite 存储 + 检索。线程内使用（后台线程各自 new 一个）。"""

    def __init__(self, db_path: str | Path, *, embedding_provider: Any = None) -> None:
        self.db_path = Path(db_path)
        self._embedder = embedding_provider  # 预留：Provider 实例（需有 embed 方法）
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- 生命周期 ----
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def mode(self) -> str:
        return "hybrid" if self._embedder is not None else "keyword"

    # ---- 文档 ----
    def add_document(self, filename: str, data: bytes) -> KnowledgeDoc:
        """同步解析+切片+入库（调用方负责放到后台线程）。"""
        suffix = Path(filename).suffix.lower()
        if suffix not in PARSERS:
            raise KnowledgeError(
                f"不支持的文件类型 {suffix or '(无扩展名)'}；支持：{', '.join(SUPPORTED_SUFFIXES)}"
            )
        if len(data) > MAX_FILE_BYTES:
            raise KnowledgeError(f"文件过大（{len(data) / 1024 / 1024:.1f}MB），上限 10MB")
        if not data:
            raise KnowledgeError("文件是空的")

        doc_id = uuid.uuid4().hex[:12]
        created = utc_now()
        with self._conn:
            self._conn.execute(
                "INSERT INTO kb_docs (id, name, size, pages, chunk_count, status, mode, error, created_at)"
                " VALUES (?, ?, ?, 0, 0, ?, ?, NULL, ?)",
                (doc_id, filename, len(data), STATUS_PARSING, self.mode, created),
            )

        self._process(doc_id, filename, data)
        return self.get_document(doc_id)

    def _process(self, doc_id: str, filename: str, data: bytes) -> None:
        """解析 → 切片 → 向量（可选）→ 入库；失败时把状态写成 failed。"""
        suffix = Path(filename).suffix.lower()
        try:
            pages = PARSERS[suffix](data)
            if not pages:
                raise KnowledgeError("没有解析出任何文本（可能是扫描版 PDF，需要 OCR）")
            pieces = chunk_pages(pages)
            if not pieces:
                raise KnowledgeError("切片结果为空")
            embeddings = self._embed_chunks([piece for _, piece in pieces])
            with self._conn:
                for index, (page_number, piece) in enumerate(pieces):
                    self._conn.execute(
                        "INSERT INTO kb_chunks (doc_id, page, idx, text, embedding)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            doc_id,
                            page_number,
                            index,
                            piece,
                            embeddings[index] if embeddings else None,
                        ),
                    )
                self._conn.execute(
                    "UPDATE kb_docs SET pages = ?, chunk_count = ?, status = ?, error = NULL"
                    " WHERE id = ?",
                    (len(pages), len(pieces), STATUS_READY, doc_id),
                )
        except Exception as exc:  # noqa: BLE001 - 失败要落库成状态，而不是让线程静默死掉
            with self._conn:
                self._conn.execute(
                    "UPDATE kb_docs SET status = ?, error = ? WHERE id = ?",
                    (STATUS_FAILED, str(exc)[:300], doc_id),
                )
            raise

    def _embed_chunks(self, texts: Sequence[str]) -> list[bytes] | None:
        """有 embedding provider 时算向量（JSON 存 BLOB）；没有就返回 None。"""
        if self._embedder is None:
            return None
        try:
            vectors = self._embedder.embed(list(texts))
        except Exception:  # noqa: BLE001 - 向量化失败就退化成纯关键词，不让文档不可用
            return None
        return [json.dumps(vector).encode("utf-8") for vector in vectors]

    def list_documents(self) -> list[KnowledgeDoc]:
        rows = self._conn.execute(
            "SELECT * FROM kb_docs ORDER BY created_at DESC"
        ).fetchall()
        return [self._row_to_doc(row) for row in rows]

    def get_document(self, doc_id: str) -> KnowledgeDoc:
        row = self._conn.execute("SELECT * FROM kb_docs WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise KnowledgeError(f"找不到文档 {doc_id}")
        return self._row_to_doc(row)

    def delete_document(self, doc_id: str) -> KnowledgeDoc:
        document = self.get_document(doc_id)
        with self._conn:
            self._conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM kb_docs WHERE id = ?", (doc_id,))
        return document

    def stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS docs, COALESCE(SUM(chunk_count), 0) AS chunks FROM kb_docs"
            " WHERE status = ?",
            (STATUS_READY,),
        ).fetchone()
        return {"docs": int(row["docs"]), "chunks": int(row["chunks"]), "mode": self.mode}

    # ---- 检索 ----
    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> list[SearchHit]:
        text = (query or "").strip()
        if not text:
            return []
        rows = self._conn.execute(
            "SELECT c.text, c.page, c.embedding, d.name AS doc_name"
            " FROM kb_chunks c JOIN kb_docs d ON d.id = c.doc_id"
            " WHERE d.status = ?",
            (STATUS_READY,),
        ).fetchall()
        if not rows:
            return []

        documents = [row["text"] for row in rows]
        keyword_scores = bm25_scores(text, documents)

        vector_scores = [0.0] * len(rows)
        has_vectors = any(row["embedding"] for row in rows)
        if self._embedder is not None and has_vectors:
            try:
                query_vector = self._embedder.embed([text])[0]
                vector_scores = [
                    cosine(query_vector, json.loads(row["embedding"])) if row["embedding"] else 0.0
                    for row in rows
                ]
            except Exception:  # noqa: BLE001 - 向量失败就纯关键词
                vector_scores = [0.0] * len(rows)

        keyword_max = max(keyword_scores) or 1.0
        combined = [
            0.7 * vector + 0.3 * (keyword / keyword_max)
            if has_vectors and self._embedder is not None
            else keyword / keyword_max
            for vector, keyword in zip(vector_scores, keyword_scores)
        ]

        order = sorted(range(len(rows)), key=lambda index: combined[index], reverse=True)
        hits: list[SearchHit] = []
        for index in order[: max(1, int(top_k))]:
            if combined[index] <= 0:
                continue
            hits.append(
                SearchHit(
                    doc_name=rows[index]["doc_name"],
                    page=int(rows[index]["page"]),
                    text=documents[index],
                    score=round(float(combined[index]), 4),
                )
            )
        return hits

    def search_as_context(self, query: str, top_k: int = DEFAULT_TOP_K) -> str:
        """给模型看的格式化结果（带来源，超预算就截断）。"""
        hits = self.search(query, top_k)
        if not hits:
            return "知识库中没有找到相关内容。（提示：知识库为空或关键词不匹配）"
        lines = [f"以下是从本地知识库检索到的 {len(hits)} 段内容（按相关度排序）："]
        used = 0
        for index, hit in enumerate(hits, start=1):
            block = f"\n[{index}] 来源：{hit.doc_name}（第 {hit.page} 页）\n{hit.text}"
            if used + len(block) > MAX_CONTEXT_CHARS:
                remaining = MAX_CONTEXT_CHARS - used
                if remaining > 80:
                    lines.append(block[:remaining] + "…（已截断）")
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines)

    # ---- 内部 ----
    @staticmethod
    def _row_to_doc(row: sqlite3.Row) -> KnowledgeDoc:
        return KnowledgeDoc(
            id=row["id"],
            name=row["name"],
            size=int(row["size"]),
            pages=int(row["pages"]),
            chunk_count=int(row["chunk_count"]),
            status=row["status"],
            mode=row["mode"],
            error=row["error"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------- 工具
def build_search_tool(service: "KnowledgeService") -> Any:
    """构造 `search_knowledge_base` 的 ToolSpec（注入方式，不改 agent 的 KNOWN_TOOLS）。

    每次调用现开一个 ``KnowledgeStore``（一次 sqlite 连接），避免把连接跨线程共享。
    """
    from agent.registry import ToolSpec

    def handler(query: str, top_k: int = DEFAULT_TOP_K) -> str:
        try:
            return service.search_as_context(str(query), int(top_k))
        except Exception as exc:  # noqa: BLE001 - 工具失败要作为"错误：…"回给模型
            return f"错误：知识库检索失败（{type(exc).__name__}）"

    return ToolSpec(
        name="search_knowledge_base",
        description=(
            "检索用户上传到本地知识库的文档（PDF / Word / Markdown / 文本等）并返回最相关的原文片段。"
            "凡是用户要求「解析/总结/查找/回答」某个上传文档的内容，或问题里提到文档、资料、附件、报告、"
            "手册、说明书时，都必须先调用本工具；不要用 list_dir / read_file 去猜文件路径（上传的文档"
            "不在 workspace 里）。返回结果带来源文件名与页码，可直接引用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词或问题，尽量具体"},
                "top_k": {
                    "type": "integer",
                    "description": f"返回片段数，默认 {DEFAULT_TOP_K}，最多 8",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )


class KnowledgeService:
    """知识库门面：后台线程跑解析，供路由与 AgentService 共用。"""

    def __init__(self, db_path: str | Path, *, embedding_provider: Any = None) -> None:
        self.db_path = Path(db_path)
        self._embedding_provider = embedding_provider
        self._threads: dict[str, threading.Thread] = {}

    def _store(self) -> KnowledgeStore:
        return KnowledgeStore(self.db_path, embedding_provider=self._embedding_provider)

    def submit(self, filename: str, data: bytes) -> KnowledgeDoc:
        """先落一条 parsing 记录并返回，再在后台线程里解析（前端轮询状态）。"""
        with self._store() as store:
            suffix = Path(filename).suffix.lower()
            if suffix not in PARSERS:
                raise KnowledgeError(
                    f"不支持的文件类型 {suffix or '(无扩展名)'}；支持：{', '.join(SUPPORTED_SUFFIXES)}"
                )
            if len(data) > MAX_FILE_BYTES:
                raise KnowledgeError(f"文件过大（{len(data) / 1024 / 1024:.1f}MB），上限 10MB")

            doc_id = uuid.uuid4().hex[:12]
            with store._conn:
                store._conn.execute(
                    "INSERT INTO kb_docs (id, name, size, pages, chunk_count, status, mode, error,"
                    " created_at) VALUES (?, ?, ?, 0, 0, ?, ?, NULL, ?)",
                    (doc_id, filename, len(data), STATUS_PARSING, store.mode, utc_now()),
                )
            document = store.get_document(doc_id)

        def worker() -> None:
            try:
                with self._store() as bg_store:
                    bg_store._process(doc_id, filename, data)
            except Exception:  # noqa: BLE001 - 失败状态已落库
                pass
            finally:
                self._threads.pop(doc_id, None)

        thread = threading.Thread(target=worker, name=f"kb-{doc_id}", daemon=True)
        self._threads[doc_id] = thread
        thread.start()
        return document

    def list_documents(self) -> list[KnowledgeDoc]:
        with self._store() as store:
            return store.list_documents()

    def delete_document(self, doc_id: str) -> KnowledgeDoc:
        with self._store() as store:
            return store.delete_document(doc_id)

    def stats(self) -> dict[str, Any]:
        with self._store() as store:
            return store.stats()

    def search_as_context(self, query: str, top_k: int = DEFAULT_TOP_K) -> str:
        with self._store() as store:
            return store.search_as_context(query, top_k)

    def wait_for(self, doc_id: str, timeout: float = 30.0) -> None:
        """仅测试/脚本用：等后台线程结束。"""
        thread = self._threads.get(doc_id)
        if thread is not None:
            thread.join(timeout)