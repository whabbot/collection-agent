"""The Cognee REST client against an in-memory transport. No network."""

import json

import httpx
import pytest

from collection_agent.cognee_client import CogneeError, LiveCognee

KEY = "cognee_test_fake_key"
BASE = "https://tenant.cognee.test"


def client_with(
    handler: httpx.MockTransport | None = None,
    *,
    seen: list[httpx.Request] | None = None,
    response: httpx.Response | None = None,
    raises: Exception | None = None,
) -> LiveCognee:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if raises is not None:
            raise raises
        return response or httpx.Response(200, json={})

    transport = handler or httpx.MockTransport(handle)
    return LiveCognee(BASE + "/", KEY, http_client=httpx.Client(transport=transport))


def test_add_text_posts_the_documented_body_with_the_key_header() -> None:
    seen: list[httpx.Request] = []
    client_with(seen=seen).add_text(["some policy"], "policies")

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/api/v1/add_text"
    assert request.headers["x-api-key"] == KEY
    assert json.loads(request.content) == {"textData": ["some policy"], "datasetName": "policies"}


def test_cognify_runs_in_the_background() -> None:
    seen: list[httpx.Request] = []
    client_with(seen=seen).cognify("policies")

    (request,) = seen
    assert str(request.url) == f"{BASE}/api/v1/cognify"
    assert json.loads(request.content) == {"datasets": ["policies"], "runInBackground": True}
    assert request.headers["x-api-key"] == KEY


def test_dataset_id_matches_on_name_and_uses_the_trailing_slash() -> None:
    seen: list[httpx.Request] = []
    data = [{"id": "id-other", "name": "contracts"}, {"id": "id-pol", "name": "policies"}]
    client = client_with(seen=seen, response=httpx.Response(200, json=data))

    assert client.dataset_id("policies") == "id-pol"
    assert str(seen[0].url) == f"{BASE}/api/v1/datasets/"
    assert seen[0].method == "GET"
    assert seen[0].headers["x-api-key"] == KEY


def test_a_missing_dataset_is_none() -> None:
    client = client_with(response=httpx.Response(200, json=[{"id": "x", "name": "contracts"}]))
    assert client.dataset_id("policies") is None


def test_an_unexpected_datasets_body_is_an_error() -> None:
    with pytest.raises(CogneeError):
        client_with(response=httpx.Response(200, json={"oops": 1})).dataset_id("policies")
    with pytest.raises(CogneeError):
        client_with(response=httpx.Response(200, text="<html>")).dataset_id("policies")


def test_visualize_returns_html_as_is() -> None:
    seen: list[httpx.Request] = []
    response = httpx.Response(200, text="<html>graph</html>", headers={"content-type": "text/html"})
    html = client_with(seen=seen, response=response).visualize_html("id-pol")

    assert html == "<html>graph</html>"
    assert seen[0].url.path == "/api/v1/visualize"
    assert seen[0].url.params["dataset_id"] == "id-pol"


def test_visualize_accepts_a_json_string() -> None:
    response = httpx.Response(200, json="<html>graph</html>")
    assert client_with(response=response).visualize_html("id") == "<html>graph</html>"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"html": "<p>"}),
        httpx.Response(200, json=["<p>"]),
        httpx.Response(200, content=b"x", headers={"content-type": "application/json"}),
        httpx.Response(200, content=b"x", headers={"content-type": "image/png"}),
    ],
)
def test_visualize_rejects_anything_else(response: httpx.Response) -> None:
    with pytest.raises(CogneeError):
        client_with(response=response).visualize_html("id")


def test_http_errors_carry_only_the_path_and_status() -> None:
    body = {"detail": f"bad key {KEY} for the policy text"}
    client = client_with(response=httpx.Response(401, json=body))
    with pytest.raises(CogneeError) as info:
        client.add_text(["secret policy text"], "policies")

    assert info.value.status_code == 401
    assert info.value.path == "/api/v1/add_text"
    assert KEY not in str(info.value)
    assert "secret policy text" not in str(info.value)


def test_a_network_failure_is_a_cognee_error_and_writes_are_not_retried() -> None:
    seen: list[httpx.Request] = []
    client = client_with(seen=seen, raises=httpx.ConnectError(f"boom {KEY}"))
    with pytest.raises(CogneeError) as info:
        client.cognify("policies")
    assert info.value.status_code is None
    assert KEY not in str(info.value)
    assert len(seen) == 1
