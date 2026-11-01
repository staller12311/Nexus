"""检索质量评测引擎（golden set 驱动）。

为什么单独抽一个模块：同一套指标要被两个地方用到 —— pytest 回归用例断言阈值，
以及命令行脚本做 A/B 实验（例如纯 BM25 vs 混合检索）。逻辑只写一份，避免两边口径漂移。

指标口径（三条，含义不同，不要混着看）：

- **Recall@K**：期望文档是否出现在前 K 条里。衡量「找得到」，偏宽松。
- **P@1**：排第一的文档是否就是期望文档。衡量「排得准」，偏严格。
- **MRR**：期望文档首次出现位置的倒数均值。介于两者之间，对排序敏感。

另一条 **evidence** 是项目特有的：命中文档还不够，还得命中文档里那段带关键数字/条件的
证据（比如「30 个自然日」）。因为 RAG 真实的失败模式经常是「文档对了、片段错了」，
只看 Recall 会把这种失败算成通过。

依赖：纯本地 BM25 路径完全确定性，不联网、不调用模型，可以直接挂在 CI 上跑。
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.service.knowledge import KnowledgeStore

#: 评测语料与 golden set 的默认位置（相对 tests/）
DEFAULT_CORPUS = Path(__file__).resolve().parent / "fixtures" / "retrieval_corpus"
DEFAULT_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "retrieval_golden.json"

#: 已知会失败、且**故意不修**的用例：修法要么是改语料迁就查询（等于作弊），
#: 要么是引入同义词扩展/向量检索（属于功能改动，不该混在评测提交里）。
#: 记在这里是为了避免后来者看到一条红用例就去调语料。
KNOWN_WEAKNESSES: dict[str, str] = {
    "vpn-device-lost": (
        "词面不匹配：查询用「笔记本丢了」，语料写的是「设备丢失」，判别性词零重叠；"
        "泛词「工作」反而把 leave-policy 顶到首位。这是纯 BM25 的固有短板，"
        "需要同义词扩展或向量召回才能解决。"
    ),
}


@dataclass
class CaseResult:
    """单条 query 的评测结果。"""

    case_id: str
    query: str
    expect_doc: str
    expect_any: list[str] = field(default_factory=list)
    top_doc: str | None = None
    top_text: str = ""
    rank: int | None = None
    evidence_ok: bool | None = None

    @property
    def hit(self) -> bool:
        return self.rank is not None

    @property
    def top1_ok(self) -> bool:
        return self.rank == 1


@dataclass
class Report:
    """一次评测的汇总。"""

    k: int
    cases: list[CaseResult]

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def recall(self) -> float:
        return sum(1 for c in self.cases if c.hit) / self.total if self.total else 0.0

    @property
    def precision_at_1(self) -> float:
        return sum(1 for c in self.cases if c.top1_ok) / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        if not self.total:
            return 0.0
        return sum(1.0 / c.rank for c in self.cases if c.rank) / self.total

    @property
    def evidence_rate(self) -> float:
        """只在「文档命中」的用例里统计证据命中率，否则分母会把两类失败混在一起。"""
        graded = [c for c in self.cases if c.expect_any and c.hit and c.evidence_ok is not None]
        if not graded:
            return 0.0
        return sum(1 for c in graded if c.evidence_ok) / len(graded)

    def missed(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.hit]

    def wrong_snippet(self) -> list[CaseResult]:
        """文档对了但证据片段没对上的用例 —— RAG 最典型的隐性失败。"""
        return [c for c in self.cases if c.hit and c.evidence_ok is False]


def load_golden(path: str | Path = DEFAULT_GOLDEN) -> tuple[list[dict[str, Any]], int]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return payload["cases"], int(payload.get("top_k", 4))


def _match_evidence(text: str, needles: list[str]) -> bool:
    """命中片段里出现任意一个证据串即算命中（语料里同一事实常有多种表述）。"""
    if not needles:
        return True
    return any(needle in text for needle in needles)


def build_store(
    corpus_dir: str | Path = DEFAULT_CORPUS,
    db_path: str | Path | None = None,
    embedding_provider: Any = None,
) -> tuple[KnowledgeStore, list[str]]:
    """把语料灌进一个临时库。返回 store 与成功入库的文件名列表。"""
    if db_path is None:
        db_path = Path(tempfile.mkdtemp(prefix="kb-eval-")) / "kb.db"
    store = KnowledgeStore(db_path, embedding_provider=embedding_provider)
    ingested: list[str] = []
    for path in sorted(Path(corpus_dir).glob("*.txt")):
        doc = store.add_document(path.name, path.read_bytes())
        if doc.chunk_count:
            ingested.append(path.name)
    return store, ingested


def run_eval(
    corpus_dir: str | Path = DEFAULT_CORPUS,
    golden_path: str | Path = DEFAULT_GOLDEN,
    *,
    top_k: int | None = None,
    db_path: str | Path | None = None,
    embedding_provider: Any = None,
    session_id: str | None = None,
) -> Report:
    """跑一遍 golden set，返回汇总报告。不抛断言，判定交给调用方。"""
    cases, default_k = load_golden(golden_path)
    k = int(top_k or default_k)
    store, _ = build_store(corpus_dir, db_path, embedding_provider)
    try:
        results: list[CaseResult] = []
        for case in cases:
            hits = store.search(case["query"], top_k=k, session_id=session_id)
            docs = [hit.doc_name for hit in hits]
            expect_doc = case["expect_doc"]
            rank = docs.index(expect_doc) + 1 if expect_doc in docs else None
            expect_any = list(case.get("expect_any") or [])
            top = hits[0] if hits else None
            results.append(
                CaseResult(
                    case_id=str(case.get("id") or case["query"]),
                    query=case["query"],
                    expect_doc=expect_doc,
                    expect_any=expect_any,
                    top_doc=top.doc_name if top else None,
                    top_text=top.text if top else "",
                    rank=rank,
                    evidence_ok=(
                        _match_evidence(top.text, expect_any)
                        if (rank == 1 and top is not None)
                        else (False if rank else None)
                    ),
                )
            )
    finally:
        store.close()
    return Report(k=k, cases=results)


def format_report(report: Report) -> str:
    """人读格式：先汇总再逐条，失败的排前面。"""
    lines = [
        f"golden set: {report.total} 条 / top_k={report.k}",
        f"Recall@{report.k}  {report.recall:.1%}",
        f"P@1         {report.precision_at_1:.1%}",
        f"MRR         {report.mrr:.3f}",
        f"证据命中率   {report.evidence_rate:.1%}（分母仅含文档已命中且带证据要求的用例）",
    ]
    missed = report.missed()
    if missed:
        lines.append("")
        lines.append(f"[未命中] {len(missed)} 条")
        for case in missed:
            note = KNOWN_WEAKNESSES.get(case.case_id)
            suffix = "  ← 已知短板（见 KNOWN_WEAKNESSES）" if note else ""
            lines.append(
                f"  - {case.case_id}: {case.query}  → 期望 {case.expect_doc}，实际 {case.top_doc}{suffix}"
            )
            if note:
                lines.append(f"      原因：{note}")
    wrong = report.wrong_snippet()
    if wrong:
        lines.append("")
        lines.append(f"[文档命中但证据片段不对] {len(wrong)} 条")
        for case in wrong:
            got = (case.top_text or "").replace("\n", " ")[:60]
            lines.append(f"  - {case.case_id}: 期望含 {case.expect_any}，实际片段「{got}…」")
    return "\n".join(lines)
