# RepoPilot 长期语义记忆升级方案

> 版本: v1.0  
> 日期: 2026-07-04  
> 作者: RepoPilot Team  
> 状态: 设计评审

---

## 1. 架构概览

### 1.1 现状问题

当前 RepoPilot 记忆层 (`src/memory/repo_store.py`) 只有两个 SQLite per-repo 表：

| 表 | 字段 | 用途 |
|---|---|---|
| `file_index` | owner, repo, path, fix_count, last_used | 记录哪个文件被修过几次 |
| `issue_log` | owner, repo, issue_number, success, created_at | 记录 issue 处理成功/失败 |

**缺失能力**：没有语义记忆。Agent 每次遇到相似错误都从零开始推理，无法复用历史修复经验。

### 1.2 升级目标

在 **PLAN 阶段**注入「相似历史修复案例」作为 Few-Shot 示例，让 LLM 知道：
- ✅ **成功案例** → "类似问题是这样修的，参考这个模式"
- ❌ **失败案例（标注 FAILURE）** → "这个方向走不通，避免重蹈覆辙"

### 1.3 新增模块全景

```
┌──────────────────────────────────────────────────────────────┐
│                     RepoPilot v2 Memory Layer                 │
├──────────────────────────────────────────────────────────────┤
│  src/memory/                                                  │
│  ├── repo_store.py          (已有) per-repo file_index +      │
│  │                               issue_log                    │
│  ├── error_episode_store.py (新增) 全局语义记忆存储            │
│  ├── embedding.py           (新增) BGE embedding 模型封装      │
│  ├── vector_index.py        (新增) 向量索引抽象层              │
│  │   ├── sqlite_vec_index.py    (方案A: sqlite-vec)           │
│  │   └── faiss_index.py         (方案B: FAISS, 备选)          │
│  └── keyframe.py            (新增) Traceback 关键帧提取        │
│                                                               │
│  存储:                                                        │
│  ~/.repopilot/memory/episodes.db  ← 全局 SQLite (error_       │
│                                     episode 表 + 向量表)       │
│  ~/.repopilot/memory/<owner>/<repo>/memory.db  ← 已有 per-repo│
└──────────────────────────────────────────────────────────────┘
```

### 1.4 核心数据流

```
┌──────────┐    ┌───────────┐    ┌───────────┐    ┌──────────┐
│ execute  │───▶│ extract_  │───▶│ embed_    │───▶│ insert_  │
│ /verify  │    │ keyframe  │    │ error_log │    │ episode  │
│ (run结束)│    │ (traceback│    │ (384-dim) │    │ + upsert │
│          │    │  裁剪)    │    │           │    │  vector  │
└──────────┘    └───────────┘    └───────────┘    └────┬─────┘
                                                       │
  ┌────────────────────────────────────────────────────┘
  │  下次 PLAN 时
  ▼
┌──────────┐    ┌───────────┐    ┌───────────┐    ┌──────────┐
│  PLAN    │◀───│ format_   │◀───│ vector    │◀───│ embed_   │
│  phase   │    │ few_shot  │    │ search    │    │ error_log│
│          │    │ (top-3    │    │ top-3     │    │ (当前     │
│          │    │  examples)│    │ similar   │    │  issue)  │
└──────────┘    └───────────┘    └───────────┘    └──────────┘
```

---

## 2. 数据表 DDL

### 2.1 全局语义记忆表 (error_episode)

**存储位置**: `~/.repopilot/memory/episodes.db`（全局单文件，跨 repo 共享）

```sql
-- 错误修复案例主表
CREATE TABLE IF NOT EXISTS error_episode (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 关键帧提取后的 error_log (≤ 2KB)
    error_keyframe  TEXT NOT NULL,
    -- 原始 error_log（完整版，仅保存最近 10KB 供调试，不参与向量化）
    error_log_raw   TEXT DEFAULT '',
    -- 修复后的代码（diff 或 patch 内容）
    patch_content   TEXT DEFAULT '',
    -- Issue 标题 + 正文摘要
    issue_text      TEXT DEFAULT '',
    -- 所属仓库 (格式: owner/repo)
    repo            TEXT NOT NULL DEFAULT '',
    -- 是否成功 (1=成功, 0=失败)
    success         INTEGER NOT NULL DEFAULT 0,
    -- 失败类型标记 (为空表示成功)
    failure_kind    TEXT DEFAULT '',
    -- 创建时间
    created_at      TEXT DEFAULT (datetime('now')),
    -- 元数据 JSON: {trace_id, issue_number, file_paths, ...}
    metadata        TEXT DEFAULT '{}'
);

-- 加速按 repo 过滤 + 最近优先
CREATE INDEX IF NOT EXISTS idx_episode_repo_success
    ON error_episode(repo, success, created_at DESC);

-- 加速按时间召回最近案例
CREATE INDEX IF NOT EXISTS idx_episode_created
    ON error_episode(created_at DESC);
```

### 2.2 向量索引表 (方案 A: sqlite-vec)

**依赖**: `pip install sqlite-vec`（纯 Python wheel，无外部 C 依赖）

```sql
-- sqlite-vec 虚拟表，存储 384 维 float32 向量
-- sqlite-vec 通过 Python API 创建，非原生 DDL
-- 等价于:
-- CREATE VIRTUAL TABLE episode_vec USING vec0(
--     embedding FLOAT[384]
-- );

-- 通过 Python 创建:
-- import sqlite_vec
-- db = sqlite3.connect("episodes.db")
-- db.enable_load_extension(True)
-- sqlite_vec.load(db)
-- db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episode_vec USING vec0(embedding FLOAT[384])")
```

### 2.3 与现有 per-repo memory.db 的关系

```
~/.repopilot/memory/
├── episodes.db                    ← 新增: 全局语义记忆 (跨 repo)
├── {owner}/
│   └── {repo}/
│       └── memory.db              ← 已有: per-repo file_index + issue_log
│           ├── file_index
│           └── issue_log
```

**设计原则**:
- `error_episode` 存全局 `episodes.db`——语义记忆跨 repo 共享
- `file_index` / `issue_log` 保持 per-repo `memory.db`——文件级统计是 repo-specific
- `error_episode.repo` 字段仍保留，支持「仅本 repo」或「跨 repo」两种召回模式

---

## 3. 向量索引选型对比

### 3.1 方案概览

| 维度 | 方案 A: sqlite-vec | 方案 B: FAISS (IndexFlatIP) |
|---|---|---|
| **定位** | SQLite 扩展，虚拟表查询 | 内存向量索引库 |
| **安装** | `pip install sqlite-vec` (wheel, ~2MB) | `pip install faiss-cpu` (~30MB) |
| **存储** | 向量存在 SQLite 文件中，持久化 | 纯内存，需自己序列化/反序列化 |
| **检索算法** | 暴力全表扫描 (exact KNN) | 也支持暴力 (IndexFlatIP)，但可升级到 IVF/HNSW |
| **性能 (2000条)** | ~2ms (暴力，384维 × 2000行) | ~0.3ms (SIMD 加速) |
| **性能 (5000条)** | ~5ms (线性增长) | ~0.5ms (仍很快) |
| **增量插入** | INSERT + 自动更新 vec0 表 | add() 到 Index，但需手动管理 |
| **运维复杂度** | **零**——数据即 SQLite，备份即 cp | 需处理索引重建、崩溃恢复、序列化 |
| **与现有架构契合** | ✅ 高度契合，现有代码已是 SQLite 体系 | ⚠️ 引入新的存储+索引管理范式 |
| **新增依赖** | `sqlite-vec` (~2MB wheel) | `faiss-cpu` (~30MB) + `numpy` |

### 3.2 性能实测估算

| 数据量 | sqlite-vec (暴力) | FAISS IndexFlatIP | FAISS IVF256 |
|---|---|---|---|
| 500 条 | ~0.5ms | ~0.1ms | ~0.05ms |
| 2,000 条 | ~2ms | ~0.3ms | ~0.1ms |
| 5,000 条 | ~5ms | ~0.5ms | ~0.15ms |
| 10,000 条 | ~10ms | ~1ms | ~0.2ms |

**关键判断**: 在 2000-5000 条场景下，2-5ms 的检索延迟完全可以接受（LLM 调用本身需要 20-140s，2ms 是零头）。方案 A 的零运维优势远超方案 B 的微秒级性能优势。

### 3.3 增量插入策略对比

**方案 A (sqlite-vec)**:
```python
# 写入 episode + 向量一气呵成，事务保证一致性
async def insert_episode(episode, embedding):
    async with db.execute("BEGIN"):
        cur = await db.execute(
            "INSERT INTO error_episode (...) VALUES (...)",
            params
        )
        episode_id = cur.lastrowid
        # vec0 表自动关联 rowid
        await db.execute(
            "INSERT INTO episode_vec (rowid, embedding) VALUES (?, ?)",
            (episode_id, embedding.tobytes())
        )
```
✅ 事务原子性、崩溃安全、无需额外序列化

**方案 B (FAISS)**:
```python
# 需要维护: 内存 Index + 磁盘序列化 + ID 映射
class FAISSEpisodeIndex:
    def __init__(self):
        self.index = faiss.IndexFlatIP(384)
        self.id_map = []  # 映射 faiss 内部 ID → episode DB ID
    
    def add(self, episode_id, embedding):
        self.index.add(embedding.reshape(1, -1))
        self.id_map.append(episode_id)
        self._save_checkpoint()  # 需要自己实现
    
    def _save_checkpoint(self):
        faiss.write_index(self.index, "episodes.faiss")
        json.dump(self.id_map, open("id_map.json", "w"))
```
⚠️ 崩溃可能丢失未持久化的索引条目、需要周期性全量重建

### 3.4 推荐结论

**✅ 推荐方案 A: sqlite-vec**

理由：
1. **零运维**: 数据即 SQLite，备份一个文件，崩溃恢复靠 WAL，无额外状态管理
2. **架构契合**: 现有代码已有 `aiosqlite` + `RepoStore` 模式，新增 `EpisodeStore` 自然扩展
3. **性能够用**: 2000-5000 条场景下 2-5ms，PLAN 阶段总耗时 20-140s，完全可忽略
4. **依赖最小**: `sqlite-vec` 是纯 Python wheel (~2MB)，vs FAISS 需要 ~30MB + numpy
5. **可降级**: 如果未来数据量突破 10000 条，可以在 `sqlite-vec` 外再包一层 FAISS 缓存

**备选**: 保留方案 B 接口抽象（见下文 `VectorIndex` 基类），未来可插拔切换。

---

## 4. 关键帧提取算法

### 4.1 设计目标

将原始 traceback（可能几百 KB）压缩到 ≤ 2KB，保留语义关键信息：
- ✅ 前 3 级调用栈（定位错误发生位置）
- ✅ 最后一级异常类型 + 消息（知道什么错了）
- ❌ 去除非关键帧（第三方库内部栈帧、重复路径、长参数列表）

### 4.2 算法伪码

```python
import re
from typing import Optional

# 匹配 Python traceback 行:   File "path", line N, in func_name
TRACEBACK_LINE_RE = re.compile(
    r'^\s*File\s+"([^"]+)",\s*line\s+(\d+),\s*in\s+(\S+)'
)
# 匹配异常行:  ExceptionType: message
EXCEPTION_RE = re.compile(
    r'^(\w+(?:\.\w+)*(?:Error|Exception|Warning|Error|Failure))\s*:?\s*(.*)'
)

# 项目源码路径模式，用于过滤掉第三方库栈帧
PROJECT_PATH_PATTERNS = [
    r'/src/', r'/lib/', r'/app/', r'/packages/',
    r'/repopilot/', r'site-packages/repopilot',
]

MAX_KEYFRAME_BYTES = 2048  # 2KB 硬上限


def extract_keyframe(error_log: str, repo_path: str = "") -> str:
    """
    从完整 error_log 提取关键帧。
    
    策略:
    1. 解析 traceback 行
    2. 保留前 3 帧（优先保留项目源码帧而非第三方帧）
    3. 保留最后一级的异常类型 + 消息
    4. 截断到 2KB
    
    Returns:
        压缩后的关键帧文本，适合向量化和 LLM prompt 注入
    """
    lines = error_log.split('\n')
    
    # Step 1: 提取 traceback 帧
    frames = []
    exception_line = None
    exception_message = ""
    
    for line in lines:
        tb_match = TRACEBACK_LINE_RE.match(line)
        if tb_match:
            filepath, lineno, func = tb_match.groups()
            is_project = any(
                re.search(pat, filepath) for pat in PROJECT_PATH_PATTERNS
            )
            frames.append({
                "file": filepath,
                "line": lineno,
                "func": func,
                "is_project": is_project,
                "text": line.strip(),
            })
            continue
        
        exc_match = EXCEPTION_RE.match(line.strip())
        if exc_match and not line.startswith(' '):
            exception_line = exc_match.group(1)
            exception_message = (exc_match.group(2) or "")[:500]
    
    if not frames and not exception_line:
        # 不是标准 traceback，直接截断返回
        return error_log[:MAX_KEYFRAME_BYTES]
    
    # Step 2: 选择前 3 帧（优先项目源码帧）
    project_frames = [f for f in frames if f["is_project"]]
    other_frames = [f for f in frames if not f["is_project"]]
    
    selected = (project_frames + other_frames)[:3]
    
    # Step 3: 构建关键帧文本
    parts = []
    parts.append("=== TRACEBACK (top 3 frames) ===")
    for i, f in enumerate(selected):
        parts.append(f"  [{i+1}] {f['text']}")
    
    if exception_line:
        parts.append(f"=== EXCEPTION ===")
        parts.append(f"  {exception_line}: {exception_message}")
    
    keyframe = '\n'.join(parts)
    
    # Step 4: 硬截断到 2KB
    if len(keyframe.encode('utf-8')) > MAX_KEYFRAME_BYTES:
        # 逐行截断直到 ≤ 2KB
        truncated = []
        size = 0
        for line in parts:
            line_bytes = len(line.encode('utf-8')) + 1  # +1 for \n
            if size + line_bytes > MAX_KEYFRAME_BYTES - 20:
                truncated.append("... (truncated)")
                break
            truncated.append(line)
            size += line_bytes
        keyframe = '\n'.join(truncated)
    
    return keyframe
```

### 4.3 关键帧示例

**输入** (100KB+ 完整 traceback):
```
Traceback (most recent call last):
  File "/app/tests/test_api.py", line 45, in test_create
    response = client.post("/api/users", json=data)
  File "/app/middleware.py", line 23, in __call__
    return self.app(environ, start_response)
  File "/usr/lib/python3.11/site-packages/flask/app.py", line 1500, in full_dispatch_request
    ...
  (中间大量第三方库栈帧)
  ...
  File "/app/db/connection.py", line 87, in execute
    cursor.execute(query, params)
  File "/usr/lib/python3.11/site-packages/psycopg2/extras.py", line 146, in execute
    ...
psycopg2.OperationalError: server closed the connection unexpectedly
	This probably means the server terminated abnormally
	before or while processing the request.
```

**输出** (≤ 2KB):
```
=== TRACEBACK (top 3 frames) ===
  [1] File "/app/tests/test_api.py", line 45, in test_create
  [2] File "/app/middleware.py", line 23, in __call__
  [3] File "/app/db/connection.py", line 87, in execute
=== EXCEPTION ===
  psycopg2.OperationalError: server closed the connection unexpectedly
```

---

## 5. 核心代码接口

### 5.1 embedding.py — 嵌入模型封装

```python
"""BGE-small-en-v1.5 embedding model wrapper.

Lazy-loaded singleton (~130MB), kept in memory for the lifetime of the process.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import torch

logger = logging.getLogger("repopilot.embedding")

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384

_embedder: "Embedder | None" = None


class Embedder:
    """Thin wrapper around sentence-transformers / HuggingFace model."""

    def __init__(self, model_name: str = MODEL_NAME):
        self._model_name = model_name
        self._model: "torch.nn.Module | None" = None
        self._tokenizer: "object | None" = None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Load model into memory (call once at startup)."""
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(
                self._model_name, device="cpu"
            )
            logger.info(
                "Embedding model loaded: %s (dim=%d)",
                self._model_name, EMBEDDING_DIM,
            )
        except ImportError:
            # Fallback: use transformers directly (no sentence-transformers dep)
            from transformers import AutoModel, AutoTokenizer
            import torch
            self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
            self._model = AutoModel.from_pretrained(self._model_name)
            self._model.eval()
            logger.info(
                "Embedding model loaded via transformers: %s (dim=%d)",
                self._model_name, EMBEDDING_DIM,
            )

    def encode(
        self,
        texts: str | list[str],
        *,
        normalize: bool = True,
    ) -> np.ndarray:
        """Encode one or more texts to float32 numpy arrays (384-dim).

        Returns shape (n, 384) for list input, (384,) for string input.
        """
        if self._model is None:
            self.load()

        single = isinstance(texts, str)
        if single:
            texts = [texts]

        # BGE models want "Represent this sentence for searching relevant passages: "
        # prefix for queries, no prefix for documents
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=normalize,
            show_progress_bar=False,
        )

        if single:
            return embeddings[0].astype(np.float32)
        return embeddings.astype(np.float32)

    def query_embed(self, text: str) -> np.ndarray:
        """Encode a query with BGE query prefix."""
        return self.encode(f"Represent this sentence for searching relevant passages: {text}")

    def doc_embed(self, text: str) -> np.ndarray:
        """Encode a document (no prefix needed for BGE)."""
        return self.encode(text)


def get_embedder() -> Embedder:
    """Return the module-level singleton embedder."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
        _embedder.load()
    return _embedder
```

### 5.2 vector_index.py — 向量索引抽象

```python
"""Vector index abstraction layer.

Supports two backends:
  - SQLiteVecIndex  (方案 A, 推荐)
  - FAISSIndex       (方案 B, 备选)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class VectorIndex(ABC):
    """Abstract vector index for error episode semantic search."""

    @abstractmethod
    async def initialize(self) -> None:
        """Create/load the index. Called once at startup."""
        ...

    @abstractmethod
    async def upsert(self, episode_id: int, embedding: np.ndarray) -> None:
        """Insert or update a vector for the given episode."""
        ...

    @abstractmethod
    async def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 3,
        *,
        success_filter: bool | None = None,
        repo_filter: str | None = None,
    ) -> list[tuple[int, float]]:
        """Return top-k (episode_id, similarity_score) tuples.

        Args:
            query_embedding: (384,) float32 query vector
            top_k: number of results
            success_filter: if True, only successful episodes; if False, only failed
            repo_filter: if set, limit to specific repo (owner/repo)
        """
        ...

    @abstractmethod
    async def delete(self, episode_id: int) -> None:
        """Remove a vector from the index."""
        ...

    @abstractmethod
    async def count(self) -> int:
        """Return total vector count in index."""
        ...
```

### 5.3 sqlite_vec_index.py — 方案 A 实现

```python
"""sqlite-vec backed vector index."""

from __future__ import annotations

import logging
import struct

import numpy as np

from .vector_index import VectorIndex

logger = logging.getLogger("repopilot.memory.vector")

EMBEDDING_DIM = 384


class SQLiteVecIndex(VectorIndex):
    """Vector index backed by sqlite-vec virtual table.

    Vectors stored in the same episodes.db as error_episode table,
    joined via rowid.

    Performance (linear scan, exact KNN):
      - 500  records: ~0.5ms
      - 2000 records: ~2ms
      - 5000 records: ~5ms
      - 10000 records: ~10ms
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db = None

    async def initialize(self) -> None:
        import sqlite_vec
        import aiosqlite

        self._db = await aiosqlite.connect(self._db_path)
        await self._db.enable_load_extension(True)

        # Load sqlite-vec extension (synchronous in a thread to not block)
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sqlite_vec.load, self._db)

        # Create virtual table if not exists
        await self._db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episode_vec
            USING vec0(embedding FLOAT[{dim}])
        """.format(dim=EMBEDDING_DIM))
        await self._db.commit()
        logger.info("sqlite-vec index initialized: %s", self._db_path)

    async def upsert(self, episode_id: int, embedding: np.ndarray) -> None:
        assert embedding.shape == (EMBEDDING_DIM,), \
            f"Expected ({EMBEDDING_DIM},), got {embedding.shape}"
        assert embedding.dtype == np.float32

        # sqlite-vec vec0 table: embedding stored as BLOB of float32 little-endian
        blob = embedding.astype(np.float32).tobytes()

        # Upsert: delete old row, insert new
        await self._db.execute(
            "DELETE FROM episode_vec WHERE rowid = ?",
            (episode_id,),
        )
        await self._db.execute(
            "INSERT INTO episode_vec (rowid, embedding) VALUES (?, ?)",
            (episode_id, blob),
        )
        await self._db.commit()

    async def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 3,
        *,
        success_filter: bool | None = None,
        repo_filter: str | None = None,
    ) -> list[tuple[int, float]]:
        blob = query_embedding.astype(np.float32).tobytes()

        # Build query with optional filters joined to error_episode table
        joins = ["JOIN error_episode e ON episode_vec.rowid = e.id"]
        where = []
        params: list = [blob, top_k]

        if success_filter is not None:
            where.append("e.success = ?")
            params.append(1 if success_filter else 0)
        if repo_filter:
            where.append("e.repo = ?")
            params.append(repo_filter)

        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        join_clause = " ".join(joins)

        # sqlite-vec KNN query syntax
        sql = f"""
            SELECT
                episode_vec.rowid as episode_id,
                vec_distance_cosine(episode_vec.embedding, ?) as distance
            FROM episode_vec
            {join_clause}
            {where_clause}
            ORDER BY distance ASC
            LIMIT ?
        """
        params_tuple = tuple(params)

        cursor = await self._db.execute(sql, params_tuple)
        rows = await cursor.fetchall()

        # Convert cosine distance to similarity: 1 - distance
        return [
            (int(row[0]), 1.0 - float(row[1]))
            for row in rows
        ]

    async def delete(self, episode_id: int) -> None:
        await self._db.execute(
            "DELETE FROM episode_vec WHERE rowid = ?",
            (episode_id,),
        )
        await self._db.commit()

    async def count(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM episode_vec")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
```

### 5.4 error_episode_store.py — 全局语义记忆存储

```python
"""Global semantic memory store for error episodes.

Wraps a shared SQLite database (~/.repopilot/memory/episodes.db) with:
  - error_episode table (keyframe, patch, metadata)
  - episode_vec virtual table (384-dim embeddings via sqlite-vec)
  - Atomic episode+vector insertion
  - Semantic search with filters
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite
import numpy as np

from .embedding import get_embedder
from .keyframe import extract_keyframe
from .sqlite_vec_index import SQLiteVecIndex
from .vector_index import VectorIndex

logger = logging.getLogger("repopilot.memory.episode")

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL_EPISODE = """
CREATE TABLE IF NOT EXISTS error_episode (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    error_keyframe  TEXT NOT NULL,
    error_log_raw   TEXT DEFAULT '',
    patch_content   TEXT DEFAULT '',
    issue_text      TEXT DEFAULT '',
    repo            TEXT NOT NULL DEFAULT '',
    success         INTEGER NOT NULL DEFAULT 0,
    failure_kind    TEXT DEFAULT '',
    created_at      TEXT DEFAULT (datetime('now')),
    metadata        TEXT DEFAULT '{}'
)
"""

_DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_episode_repo_success "
    "ON error_episode(repo, success, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_episode_created "
    "ON error_episode(created_at DESC)",
]


def _episode_db_path(base: str = "~/.repopilot/memory") -> Path:
    return Path(base).expanduser() / "episodes.db"


class ErrorEpisodeStore:
    """Global semantic memory store: error episodes + vector index."""

    def __init__(
        self,
        base_path: str = "~/.repopilot/memory",
        vector_index: VectorIndex | None = None,
    ):
        self._db_path = str(_episode_db_path(base_path))
        self._db: aiosqlite.Connection | None = None
        self._vec = vector_index

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open DB, create tables/indexes, initialize vector index."""
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_DDL_EPISODE)
        for idx_ddl in _DDL_INDEXES:
            await self._db.execute(idx_ddl)
        await self._db.commit()

        if self._vec is None:
            self._vec = SQLiteVecIndex(self._db_path)
        await self._vec.initialize()

        # Ensure embedder is loaded (lazy, first use will trigger)
        get_embedder()

        logger.info(
            "ErrorEpisodeStore initialized: %s (%d episodes, %d vectors)",
            self._db_path,
            await self._count_episodes(),
            await self._vec.count(),
        )

    async def close(self) -> None:
        """Close DB connection and vector index."""
        if hasattr(self._vec, 'close'):
            await self._vec.close()
        if self._db:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # write path: run end → episode insertion
    # ------------------------------------------------------------------

    async def record_episode(
        self,
        *,
        error_log: str,
        patch_content: str = "",
        issue_text: str = "",
        repo: str = "",
        success: bool = False,
        failure_kind: str = "",
        metadata: dict | None = None,
        repo_path: str = "",
    ) -> int:
        """Record a fix attempt as a semantic memory episode.

        This is called at the end of a run (success or failure).
        1. Extract keyframe from error_log
        2. Generate embedding from keyframe
        3. Insert episode row
        4. Upsert vector

        Returns:
            episode_id (int)
        """
        # Step 1: keyframe extraction
        keyframe = extract_keyframe(error_log, repo_path=repo_path)

        # Step 2: generate embedding (doc mode, no query prefix)
        embedder = get_embedder()
        try:
            embedding = embedder.doc_embed(keyframe)
        except Exception:
            logger.warning("Failed to embed keyframe, skipping vector", exc_info=True)
            embedding = None

        # Step 3: insert episode
        raw_log = error_log[:10240]  # keep last 10KB of raw log
        meta_json = json.dumps(metadata or {})

        cursor = await self._db.execute(
            """INSERT INTO error_episode
               (error_keyframe, error_log_raw, patch_content, issue_text,
                repo, success, failure_kind, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (keyframe, raw_log, patch_content, issue_text,
             repo, 1 if success else 0, failure_kind, meta_json),
        )
        await self._db.commit()
        episode_id = cursor.lastrowid

        # Step 4: upsert vector (if embedding succeeded)
        if embedding is not None:
            try:
                await self._vec.upsert(episode_id, embedding)
            except Exception:
                logger.warning(
                    "Failed to upsert vector for episode %d", episode_id, exc_info=True
                )

        logger.info(
            "Recorded episode %d: repo=%s success=%s keyframe_len=%d",
            episode_id, repo, success, len(keyframe),
        )
        return episode_id

    # ------------------------------------------------------------------
    # read path: PLAN phase → semantic recall
    # ------------------------------------------------------------------

    async def search_similar(
        self,
        error_log: str,
        issue_text: str = "",
        *,
        repo: str = "",
        top_k: int = 3,
        cross_repo: bool = True,  # True = global recall, False = same repo only
        include_success: bool = True,
        include_failure: bool = True,
        repo_path: str = "",
    ) -> list[dict]:
        """Semantic search for similar historical episodes.

        Called at the start of PLAN phase.

        Args:
            error_log: current issue's error_log (for query embedding)
            issue_text: current issue title + body (for hybrid weighting, future)
            repo: current repo (owner/repo)
            top_k: number of results
            cross_repo: if True, search across all repos; if False, same repo only
            include_success: include successful episodes
            include_failure: include failed episodes

        Returns:
            List of episode dicts sorted by similarity (descending):
            [{id, similarity, keyframe, patch_content, issue_text,
              repo, success, failure_kind, created_at}, ...]
        """
        # Generate query embedding
        keyframe = extract_keyframe(error_log, repo_path=repo_path)
        combined_query = (
            f"error: {keyframe}\nissue: {issue_text[:500]}"
            if issue_text else keyframe
        )
        embedder = get_embedder()
        try:
            query_vec = embedder.query_embed(combined_query)
        except Exception:
            logger.warning("Failed to embed query, returning empty results")
            return []

        # Determine filter
        if not cross_repo and repo:
            repo_filter = repo
        else:
            repo_filter = None

        # Determine success filter
        if include_success and not include_failure:
            success_filter = True
        elif include_failure and not include_success:
            success_filter = False
        else:
            success_filter = None

        # Vector search
        results = await self._vec.search(
            query_vec,
            top_k=top_k,
            success_filter=success_filter,
            repo_filter=repo_filter,
        )

        if not results:
            return []

        # Fetch full episode data
        episode_ids = [r[0] for r in results]
        similarities = {r[0]: r[1] for r in results}

        placeholders = ",".join("?" * len(episode_ids))
        cursor = await self._db.execute(
            f"""SELECT id, error_keyframe, patch_content, issue_text,
                       repo, success, failure_kind, created_at
                FROM error_episode
                WHERE id IN ({placeholders})
                ORDER BY created_at DESC""",
            episode_ids,
        )
        rows = await cursor.fetchall()

        # Re-sort by similarity
        episodes = []
        for row in rows:
            eid = row[0]
            episodes.append({
                "id": eid,
                "similarity": round(similarities.get(eid, 0.0), 4),
                "keyframe": row[1],
                "patch_content": row[2],
                "issue_text": row[3],
                "repo": row[4],
                "success": bool(row[5]),
                "failure_kind": row[6],
                "created_at": row[7],
            })

        episodes.sort(key=lambda e: e["similarity"], reverse=True)
        return episodes

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------

    async def _count_episodes(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) FROM error_episode")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def prune_old(self, keep_recent: int = 5000) -> int:
        """Remove oldest episodes beyond keep_recent limit."""
        cursor = await self._db.execute(
            """DELETE FROM error_episode WHERE id NOT IN
               (SELECT id FROM error_episode ORDER BY created_at DESC LIMIT ?)""",
            (keep_recent,),
        )
        await self._db.commit()
        deleted = cursor.rowcount
        if deleted:
            # Also clean up orphan vectors
            # (sqlite-vec vec0 is rowid-based, so deleting from error_episode
            #  leaves orphans; need explicit cleanup)
            await self._db.execute(
                """DELETE FROM episode_vec WHERE rowid NOT IN
                   (SELECT id FROM error_episode)"""
            )
            await self._db.commit()
        logger.info("Pruned %d old episodes", deleted)
        return deleted


# ---------------------------------------------------------------------------
# module-level convenience (mirrors repo_store.py pattern)
# ---------------------------------------------------------------------------

_store: ErrorEpisodeStore | None = None


def get_episode_store() -> ErrorEpisodeStore:
    global _store
    if _store is None:
        _store = ErrorEpisodeStore()
    return _store


async def initialize_episode_store() -> ErrorEpisodeStore:
    store = get_episode_store()
    await store.initialize()
    return store


async def close_episode_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None
```

---

## 6. PLAN Prompt 升级

### 6.1 注入 Few-Shot 示例的 prompt 模板

在 `plan.py` 的 `plan_fix()` 函数中，在构建 `user` prompt 之前插入语义召回逻辑：

```python
# === 新增: 语义记忆召回 (plan.py 中插入) ===
few_shot_context = ""
if state.fix_attempts:
    # 有失败经验时，用最新的 error_log 做语义搜索
    latest_error = state.fix_attempts[-1].error_log
    from ..memory.error_episode_store import get_episode_store
    store = get_episode_store()
    similar = await store.search_similar(
        error_log=latest_error,
        issue_text=f"{state.issue_title}\n{state.issue_body[:500]}",
        repo=f"{state.owner}/{state.repo}",
        top_k=3,
        cross_repo=True,
    )
    if similar:
        few_shot_context = _format_few_shot_examples(similar)
else:
    # 首次 PLAN，用 issue 文本做语义搜索
    issue_text = f"{state.issue_title}\n{state.issue_body[:500]}"
    from ..memory.error_episode_store import get_episode_store
    store = get_episode_store()
    similar = await store.search_similar(
        error_log=issue_text,
        issue_text=issue_text,
        repo=f"{state.owner}/{state.repo}",
        top_k=3,
        cross_repo=True,
    )
    if similar:
        few_shot_context = _format_few_shot_examples(similar)
# === 注入到 user prompt ===
```

### 6.2 Few-Shot 格式化函数

```python
def _format_few_shot_examples(
    episodes: list[dict],
    max_example_chars: int = 1500,
) -> str:
    """Format historical episodes as Few-Shot examples for the PLAN prompt.

    Successful episodes → "template to follow"
    Failed episodes → "pitfall to avoid (marked FAILURE)"
    """
    lines = [
        "=== HISTORICAL FIX EXAMPLES (few-shot) ===",
        "The following similar issues were fixed (or failed) in the past. "
        "Use successful examples as a template; avoid approaches that failed.",
        "",
    ]

    for i, ep in enumerate(episodes[:3], start=1):
        tag = "✅ SUCCESS" if ep["success"] else "❌ FAILURE — DO NOT REPEAT"
        similarity_pct = f"{ep['similarity'] * 100:.0f}%"

        example = (
            f"--- Example {i} ({tag}, similarity: {similarity_pct}, "
            f"repo: {ep['repo']}) ---\n"
            f"Error pattern:\n{ep['keyframe'][:500]}\n"
        )

        if ep["success"]:
            example += f"\nFix applied (patch):\n{ep['patch_content'][:600]}\n"
        else:
            example += (
                f"\nFAILURE reason: {ep.get('failure_kind', 'unknown')}\n"
                f"Attempted (failed) patch:\n{ep['patch_content'][:400]}\n"
            )

        if ep.get("issue_text"):
            example += f"\nRelated issue: {ep['issue_text'][:200]}\n"

        lines.append(example)

    # Truncate to avoid blowing up prompt
    result = "\n".join(lines)
    if len(result) > max_example_chars:
        result = result[:max_example_chars] + "\n... (few-shot examples truncated)"
    return result
```

### 6.3 注入位置

在 `plan.py` 的 `plan_fix()` 函数第 408 行（`user = (...)` 之前）注入：

```python
# === 原代码 376-418 行的修改 ===
# 在 files_context 之后、user 构建之前插入:

    # --- 语义记忆召回 (新增) ---
    few_shot_context = ""
    try:
        from ..memory.error_episode_store import get_episode_store
        store = get_episode_store()
        search_error = (
            state.fix_attempts[-1].error_log
            if state.fix_attempts
            else f"{state.issue_title}\n{state.issue_body[:800]}"
        )
        similar = await store.search_similar(
            error_log=search_error,
            issue_text=f"{state.issue_title} {state.issue_body[:500]}",
            repo=f"{state.owner}/{state.repo}",
            top_k=3,
            cross_repo=True,
        )
        if similar:
            few_shot_context = _format_few_shot_examples(similar)
            _record_node_diagnostic(
                state,
                node="plan_fix",
                event="semantic_recall",
                status="success",
                elapsed_seconds=0.0,
                similar_count=len(similar),
                top_similarity=similar[0]["similarity"] if similar else 0.0,
            )
    except Exception as exc:
        logger.warning("Semantic recall failed: %s", exc)
        few_shot_context = ""
    # --- 语义记忆召回结束 ---

    user = (
        f"Issue URL: {state.issue_url}\n"
        f"Title: {state.issue_title}\n\nBody:\n"
        f"{_truncate_prompt_text(state.issue_body, PLAN_ISSUE_BODY_LIMIT)}\n\n"
        f"Relevant files:\n{files_context}\n\n"
        # === 新增: 注入 Few-Shot 示例 ===
        f"{few_shot_context}\n\n"
        # === 原有内容继续 ===
        f"Previous failures:\n{previous_failures}"
        f"{reflection_context}"
        f"{hypothesis_continuity_context}"
        f"{context_pressure_context}"
        f"{diversity_context}"
        f"{human_context}"
    )
```

### 6.4 Prompt 注入效果示例

```
Issue URL: https://github.com/org/repo/issues/42
Title: Database connection pool exhausted under load
Body: ...

Relevant files:
FILE: src/db/pool.py
...

=== HISTORICAL FIX EXAMPLES (few-shot) ===
The following similar issues were fixed (or failed) in the past. Use successful examples as a template; avoid approaches that failed.

--- Example 1 (✅ SUCCESS, similarity: 92%, repo: flask-app/db-utils) ---
Error pattern:
=== TRACEBACK (top 3 frames) ===
  [1] File "/app/db/pool.py", line 142, in get_connection
  [2] File "/app/db/pool.py", line 89, in _acquire
  [3] File "/usr/lib/python3/site-packages/psycopg2/pool.py", line 195, in getconn
=== EXCEPTION ===
  psycopg2.pool.PoolError: connection pool exhausted

Fix applied (patch):
- Added connection timeout (30s) in pool config
- Added automatic reconnection on BrokenPipeError
- Increased max_connections from 10 to 20

--- Example 2 (❌ FAILURE — DO NOT REPEAT, similarity: 78%, repo: django-app/api) ---
Error pattern:
=== TRACEBACK (top 3 frames) ===
  [1] File "/app/db/utils.py", line 67, in execute_query
  ...

FAILURE reason: test_failed
Attempted (failed) patch:
Simply retried on connection error — caused cascading timeouts under load.

Related issue: DB connection dropped under high concurrency

--- Example 3 (✅ SUCCESS, similarity: 65%, repo: fastapi-service/core) ---
...
```

---

## 7. 数据流图 (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           RUNTIME DATA FLOW                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────┐                                                                │
│  │   RUN    │  agent_v2() 启动                                               │
│  │  START   │                                                                │
│  └────┬─────┘                                                                │
│       │                                                                      │
│       ▼                                                                      │
│  ┌──────────┐    ┌──────────────────┐                                       │
│  │UNDERSTAND│───▶│ issue_title/body │                                       │
│  └────┬─────┘    │ 解析 + 分类      │                                       │
│       │          └──────────────────┘                                       │
│       ▼                                                                      │
│  ┌──────────┐                                                                │
│  │  LOCATE  │  GitHub 搜索 → relevant_files                                  │
│  └────┬─────┘                                                                │
│       │                                                                      │
│       ▼                                                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  PLAN ◀──── 语义记忆召回 ────┐                                        │   │
│  │                              │                                        │   │
│  │  1. extract_keyframe()       │                                        │   │
│  │     error_log → 关键帧       │    ┌──────────────────┐                │   │
│  │                              │    │  episodes.db     │                │   │
│  │  2. embedder.query_embed()   │    │                  │                │   │
│  │     关键帧 → (384,) vector   │    │  error_episode   │                │   │
│  │                              │    │  ┌────────────┐  │                │   │
│  │  3. vec.search(top_k=3) ─────┼───▶│  │ id,keyframe │  │                │   │
│  │     vector → [(id,sim),...]  │    │  │ patch,repo  │  │                │   │
│  │                              │    │  │ success,... │  │                │   │
│  │  4. format_few_shot()        │    │  └────────────┘  │                │   │
│  │     episodes → prompt text   │    │                  │                │   │
│  │                              │    │  episode_vec     │                │   │
│  │  5. 注入 user prompt         │    │  ┌────────────┐  │                │   │
│  │     → llm_call(system,user)  │    │  │rowid,embed  │  │                │   │
│  │                              │    │  └────────────┘  │                │   │
│  └──────────────┬───────────────┘    └──────────────────┘                │   │
│                 │                                                                 │
│                 ▼                                                                 │
│  ┌──────────┐                                                                     │
│  │ EXECUTE  │  apply_patch_edits / git_apply + run_pytest                        │
│  └────┬─────┘                                                                     │
│       │                                                                           │
│       ▼                                                                           │
│  ┌──────────┐     ┌─────────────────────────────────┐                            │
│  │  VERIFY  │────▶│ FixAttempt 记录在 state 中       │                            │
│  └────┬─────┘     │ .error_log, .patch_content,      │                            │
│       │           │ .success, .failure_kind          │                            │
│       │           └─────────────────────────────────┘                            │
│       │                                                                           │
│       ├── success ──▶ COMMIT ──▶ DONE                                             │
│       │                                                                           │
│       └── failure ──▶ REFLECT ──▶ (retry) PLAN ──▶ ...                           │
│                           │                                                       │
│                           │ (max_retries 耗尽 或 放弃)                             │
│                           ▼                                                       │
│                    ┌──────────────┐                                               │
│                    │ HANDLE_FAIL  │                                               │
│                    │ or DONE      │                                               │
│                    └──────┬───────┘                                               │
│                           │                                                       │
│                           │  ┌──────────────────────────────────┐                │
│                           └─▶│ WRITE PATH: 记录语义记忆         │                │
│                              │                                  │                │
│                              │ 1. extract_keyframe(error_log)   │                │
│                              │ 2. embedder.doc_embed(keyframe)  │                │
│                              │ 3. INSERT error_episode          │                │
│                              │ 4. INSERT episode_vec (vector)   │                │
│                              │                                  │                │
│                              │ episodes.db ← 持久化             │                │
│                              └──────────────────────────────────┘                │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 写入时机

写入 `error_episode` 的调用点选在 **graph 运行结束后**（`new_agent.py` 的 `agent_v2()` 函数中），而非每个 `FixAttempt` 写入后。原因：

1. **一个 run 可能产生多个 FixAttempt**（retry 场景），语义记忆应记录整个 run 的最终结果
2. **语义记忆聚焦「issue → 最终 repair 方案」**，中间尝试的 patch 变化不重要
3. **减少写入频率**——Fire-and-forget，不阻塞 graph 循环

```python
# new_agent.py agent_v2() 末尾，final_state 构建完成后:

async def agent_v2(...) -> dict:
    ...
    final_state = await run_graph(graph, state)
    ...
    
    # === 新增: 记录语义记忆 ===
    try:
        from .memory.error_episode_store import get_episode_store
        store = get_episode_store()
        
        # 取最后一个 FixAttempt 作为 episode 数据源
        last_attempt = final_state.fix_attempts[-1] if final_state.fix_attempts else None
        if last_attempt:
            await store.record_episode(
                error_log=last_attempt.error_log or final_state.failure_reason,
                patch_content=last_attempt.patch_content,
                issue_text=f"{final_state.issue_title}\n{final_state.issue_body[:500]}",
                repo=f"{final_state.owner}/{final_state.repo}",
                success=last_attempt.success,
                failure_kind=last_attempt.failure_kind,
                metadata={
                    "trace_id": final_state.trace_id,
                    "issue_number": final_state.issue_number,
                    "retry_count": final_state.retry_count,
                    "file_path": last_attempt.file_path,
                },
                repo_path=final_state.repo_path,
            )
    except Exception:
        logger.warning("Failed to record semantic memory episode", exc_info=True)
    # === 语义记忆记录结束 ===
    
    payload = agent_payload_from_state(...)
    return payload
```

---

## 8. 存储策略: Global vs Per-Repo

### 8.1 决策：global 为主，per-repo 为辅

```
语义记忆 (error_episode + episode_vec)
  → 存储: ~/.repopilot/memory/episodes.db  (全局单文件)
  → 召回: 默认 cross_repo=True (跨 repo 共享)
  → 过滤: repo 字段保留, 支持 search_similar(repo="...") 限定范围

文件级统计 (file_index + issue_log)
  → 存储: ~/.repopilot/memory/{owner}/{repo}/memory.db  (per-repo)
  → 不变
```

### 8.2 理由

1. **错误模式跨 repo 高度可复用**: 「connection pool exhausted」「race condition」「null pointer」「import error」等模式在 Flask / Django / FastAPI 之间高度相似
2. **冷启动加速**: 新 repo 虽然没有自己的历史，但可以立即受益于其他 repo 的经验
3. **数据密度**: 单 repo 可能只有 10-50 条记录，cross-repo 聚合可达 2000-5000 条，召回质量显著更高
4. **repo 字段保留**: 当用户需要「只看本项目经验」时，`search_similar(repo="owner/repo", cross_repo=False)` 仍然可用

### 8.3 召回策略参数

| 场景 | cross_repo | include_success | include_failure |
|---|---|---|---|
| 首次 PLAN（无 prior 失败） | True | True | True |
| retry PLAN（有失败经验） | True | True | True |
| retry PLAN（连续失败 2 次） | True | True | True (重点) |
| 特定 repo 调试 | False | True | True |

---

## 9. 实施步骤 (3 个 Phase)

### Phase 1: 基础设施搭建 (预计 2-3 天)

**目标**: embedding 模块 + SQLite 表 + 向量索引就绪，可以写入和召回。

| # | 任务 | 产出 | 验证方式 |
|---|---|---|---|
| 1.1 | 安装依赖: `sentence-transformers`, `sqlite-vec` | `requirements.txt` 更新 | `pip install` 成功 |
| 1.2 | 实现 `src/memory/embedding.py` | Embedder 单例，`encode()` + `query_embed()` + `doc_embed()` | 单元测试: encode 返回 (384,) float32 |
| 1.3 | 实现 `src/memory/keyframe.py` | `extract_keyframe()` 函数 | 单元测试: 验证截断到 2KB，前 3 帧+异常信息正确 |
| 1.4 | 实现 `src/memory/vector_index.py` | `VectorIndex` 抽象基类 | 接口定义完成 |
| 1.5 | 实现 `src/memory/sqlite_vec_index.py` | `SQLiteVecIndex` + DDL | 集成测试: insert → search 返回正确结果 |
| 1.6 | 实现 `src/memory/error_episode_store.py` | `ErrorEpisodeStore` 完整类 | 集成测试: record → search_similar 闭环 |
| 1.7 | 在 `new_agent.py` 的 `agent_v2()` 末尾接入写入 | 每次 run 结束时写入 episode | e2e 测试: episodes.db 中有记录 |
| 1.8 | 在 `plan.py` 的 `plan_fix()` 中接入召回 | PLAN prompt 中包含 few-shot 示例 | 手动运行，检查 stderr 日志 |

**交付物**:
- `src/memory/embedding.py`
- `src/memory/keyframe.py`
- `src/memory/vector_index.py`
- `src/memory/sqlite_vec_index.py`
- `src/memory/error_episode_store.py`
- 单元测试文件: `tests/test_embedding.py`, `tests/test_keyframe.py`, `tests/test_episode_store.py`

### Phase 2: 集成 + Prompt 优化 (预计 1-2 天)

**目标**: Few-Shot 示例格式优化，确保 LLM 能有效利用。

| # | 任务 | 产出 | 验证方式 |
|---|---|---|---|
| 2.1 | 调优 `_format_few_shot_examples()` 格式 | 优化后的 prompt 模板 | A/B 对比测试 |
| 2.2 | 添加 `similarity < 0.5` 的低质量结果过滤 | 低相似度结果不注入 | 日志检查 |
| 2.3 | 调优 `extract_keyframe()` 的项目路径识别 | 更准确的项目帧 vs 第三方帧分离 | 回归测试 |
| 2.4 | 添加 prompt token 预算检查 | 注入前估算 few-shot 的 token 数 | 不超过 token 预算 |
| 2.5 | 端到端验证: 运行真实 issue，检查修复质量 | e2e 测试报告 | success rate 对比 |

### Phase 3: 运维 + 监控 (预计 1 天)

**目标**: 稳定性保障，监控和清理机制。

| # | 任务 | 产出 | 验证方式 |
|---|---|---|---|
| 3.1 | 添加 episodes.db 定期 prune | `prune_old(keep_recent=5000)` | 自动清理验证 |
| 3.2 | 添加 embedding 模型加载失败降级 | 模型加载失败时跳过语义召回 | 无模型环境不崩溃 |
| 3.3 | 添加 `src/memory/__init__.py` 统一导出 | `from src.memory import ErrorEpisodeStore, get_embedder` | import 正常 |
| 3.4 | 更新 `new_agent.py` 启动时预加载 embedder | Agent 启动时不阻塞在首个 PLAN | 启动日志确认 |
| 3.5 | 添加 node_diagnostic 记录语义召回耗时 | 监控召回性能 | 日志中有 `semantic_recall` 事件 |

---

## 10. 风险点与缓解措施

| # | 风险 | 影响 | 概率 | 缓解措施 |
|---|---|---|---|---|
| 1 | **BGE 模型加载失败** (网络问题/HuggingFace 不可用) | PLAN 阶段无 Few-Shot 注入，退化为当前行为 | 低 | ✅ try/except 降级，不影响核心流程；可预下载模型到本地 `~/.cache/` |
| 2 | **embedding 延迟高** (首次 encode 触发模型 warmup) | 首个 PLAN 额外耗时 1-3s | 中 | ✅ Agent 启动时预加载 + warmup encode("test") |
| 3 | **sqlite-vec 版本兼容性** (Python 3.10/3.11/3.12 不同) | 向量索引创建失败 | 低 | ✅ sqlite-vec 提供预编译 wheel，CI 覆盖 3 个 Python 版本 |
| 4 | **episodes 数据质量差** (keyframe 提取不准、无效 episode 污染索引) | 召回的 Few-Shot 示例不相关 | 中 | ✅ similarity threshold 过滤 + 定期 prune + 后续可加 RLHF 评分 |
| 5 | **内存增长** (5000 条 × 384 维 × 4 bytes ≈ 7.7MB 向量数据) | 几乎无影响 | 极低 | ✅ 内存占用可忽略，sqlite-vec 暴力扫描也只需 ~5ms |
| 6 | **跨 repo 隐私问题** (不同项目的 episode 互相可见) | 敏感信息泄露 | 低 | ✅ 当前是单机 WSL 场景，无多租户；后续加 repo 级别的可见性控制 |
| 7 | **写入性能** (每次 run 结束时 embed + insert) | run 结束额外 50-200ms | 极低 | ✅ Fire-and-forget 异步写入，不阻塞 agent 返回 |
| 8 | **token 预算超限** (few-shot 示例太长) | prompt 超过 LLM context | 中 | ✅ `_format_few_shot_examples()` 有 max_example_chars 硬截断；注入前估算 token 数 |
| 9 | **错误的正反馈循环** (失败的 patch 被错误地当作成功模板) | 反复推荐错误方案 | 低 | ✅ success 字段严格区分 ✅SUCCESS / ❌FAILURE，失败案例明确标注 DO NOT REPEAT |

---

## 11. 新增依赖清单

```diff
# requirements.txt / pyproject.toml 新增

+ sentence-transformers>=3.0.0   # BGE 模型加载 (推荐，一行代码)
+ sqlite-vec>=0.1.0              # SQLite 向量扩展 (方案 A)
+ numpy>=1.24.0                  # embedding 数组操作
+ torch>=2.0.0                   # sentence-transformers 依赖 (已间接存在)
```

**备选依赖** (仅方案 B 需要):
```diff
+ faiss-cpu>=1.7.4               # 方案 B: FAISS 索引
```

**最小化策略**: 如果不想引入 `sentence-transformers`，可用 `transformers` + `torch` 直接加载 BGE 模型（已在 `Embedder` 中实现 fallback）。`torch` 通常是现有 LLM 依赖的传递依赖，无需额外安装。

---

## 12. 文件结构总览

```
src/memory/
├── __init__.py                  # 导出: ErrorEpisodeStore, get_embedder, etc.
├── repo_store.py                # [已有] per-repo file_index + issue_log
├── error_episode_store.py       # [新增] 全局语义记忆存储
├── embedding.py                 # [新增] BGE-small-en-v1.5 封装
├── vector_index.py              # [新增] VectorIndex 抽象基类
├── sqlite_vec_index.py          # [新增] sqlite-vec 实现
├── keyframe.py                  # [新增] traceback 关键帧提取
└── faiss_index.py               # [可选] FAISS 备选实现

~/ .repopilot/memory/
├── episodes.db                  # [新增] 全局语义记忆 (error_episode + episode_vec)
└── {owner}/
    └── {repo}/
        └── memory.db            # [已有] per-repo 统计
```

---

## 附录 A: 召回质量评估指标 (后续迭代)

| 指标 | 定义 | 目标 |
|---|---|---|
| Recall@3 Hit Rate | 召回的 top-3 中至少有一个 `success=True` 且 `repo` 相同或技术栈相似的案例 | > 70% |
| Few-Shot Utilization | LLM 输出中引用了 Few-Shot 示例的比例 | > 40% |
| Fix Success Rate Lift | 有 Few-Shot vs 无 Few-Shot 的修复成功率提升 | > 5pp |
| Avg Similarity Score | 召回案例的平均 cosine similarity | > 0.6 |

## 附录 B: 关键帧提取 — Python Traceback 格式参考

```
Traceback (most recent call last):
  File "path/to/file.py", line 42, in function_name
    code_line_that_failed()
  File "path/to/another.py", line 15, in wrapper
    return func(*args)
  File "path/to/core.py", line 99, in process
    raise ValueError("something went wrong")
ValueError: something went wrong
```

关键帧提取保留:
- Frame 1: `File "path/to/file.py", line 42, in function_name`
- Frame 2: `File "path/to/another.py", line 15, in wrapper`
- Frame 3: `File "path/to/core.py", line 99, in process`
- Exception: `ValueError: something went wrong`
