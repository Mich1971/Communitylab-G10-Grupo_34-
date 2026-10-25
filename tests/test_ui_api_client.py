import httpx
import pytest

from ui.api_client import APIClient, APIError, UISettings


def settings():
    return UISettings(
        api_url="http://localhost:8000",
        webhook_secret="secreto-pruebas-1234",
        _env_file=None,
    )


def test_auth_and_no_empty_filters():
    def handler(request):
        assert request.headers["X-Webhook-Secret"] == "secreto-pruebas-1234"
        assert "cursor" not in request.url.params
        return httpx.Response(200, json={"items": [], "total": 0})

    api = APIClient(settings(), transport=httpx.MockTransport(handler))
    assert api.get("/runs", cursor=None)["total"] == 0


@pytest.mark.parametrize("status", [401, 404, 409, 413, 422, 503])
def test_errors_preserve_conflict_without_leaking_secret(status):
    def handler(request):
        return httpx.Response(
            status,
            json={
                "error": "conflicto_revision",
                "detalle": "secreto-pruebas-1234",
                "revision_actual": 3,
            },
        )

    api = APIClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(APIError) as error:
        api.post("/runs/run/revisiones", {"revision_base": 1})
    assert error.value.status == status
    assert error.value.revision_actual == 3
    assert "secreto-pruebas-1234" not in str(error.value)


def test_timeout_does_not_repeat_mutation():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("private detail", request=request)

    api = APIClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(APIError) as error:
        api.post("/runs/run/revisiones", {})
    assert error.value.uncertain
    assert len(requests) == 1


def test_invalid_response():
    api = APIClient(
        settings(),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text="<html>bad</html>")
        ),
    )
    with pytest.raises(APIError):
        api.get("/runs")
