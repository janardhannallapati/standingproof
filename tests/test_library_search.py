"""One search over approved answers and policy text: by word, by meaning, or both."""

import datetime as dt

from fastapi.testclient import TestClient

from tests.conftest import ClientFactory
from tests.test_library_api import MFA, TOMORROW, YESTERDAY, add_answer, upload_policy

ENCRYPTION = {
    "question": "Is customer data encrypted at rest?",
    "answer": "Yes. Customer data is encrypted at rest using AES-256.",
    "owner": "cto@example.com",
    "expires_on": TOMORROW,
    "control_id": "CC6.7",
}


def search(client: TestClient, q: str, **params: object) -> list[dict[str, object]]:
    response = client.get("/library/search", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return list(response.json()["results"])


def test_a_word_in_an_entry_finds_it_by_keyword(api: TestClient) -> None:
    answer_id = add_answer(api).json()["id"]
    add_answer(api, **ENCRYPTION)

    results = search(api, "authentication")

    assert results[0]["id"] == answer_id
    assert results[0]["kind"] == "answer"
    assert "keyword" in results[0]["found_by"]  # type: ignore[operator]
    assert results[0]["owner"] == MFA["owner"]
    assert results[0]["expires_on"] == TOMORROW


def test_different_words_with_the_same_meaning_find_it_by_meaning(api: TestClient) -> None:
    answer_id = add_answer(api).json()["id"]
    add_answer(api, **ENCRYPTION)

    # "2FA" and "staff" appear nowhere in the stored answer.
    results = search(api, "2FA staff")

    assert results[0]["id"] == answer_id
    assert results[0]["found_by"] == ["meaning"]


def test_found_by_both_ranks_above_found_by_one(api: TestClient) -> None:
    # Shares the word "customer" with the query, and nothing of its meaning.
    keyword_only = add_answer(
        api,
        question="Who is the customer success contact?",
        answer="The customer success manager named in the order form.",
        control_id=None,
    ).json()["id"]
    both = add_answer(api, **ENCRYPTION).json()["id"]

    results = search(api, "customer data encryption")

    assert [r["id"] for r in results[:2]] == [both, keyword_only]
    assert results[0]["found_by"] == ["keyword", "meaning"]
    assert results[1]["found_by"] == ["keyword"]


def test_an_unrelated_query_finds_nothing(api: TestClient) -> None:
    add_answer(api)

    assert search(api, "parking permits") == []


def test_an_expired_answer_is_never_returned(api: TestClient) -> None:
    add_answer(api, expires_on=YESTERDAY)

    assert search(api, "multi-factor authentication employees") == []


def test_an_answer_is_still_returned_on_its_expiry_date(api: TestClient) -> None:
    today = dt.datetime.now(dt.UTC).date().isoformat()
    answer_id = add_answer(api, expires_on=today).json()["id"]

    assert [r["id"] for r in search(api, "authentication")] == [answer_id]


def test_another_tenants_entries_never_appear(make_api: ClientFactory) -> None:
    ours = make_api()
    theirs = make_api(dev_tenant_name="another-company")
    # Sixty entries that match the query better than ours, in words and in meaning. More
    # than the vector index examines in one pass, so ours is found only if the search
    # filters by tenant and keeps scanning past rows that belong to someone else.
    for n in range(60):
        add_answer(theirs, question=f"2FA staff {n}", answer="2FA staff", control_id=None)
    upload_policy(theirs)
    mine = add_answer(ours).json()["id"]

    assert [r["id"] for r in search(ours, "2FA staff", limit=1)] == [mine]
    assert len(search(theirs, "2FA staff", limit=20)) == 20


def test_an_exact_control_id_comes_first(api: TestClient) -> None:
    add_answer(api)  # CC6.1, and a much closer match for the words of the query
    encryption = add_answer(api, **ENCRYPTION).json()["id"]

    results = search(api, "multi-factor authentication for employees, see cc6.7?")

    assert results[0]["id"] == encryption
    assert "control_id" in results[0]["found_by"]  # type: ignore[operator]


def test_policy_text_is_found_with_its_heading_path_and_document(api: TestClient) -> None:
    upload_policy(api)

    results = search(api, "quarter reviews")

    assert results[0]["kind"] == "policy"
    assert results[0]["heading_path"] == "Access Control Policy > 4.3 Reviews"
    assert results[0]["document_title"] == "Access Control Policy"
    assert results[0]["text"] == "Access is reviewed every quarter."


def test_limit_is_respected_and_bounded(make_api: ClientFactory) -> None:
    api = make_api(search_max_results=5)
    for n in range(4):
        add_answer(api, question=f"Is multi-factor authentication required, case {n}?")

    assert len(search(api, "authentication", limit=2)) == 2
    assert api.get("/library/search", params={"q": "x", "limit": 6}).status_code == 422
    assert api.get("/library/search", params={"q": "x", "limit": 0}).status_code == 422


def test_an_empty_query_is_refused(api: TestClient) -> None:
    assert api.get("/library/search", params={"q": "   "}).status_code == 422
    assert api.get("/library/search").status_code == 422


def test_query_text_is_data_not_sql_or_search_syntax(api: TestClient) -> None:
    answer_id = add_answer(api).json()["id"]

    for q in ["authentication' OR '1'='1'; --", "authentication & | ! ( :* <->"]:
        assert [r["id"] for r in search(api, q)] == [answer_id]


def test_when_an_answer_and_a_policy_passage_tie_the_approved_answer_comes_first(
    api: TestClient,
) -> None:
    # Each is found once, at rank 1 of a different search, so their merged scores are equal.
    # Run several times over: with no rule, the order would follow the random ids.
    for n in range(6):
        upload_policy(api, f"# Doc {n}\n\nzebra{n} crossing rules.\n".encode(), name=f"{n}.md")
        add_answer(api, question=f"marker{n}?", answer="2FA for staff.", control_id=None)

        results = search(api, f"zebra{n} multi-factor employees", limit=2)

        assert [r["kind"] for r in results] == ["answer", "policy"], n
