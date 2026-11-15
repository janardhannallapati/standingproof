import pytest

from standingproof.library.fusion import reciprocal_rank_fusion


def test_an_item_in_both_lists_outranks_items_in_one() -> None:
    fused = reciprocal_rank_fusion({"keyword": ["a", "b"], "meaning": ["c", "b"]})

    assert [f.key for f in fused] == ["b", "a", "c"]
    assert fused[0].found_by == ("keyword", "meaning")
    assert fused[1].found_by == ("keyword",)
    assert fused[2].found_by == ("meaning",)


def test_score_is_the_sum_of_one_over_k_plus_rank() -> None:
    fused = reciprocal_rank_fusion({"keyword": ["a", "b"], "meaning": ["b"]}, k=60)

    scores = {f.key: f.score for f in fused}
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_ties_keep_the_order_the_lists_were_given_in() -> None:
    fused = reciprocal_rank_fusion({"keyword": ["a"], "meaning": ["b"]})

    assert [f.key for f in fused] == ["a", "b"]


def test_empty_lists_are_fine() -> None:
    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"keyword": [], "meaning": []}) == []
    assert [f.key for f in reciprocal_rank_fusion({"keyword": [], "meaning": ["x"]})] == ["x"]


def test_a_repeat_within_one_list_counts_once_at_its_best_rank() -> None:
    fused = reciprocal_rank_fusion({"keyword": ["a", "a", "b"]})

    assert [f.key for f in fused] == ["a", "b"]
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[1].score == pytest.approx(1 / 62)


def test_a_weight_scales_what_one_list_contributes() -> None:
    rankings = {"keyword": ["a", "b"], "meaning": ["b", "a"]}

    even = reciprocal_rank_fusion(rankings)
    weighted = reciprocal_rank_fusion(rankings, weights={"keyword": 0.5})

    # Unweighted it is a dead heat; with keyword halved, the meaning list decides.
    assert even[0].score == pytest.approx(even[1].score)
    assert [f.key for f in weighted] == ["b", "a"]
    assert weighted[0].score == pytest.approx(0.5 / 62 + 1 / 61)
