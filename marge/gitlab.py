import abc
import dataclasses
import json
import logging as log
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Union,
    cast,
)

import requests


class RequestsMethod(Protocol):
    # pylint: disable=too-few-public-methods

    __name__: str

    def __call__(self, urls: str, **kwargs: Any) -> requests.Response:
        ...


class Api:
    def __init__(self, gitlab_url: str, auth_token: str) -> None:
        self._auth_token = auth_token
        self._api_base_url = gitlab_url.rstrip("/") + "/api/v4"

    def call(
        self, command: "Command", sudo: Optional[int] = None
    ) -> Union[bool, Dict[str, Any], List[Dict[str, Any]]]:
        method = command.method
        url = self._api_base_url + command.endpoint
        headers = {"PRIVATE-TOKEN": self._auth_token}
        if sudo:
            headers["SUDO"] = f"{sudo}"
        log.debug(
            "REQUEST: %s %s %r %r",
            method.__name__.upper(),
            url,
            headers,
            command.call_args,
        )
        # Timeout to prevent indefinitely hanging requests. 60s is very conservative,
        # but should be short enough to not cause any practical annoyances. We just
        # crash rather than retry since marge-bot should be run in a restart loop anyway.
        try:
            response = method(url, headers=headers, timeout=60, **command.call_args)
        except requests.exceptions.Timeout as err:
            log.error("Request timeout: %s", err)
            raise
        log.debug("RESPONSE CODE: %s", response.status_code)
        log.debug("RESPONSE BODY: %r", response.content)

        if response.status_code == 202:
            return True  # Accepted

        if response.status_code == 204:
            return True  # NoContent

        if response.status_code < 300:
            return (
                command.extract(response.json()) if command.extract else response.json()
            )

        if response.status_code == 304:
            return False  # Not Modified

        errors = {
            400: BadRequest,
            401: Unauthorized,
            403: Forbidden,
            404: NotFound,
            405: MethodNotAllowed,
            406: NotAcceptable,
            409: Conflict,
            422: Unprocessable,
            500: InternalServerError,
        }

        def other_error(code: int, msg: Union[str, Dict[str, Any]]) -> Exception:
            exception = InternalServerError if 500 < code < 600 else UnexpectedError
            return exception(code, msg)

        error = errors.get(response.status_code, other_error)
        try:
            err_message = response.json()
        except json.JSONDecodeError:
            err_message = response.reason

        raise error(response.status_code, err_message)

    def collect_all_pages(self, get_command: "GET") -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        fetch_again, page_no = True, 1
        while fetch_again:
            page = self.call(get_command.for_page(page_no))
            if page:
                if TYPE_CHECKING:
                    assert isinstance(page, list)
                result.extend(page)
                page_no += 1
            else:
                fetch_again = False

        return result


def from_singleton_list(
    fun: Optional[Callable[[Dict[str, Any]], Any]] = None
) -> Callable[[List[Dict[str, Any]]], Any]:
    def extractor(response_list: List[Dict[str, Any]]) -> Any:
        assert isinstance(response_list, list), type(response_list)
        assert len(response_list) <= 1, len(response_list)
        if not response_list:
            return None
        if fun is None:
            return response_list[0]
        return fun(response_list[0])

    return extractor


@dataclasses.dataclass(frozen=True)
class Command(abc.ABC):
    endpoint: str
    args: Dict[str, Any] = dataclasses.field(default_factory=dict)
    extract: Optional[Callable[[List[Dict[str, Any]]], Dict[str, Any]]] = None

    @property
    @abc.abstractmethod
    def method(self) -> RequestsMethod:
        ...

    @property
    def call_args(self) -> Dict[str, Dict[str, Any]]:
        return {"json": self.args}


class GET(Command):
    @property
    def method(self) -> RequestsMethod:
        return cast(RequestsMethod, requests.get)

    @property
    def call_args(self) -> Dict[str, Dict[str, str]]:
        return {"params": _prepare_params(self.args)}

    def for_page(self, page_no: int) -> "GET":
        args = self.args
        return dataclasses.replace(self, args=dict(args, page=page_no, per_page=100))


class PUT(Command):
    @property
    def method(self) -> RequestsMethod:
        return cast(RequestsMethod, requests.put)


class POST(Command):
    @property
    def method(self) -> RequestsMethod:
        return cast(RequestsMethod, requests.post)


class DELETE(Command):
    @property
    def method(self) -> RequestsMethod:
        return cast(RequestsMethod, requests.delete)


def _prepare_params(params: Dict[str, Any]) -> Dict[str, str]:
    def process(val: Any) -> str:
        if isinstance(val, bool):
            return "true" if val else "false"
        return str(val)

    return {key: process(val) for key, val in params.items()}


class ApiError(Exception):
    @property
    def error_message(self) -> Optional[str]:
        args = self.args
        if len(args) != 2:
            return None

        # Code is the first element
        arg = args[1]
        # Might be as deep as {"message": {"base": ["real message"]}}
        if isinstance(arg, dict):
            arg = arg.get("message")

        if isinstance(arg, dict) and "base" in arg:
            arg = arg.get("base")

        if isinstance(arg, list) and len(arg) == 1:
            arg = arg[0]

        if TYPE_CHECKING:
            assert isinstance(arg, str)
        return arg


class BadRequest(ApiError):
    pass


class Unauthorized(ApiError):
    pass


class Forbidden(ApiError):
    pass


class NotFound(ApiError):
    pass


class MethodNotAllowed(ApiError):
    pass


class NotAcceptable(ApiError):
    pass


class Conflict(ApiError):
    pass


class Unprocessable(ApiError):
    pass


class InternalServerError(ApiError):
    pass


class UnexpectedError(ApiError):
    pass


class Resource(abc.ABC):
    def __init__(self, api: Api, info: Dict[str, Any]):
        self._info = info
        self._api = api

    @property
    def info(self) -> Dict[str, Any]:
        return self._info

    @property
    @abc.abstractmethod
    def id(self) -> Union[int, str]:  # pylint: disable=invalid-name
        ...

    @property
    def api(self) -> Api:
        return self._api

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._api}, {self.info})"
