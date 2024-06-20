import abc
import datetime
import re
import tempfile
from typing import TYPE_CHECKING, Dict, Optional

from . import git
from . import project as mb_project
from . import user as mb_user


class RepoManager(abc.ABC):
    def __init__(
        self,
        user: mb_user.User,
        root_dir: str,
        timeout: Optional[datetime.timedelta] = None,
        reference: Optional[str] = None,
    ):
        self._root_dir = root_dir
        self._user = user
        self._repos: Dict[int, git.Repo] = {}
        self._timeout = timeout
        self._reference = reference

    @abc.abstractmethod
    def repo_for_project(self, project: mb_project.Project) -> git.Repo:
        ...

    @property
    def user(self) -> mb_user.User:
        return self._user

    @property
    def root_dir(self) -> str:
        return self._root_dir


class SshRepoManager(RepoManager):
    def __init__(
        self,
        user: mb_user.User,
        root_dir: str,
        ssh_key_file: Optional[str] = None,
        timeout: Optional[datetime.timedelta] = None,
        reference: Optional[str] = None,
    ):
        super().__init__(user, root_dir, timeout, reference)
        self._ssh_key_file = ssh_key_file

    def repo_for_project(self, project: mb_project.Project) -> git.Repo:
        repo = self._repos.get(project.id)
        if not repo or repo.remote_url != project.ssh_url_to_repo:
            repo_url = project.ssh_url_to_repo
            local_repo_dir = tempfile.mkdtemp(dir=self._root_dir)

            repo = git.Repo(
                repo_url,
                local_repo_dir,
                ssh_key_file=self._ssh_key_file,
                timeout=self._timeout,
                reference=self._reference,
            )
            repo.clone()
            if TYPE_CHECKING:
                assert self._user.email is not None
            repo.config_user_info(
                user_email=self._user.email,
                user_name=self._user.name,
            )

            self._repos[project.id] = repo

        return repo

    @property
    def ssh_key_file(self) -> Optional[str]:
        return self._ssh_key_file


class HttpsRepoManager(RepoManager):
    def __init__(
        self,
        user: mb_user.User,
        root_dir: str,
        auth_token: Optional[str] = None,
        timeout: Optional[datetime.timedelta] = None,
        reference: Optional[str] = None,
    ):
        super().__init__(user, root_dir, timeout, reference)
        self._auth_token = auth_token

    def repo_for_project(self, project: mb_project.Project) -> git.Repo:
        repo = self._repos.get(project.id)
        if not repo or repo.remote_url != project.http_url_to_repo:
            if TYPE_CHECKING:
                assert self._auth_token is not None
            credentials = "oauth2:" + self._auth_token
            # insert token auth "oauth2:<auth_token>@"
            pattern = "(http(s)?://)"
            replacement = r"\1" + credentials + "@"
            repo_url = re.sub(pattern, replacement, project.http_url_to_repo, 1)
            local_repo_dir = tempfile.mkdtemp(dir=self._root_dir)

            repo = git.Repo(
                repo_url,
                local_repo_dir,
                ssh_key_file=None,
                timeout=self._timeout,
                reference=self._reference,
            )
            repo.clone()
            if TYPE_CHECKING:
                assert self._user.email is not None
            repo.config_user_info(
                user_email=self._user.email,
                user_name=self._user.name,
            )

            self._repos[project.id] = repo

        return repo

    @property
    def auth_token(self) -> Optional[str]:
        return self._auth_token
