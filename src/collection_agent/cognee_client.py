"""Cognee Cloud over REST. Policy text goes to the policies dataset as soft guidance and to draw
the knowledge graph; hard rules never come from here. No retries on writes."""

from typing import Protocol

import httpx

TIMEOUT_SECONDS = 30.0


class CogneeError(Exception):
    """A Cognee call failed. Carries only the path and status, never a key or a body."""

    def __init__(self, path: str, status_code: int | None = None) -> None:
        self.path = path
        self.status_code = status_code
        what = f"HTTP {status_code}" if status_code is not None else "no usable reply"
        super().__init__(f"Cognee {path}: {what}")


class CogneeGateway(Protocol):
    def add_text(self, texts: list[str], dataset_name: str) -> None: ...

    def cognify(self, dataset_name: str) -> None: ...

    def dataset_id(self, name: str) -> str | None: ...

    def visualize_html(self, dataset_id: str) -> str: ...


class LiveCognee:
    def __init__(
        self, base_url: str, api_key: str, http_client: httpx.Client | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._client = http_client or httpx.Client(timeout=TIMEOUT_SECONDS)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{self._base}{path}",
                json=json,
                params=params,
                headers={"X-Api-Key": self._key},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise CogneeError(path) from None
        if response.status_code >= 400:
            raise CogneeError(path, response.status_code)
        return response

    def add_text(self, texts: list[str], dataset_name: str) -> None:
        self._request(
            "POST", "/api/v1/add_text", json={"textData": texts, "datasetName": dataset_name}
        )

    def cognify(self, dataset_name: str) -> None:
        self._request(
            "POST", "/api/v1/cognify", json={"datasets": [dataset_name], "runInBackground": True}
        )

    def dataset_id(self, name: str) -> str | None:
        path = "/api/v1/datasets/"
        try:
            data = self._request("GET", path).json()
        except ValueError:
            raise CogneeError(path) from None
        if not isinstance(data, list):
            raise CogneeError(path)
        for item in data:
            if isinstance(item, dict) and item.get("name") == name:
                found = item.get("id")
                return found if isinstance(found, str) else None
        return None

    def visualize_html(self, dataset_id: str) -> str:
        path = "/api/v1/visualize"
        response = self._request("GET", path, params={"dataset_id": dataset_id})
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith("text/html"):
            return response.text
        if "json" in content_type:
            try:
                body = response.json()
            except ValueError:
                raise CogneeError(path) from None
            if isinstance(body, str):
                return body
        raise CogneeError(path)
