from typing import TYPE_CHECKING, Optional

from . import gitlab


class User(gitlab.Resource):
    @classmethod
    def myself(cls, api: gitlab.Api) -> "User":
        info = api.call(gitlab.GET("/user"))
        if TYPE_CHECKING:
            assert isinstance(info, dict)

        return cls(api, info)

    @property
    def id(self) -> int:
        result = self._info["id"]
        if TYPE_CHECKING:
            assert isinstance(result, int)
        return result

    @property
    def is_admin(self) -> bool:
        result = self.info.get("is_admin") is True
        if TYPE_CHECKING:
            assert isinstance(result, bool)
        return result

    @classmethod
    def fetch_by_id(cls, user_id: int, api: gitlab.Api) -> "User":
        info = api.call(gitlab.GET(f"/users/{user_id}"))
        if TYPE_CHECKING:
            assert isinstance(info, dict)
        return cls(api, info)

    @property
    def name(self) -> str:
        result = self.info["name"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result.strip()

    @property
    def username(self) -> str:
        result = self.info["username"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result

    @property
    def email(self) -> Optional[str]:
        """Only visible to admins and 'self'. Sigh."""
        result = self.info.get("email")
        if TYPE_CHECKING:
            if result is not None:
                assert isinstance(result, str)
        return result

    @property
    def state(self) -> str:
        result = self.info["state"]
        if TYPE_CHECKING:
            assert isinstance(result, str)
        return result
