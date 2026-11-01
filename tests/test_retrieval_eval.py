"""知识库检索质量回归（golden set）。

这套用例存在的理由：功能测试只能证明「检索能返回结果」，证明不了「检索返回得对」。
一次分词改动、一个权重调整，都可能让召回从 95% 掉到 70%，而所有功能测试依然全绿。
这里用固定语料 + 固定 query 把检索质量钉住，掉下去就让 CI 红。

确定性：语料是仓库内的小 txt，走纯 BM25 路径，不联网、不调模型，因此结果可复现。

阈值策略：设为「略低于当前实测值」。基线（21 条 golden set，top_k=4）是
Recall@4 95.2% / P@1 95.2% / MRR 0.952 / 证据命中率 100%。
留出约 1 条用例的余量（1/21 ≈ 4.8%），既能容错，又能在真退化时报警。
**要抬高阈值，必须先把指标做上去，不要为了绿而调数字。**
"""

from __future__ import annotations

import pytest

from retrieval_eval import KNOWN_WEAKNESSES, build_store, format_report, run_eval

#: 低于这些值即判定检索质量退化
MIN_RECALL = 0.90
MIN_PRECISION_AT_1 = 0.90
MIN_MRR = 0.90
MIN_EVIDENCE_RATE = 0.95


def test_corpus_is_fully_ingested() -> None:
    """语料必须全部解析成功且切出片段，否则评测会静默变成「少文档比赛」。"""
    store, ingested = build_store()
    try:
        docs = store.list_documents()
    finally:
        store.close()

    assert len(ingested) == 6, f"应入库 6 份语料，实际 {ingested}"
    assert all(doc.status == "ready" for doc in docs)
    assert all(doc.chunk_count > 0 for doc in docs), "存在零切片的文档"


def test_retrieval_quality_meets_baseline() -> None:
    """golden set 上的召回与排序不得低于基线。"""
    report = run_eval()
    print()
    print(format_report(report))

    assert report.total >= 20, f"golden set 不应少于 20 条，当前 {report.total}"
    assert report.recall >= MIN_RECALL, (
        f"Recall@{report.k} 退化到 {report.recall:.1%}（阈值 {MIN_RECALL:.0%}）\n"
        + format_report(report)
    )
    assert report.precision_at_1 >= MIN_PRECISION_AT_1, (
        f"P@1 退化到 {report.precision_at_1:.1%}（阈值 {MIN_PRECISION_AT_1:.0%}）\n"
        + format_report(report)
    )
    assert report.mrr >= MIN_MRR, (
        f"MRR 退化到 {report.mrr:.3f}（阈值 {MIN_MRR}）\n" + format_report(report)
    )
    assert report.evidence_rate >= MIN_EVIDENCE_RATE, (
        f"证据命中率退化到 {report.evidence_rate:.1%}（阈值 {MIN_EVIDENCE_RATE:.0%}）\n"
        + format_report(report)
    )


def test_unexpected_misses_are_reported() -> None:
    """未命中的用例必须都在 KNOWN_WEAKNESSES 里登记。

    新出现的未命中会在这里暴露出来 —— 要么修检索，要么在 KNOWN_WEAKNESSES 写明原因，
    不允许「悄悄变差还继续绿」。
    """
    report = run_eval()
    unexpected = [c.case_id for c in report.missed() if c.case_id not in KNOWN_WEAKNESSES]
    assert not unexpected, (
        "出现未登记的检索失败用例：" + ", ".join(unexpected) + "\n" + format_report(report)
    )


@pytest.mark.parametrize("case_id", sorted(KNOWN_WEAKNESSES))
def test_known_weakness_still_fails(case_id: str) -> None:
    """已知短板若被修好了，应同步删掉登记项并抬高基线。

    反向断言：防止「短板修好了但 KNOWN_WEAKNESSES 忘了删」，导致阈值长期虚低。
    """
    report = run_eval()
    passed = {c.case_id for c in report.cases if c.top1_ok}
    assert case_id not in passed, (
        f"{case_id} 已经能正确召回，请从 KNOWN_WEAKNESSES 删除该条并上调 MIN_* 阈值"
    )
