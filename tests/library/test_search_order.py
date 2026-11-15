import uuid

from standingproof.library.fusion import Fused
from standingproof.library.search import in_final_order

LOW, HIGH = uuid.UUID(int=1), uuid.UUID(int=2)


def test_higher_score_first() -> None:
    weak = Fused(("answer", LOW), 0.01, ("keyword",))
    strong = Fused(("policy", HIGH), 0.02, ("meaning",))

    assert in_final_order([weak, strong]) == [strong, weak]


def test_on_equal_scores_an_approved_answer_goes_before_policy_text() -> None:
    policy = Fused(("policy", LOW), 0.02, ("keyword",))
    answer = Fused(("answer", HIGH), 0.02, ("meaning",))

    assert in_final_order([policy, answer]) == [answer, policy]


def test_equal_scores_of_one_kind_are_ordered_by_id_whatever_order_they_arrive_in() -> None:
    first = Fused(("answer", LOW), 0.02, ("meaning",))
    second = Fused(("answer", HIGH), 0.02, ("meaning",))

    assert in_final_order([second, first]) == in_final_order([first, second]) == [first, second]
