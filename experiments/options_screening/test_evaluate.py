from evaluate import aggregate, ranking_metrics, rrf


def test_rrf_no_duplicate_query_or_duplicate_document_votes():
    lists = [{"query_key": "a", "version_ids": ["x", "x", "y"]},
             {"query_key": "a", "version_ids": ["y", "x"]}, {"query_key": "b", "version_ids": ["y"]}]
    ranked, scores = rrf(lists)
    assert ranked == ["y", "x"]
    assert scores["x"] == 1/61 and scores["y"] == 1/62 + 1/61


def test_unknown_is_not_violation_and_abstract_not_final_inclusion():
    criteria = [{"criterion_id": "a", "stage": "title_abstract", "required": True}]
    assert aggregate(criteria, [], "title_abstract")["result"] == "pending_review"
    supplied = [{"criterion_id": "a", "judgment": "meets", "references": ["source"]}]
    assert aggregate(criteria, supplied, "title_abstract")["result"] == "advance_to_fulltext"
    supplied[0].update(judgment="violates", basis="absence")
    assert aggregate(criteria, supplied, "title_abstract")["result"] == "pending_review"


def test_explicit_violation_preserves_other_unknowns():
    criteria = [{"criterion_id": c, "stage": "title_abstract", "required": True} for c in ["a", "b"]]
    result = aggregate(criteria, [{"criterion_id": "a", "judgment": "violates", "basis": "explicit_conflict", "references": ["source"]}], "title_abstract")
    assert result["result"] == "excluded" and result["unknown"] == ["b"]


def test_uncertain_not_counted_as_relevant_or_complete_recall():
    result = ranking_metrics(["x", "y"], {"x": "core", "y": "uncertain"}, 10)
    assert result["available_precision_lower"] == .5
    assert result["precision_lower"] == .1 and result["precision_upper"] == .2
    assert result["recall_in_labeled_reference"] is None
