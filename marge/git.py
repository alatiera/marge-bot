import dataclasses
import datetime
import logging as log
import os
import shlex
import subprocess
import sys
from subprocess import PIPE, TimeoutExpired
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from . import trailerfilter

# Turning off StrictHostKeyChecking is a nasty hack to approximate
# just accepting the hostkey sight unseen the first time marge
# connects. The proper solution would be to pass in known_hosts as
# a commandline parameter, but in practice few people will bother anyway and
# in this case the threat of MiTM seems somewhat bogus.
GIT_SSH_COMMAND = "ssh -o StrictHostKeyChecking=no "


def _filter_branch_script(
    trailer_name: str, trailer_values: List[str], keep_trailers: bool
) -> str:
    trailers: str = shlex.quote(
        "\n".join(
            f"{trailer_name}: {trailer_value}"
            for trailer_value in trailer_values or [""]
        )
    )

    filter_script = f"TRAILERS={trailers} KEEP_TRAILERS={keep_trailers} python3 {trailerfilter.__file__}"
    return filter_script


@dataclasses.dataclass
class Repo:
    remote_url: str
    local_path: str
    ssh_key_file: Optional[str]
    timeout: Optional[datetime.timedelta]
    reference: Optional[str]

    def clone(self) -> None:
        reference_flag = "--reference=" + self.reference if self.reference else ""
        self.git(
            "clone",
            "--origin=origin",
            reference_flag,
            self.remote_url,
            self.local_path,
            from_repo=False,
        )

    def config_user_info(self, user_name: str, user_email: str) -> None:
        self.git("config", "user.email", user_email)
        self.git("config", "user.name", user_name)

    def fetch(self, remote_name: str, remote_url: Optional[str] = None) -> None:
        if remote_name != "origin":
            assert remote_url is not None
            # upsert remote
            try:
                self.git("remote", "rm", remote_name)
            except GitError:
                pass
            self.git("remote", "add", remote_name, remote_url)
        self.git("fetch", "--prune", remote_name)

    def tag_with_trailer(
        self,
        trailer_name: str,
        trailer_values: List[str],
        branch: str,
        start_commit: str,
        keep_trailers: bool,
    ) -> str:
        """Add `trailer_name` in commit messages with `trailer_values` in `branch` from `start_commit`."""

        # Strips all `$trailer_name``: lines and trailing newlines, adds an empty
        # newline and tags on the `$trailer_name: $trailer_value` for each `trailer_value` in
        # `trailer_values`.
        filter_script = _filter_branch_script(
            trailer_name, trailer_values, keep_trailers
        )
        commit_range = start_commit + ".." + branch
        try:
            # --force = overwrite backup of last filter-branch
            self.git(
                "filter-branch", "--force", "--msg-filter", filter_script, commit_range
            )
        except GitError:
            log.warning("filter-branch failed, will try to restore")
            try:
                self.get_commit_hash("refs/original/refs/heads/")
            except GitError:
                log.warning("No changes have been effected by filter-branch")
            else:
                self.git("reset", "--hard", "refs/original/refs/heads/" + branch)
            raise
        return self.get_commit_hash()

    def merge(
        self,
        source_branch: str,
        target_branch: str,
        *merge_args: str,
        source_repo_url: Optional[str] = None,
        local: bool = False,
    ) -> str:
        """Merge `target_branch` into `source_branch` and return the new HEAD commit id.

        By default `source_branch` and `target_branch` are assumed to reside in the same
        repo as `self`. However, if `source_repo_url` is passed and not `None`,
        `source_branch` is taken from there.

        Throws a `GitError` if the merge fails. Will also try to --abort it.
        """
        return self._fuse_branch(
            "merge",
            source_branch,
            target_branch,
            *merge_args,
            source_repo_url=source_repo_url,
            local=local,
        )

    def fast_forward(
        self,
        source: str,
        target: str,
        source_repo_url: Optional[str] = None,
        local: bool = False,
    ) -> str:
        return self.merge(
            source,
            target,
            "--ff",
            "--ff-only",
            source_repo_url=source_repo_url,
            local=local,
        )

    def rebase(
        self,
        branch: str,
        new_base: str,
        source_repo_url: Optional[str] = None,
        local: bool = False,
    ) -> str:
        """Rebase `new_base` into `branch` and return the new HEAD commit id.

        By default `branch` and `new_base` are assumed to reside in the same
        repo as `self`. However, if `source_repo_url` is passed and not `None`,
        `branch` is taken from there.

        Throws a `GitError` if the rebase fails. Will also try to --abort it.
        """
        fuse_args = [ '--no-ff' ]
        return self._fuse_branch(
            "rebase", branch, new_base, *fuse_args, source_repo_url=source_repo_url, local=local
        )

    def _fuse_branch(
        self,
        strategy: str,
        branch: str,
        target_branch: str,
        *fuse_args: Any,
        source_repo_url: Optional[str] = None,
        local: bool = False,
    ) -> str:
        assert source_repo_url or branch != target_branch, branch

        if not local:
            self.fetch("origin")
            target = "origin/" + target_branch
            if source_repo_url:
                self.fetch("source", source_repo_url)
                self.checkout_branch(branch, "source/" + branch)
            else:
                self.checkout_branch(branch, "origin/" + branch)
        else:
            self.checkout_branch(branch)
            target = target_branch

        try:
            self.git(strategy, target, *fuse_args)
        except GitError:
            log.warning("%s failed, doing an --abort", strategy)
            self.git(strategy, "--abort")
            raise
        return self.get_commit_hash()

    def remove_branch(self, branch: str, *, new_current_branch: str = "master") -> None:
        assert branch != new_current_branch
        self.git("branch", "-D", branch)

    def checkout_branch(self, branch: str, start_point: str = "") -> None:
        create_and_reset = "-B" if start_point else ""
        self.git("checkout", create_and_reset, branch, start_point, "--")

    def push(
        self,
        branch: str,
        *,
        source_repo_url: Optional[str] = None,
        force: bool = False,
        skip_ci: bool = False,
    ) -> None:
        self.git("checkout", branch, "--")

        self.git("diff-index", "--quiet", "HEAD")  # check it is not dirty

        untracked_files = self.git(
            "ls-files", "--others"
        ).stdout  # check no untracked files
        if untracked_files:
            raise GitError("There are untracked files", untracked_files)

        if source_repo_url:
            assert self.get_remote_url("source") == source_repo_url
            source = "source"
        else:
            source = "origin"
        force_flag = "--force" if force else ""
        skip_flag = ("-o", "ci.skip") if skip_ci else ()
        self.git("push", force_flag, *skip_flag, source, f"{branch}:{branch}")

    def get_commit_hash(self, rev: str = "HEAD") -> str:
        """Return commit hash for `rev` (default "HEAD")."""
        result = self.git("rev-parse", rev)
        return result.stdout.decode("ascii").strip()

    def get_remote_url(self, name: str) -> str:
        return (
            self.git("config", "--get", f"remote.{name}.url")
            .stdout.decode("utf-8")
            .strip()
        )

    def git(
        self, *args: str, from_repo: bool = True
    ) -> "subprocess.CompletedProcess[bytes]":
        env = None
        if self.ssh_key_file:
            env = os.environ.copy()
            # ssh's handling of identity files is infuriatingly dumb, to get it
            # to actually really use the IdentityFile we pass in via -i we also
            # need to tell it to ignore ssh-agent (IdentitiesOnly=true) and not
            # read in any identities from ~/.ssh/config etc (-F /dev/null),
            # because they append and it tries them in order, starting with config file
            env["GIT_SSH_COMMAND"] = " ".join(
                [
                    GIT_SSH_COMMAND,
                    "-F",
                    "/dev/null",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-i",
                    self.ssh_key_file,
                ]
            )

        command = ["git"]
        if from_repo:
            command.extend(["-C", self.local_path])
        command.extend([arg for arg in args if str(arg)])

        log.debug("Running %s", " ".join(shlex.quote(w) for w in command))
        try:
            timeout_seconds = (
                self.timeout.total_seconds() if self.timeout is not None else None
            )
            return _run(*command, env=env, check=True, timeout=timeout_seconds)
        except subprocess.CalledProcessError as err:
            log.warning("git returned %s", err.returncode)
            log.warning("stdout: %r", err.stdout)
            log.warning("stderr: %r", err.stderr)
            raise GitError(err) from err


def _run(
    *args: Any,
    env: Optional[Dict[str, str]] = None,
    check: bool = False,
    timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[bytes]":
    encoded_args = (
        [a.encode("utf-8") for a in args] if sys.platform != "win32" else args
    )
    with subprocess.Popen(encoded_args, env=env, stdout=PIPE, stderr=PIPE) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except TimeoutExpired as err:
            process.kill()
            stdout, stderr = process.communicate()
            if TYPE_CHECKING:
                assert isinstance(timeout, float)
            raise TimeoutExpired(
                process.args,
                timeout,
                output=stdout,
                stderr=stderr,
            ) from err
        except Exception:
            process.kill()
            process.wait()
            raise
        retcode = process.poll()
        if check and retcode:
            raise subprocess.CalledProcessError(
                retcode,
                process.args,
                output=stdout,
                stderr=stderr,
            )
        if TYPE_CHECKING:
            assert isinstance(retcode, int)
        return subprocess.CompletedProcess(
            args=process.args, returncode=retcode, stdout=stdout, stderr=stderr
        )


class GitError(Exception):
    pass
