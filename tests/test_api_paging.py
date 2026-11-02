from typing import Any

from fakes import FakeApiClient, FakeResponse, RecordedRequest

from moveit_py.api import get_all_pages


def _paged_client(total_pages: int, per_page_items: int = 2) -> FakeApiClient:
    client = FakeApiClient()

    def answer(request: RecordedRequest) -> FakeResponse:
        params: Any = request.params
        page = params.get("page", 1)
        items = [f"p{page}-{i}" for i in range(per_page_items)]
        return FakeResponse(
            json_body={"items": items, "paging": {"page": page, "perPage": 200, "totalPages": total_pages}}
        )

    client.add("GET", "/files", answer)
    return client


def test_get_all_pages_concatenates_three_pages() -> None:
    client = _paged_client(3)
    items = get_all_pages(client, "/files", {"path": "/Data"})
    assert items == ["p1-0", "p1-1", "p2-0", "p2-1", "p3-0", "p3-1"]
    assert [r.params for r in client.requests] == [
        {"path": "/Data", "perPage": 200},
        {"path": "/Data", "page": 2, "perPage": 200},
        {"path": "/Data", "page": 3, "perPage": 200},
    ]


def test_get_all_pages_single_page() -> None:
    client = _paged_client(1)
    assert get_all_pages(client, "/files") == ["p1-0", "p1-1"]
    assert len(client.requests) == 1


def test_get_all_pages_two_pages_and_empty() -> None:
    assert len(get_all_pages(_paged_client(2), "/files")) == 4
    assert get_all_pages(_paged_client(1, per_page_items=0), "/files") == []
