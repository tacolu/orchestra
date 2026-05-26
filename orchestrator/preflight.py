"""Pre-flight checks — verify and repair environment before launching worker.

Runs sequential checks. If a check fails, attempts auto-repair.
If repair fails, returns the error to abort spawn (without wasting budget).
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("orchestrator.preflight")


@dataclass
class PreflightResult:
    ok: bool
    cwd: str = ""
    base_branch: str = "main"
    repairs: list[str] = field(default_factory=list)
    error: str = ""


class PreflightChecker:
    """Verifies the environment is ready for a worker.

    Checks (in order):
    1. Repo exists locally (if not: init or clone)
    2. Base branch exists (detect main/master, create if missing)
    3. Integration branch exists (create from base if missing)
    4. Basic dependencies present (requirements.txt or Cargo.toml)
    5. Minimum project structure exists
    """

    def __init__(self, workspace_dir: str, github_org: str = "", repo_map: dict | None = None):
        self.workspace_dir = workspace_dir
        self.github_org = github_org
        self.repo_map = repo_map or {}

    async def check(
        self,
        frente: dict,
        integration_branch: str = "",
        task_data: dict | None = None,
    ) -> PreflightResult:
        result = PreflightResult(ok=False)
        slug = frente.get("slug", "unknown")

        # 1. Verify/create repo directory
        cwd = await self._check_repo_exists(frente, slug)
        if not cwd:
            result.error = f"Could not obtain/create repo for {slug}"
            return result
        result.cwd = cwd

        # 2. Verify base branch (detect main vs master)
        base_branch = await self._check_base_branch(cwd, frente)
        if not base_branch:
            result.error = f"No base branch found in {cwd}"
            return result
        result.base_branch = base_branch
        if base_branch != frente.get("branch", "main"):
            result.repairs.append(f"Base branch detected: {base_branch} (not '{frente.get('branch', 'main')}')")

        # 3. Integration branch
        if integration_branch:
            repaired = await self._check_integration_branch(cwd, integration_branch, base_branch)
            if repaired:
                result.repairs.append(f"Integration branch '{integration_branch}' created from {base_branch}")

        # 4. Minimum project structure
        structure_repair = await self._check_project_structure(cwd, task_data or {})
        if structure_repair:
            result.repairs.extend(structure_repair)

        # 5. Dependencies (verify only, don't install — worker will do it if needed)
        deps_warning = self._check_dependencies(cwd)
        if deps_warning:
            result.repairs.append(deps_warning)

        result.ok = True
        if result.repairs:
            log.info("Preflight %s: %d repairs applied: %s", slug, len(result.repairs), "; ".join(result.repairs))
        else:
            log.debug("Preflight %s: all OK", slug)
        return result

    # -- Check 1: Repo exists --

    async def _check_repo_exists(self, frente: dict, slug: str) -> str | None:
        if self.workspace_dir:
            local_path = Path(self.workspace_dir) / slug
            if local_path.joinpath(".git").exists():
                return str(local_path)

            if self.github_org:
                repo_name = self.repo_map.get(slug, slug)
                cloned = await self._try_clone(repo_name, str(local_path))
                if cloned:
                    return str(local_path)

            return await self._init_repo(str(local_path), slug)

        return None

    async def _try_clone(self, repo_name: str, dest: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "repo", "clone", f"{self.github_org}/{repo_name}", dest,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode == 0:
                log.info("Repo cloned: %s/%s -> %s", self.github_org, repo_name, dest)
                return True
            err = stderr.decode().strip()
            if "Could not resolve" in err or "not found" in err.lower():
                log.debug("Repo %s/%s not found on GitHub, creating local", self.github_org, repo_name)
            else:
                log.warning("Clone failed: %s", err[:200])
        except Exception as e:
            log.debug("Clone error: %s", e)
        return False

    async def _init_repo(self, path: str, slug: str) -> str | None:
        try:
            os.makedirs(path, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "git", "init", "--initial-branch=master",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=path,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                proc = await asyncio.create_subprocess_exec(
                    "git", "init",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    cwd=path,
                )
                await asyncio.wait_for(proc.communicate(), timeout=10)

            gitignore = Path(path) / ".gitignore"
            gitignore.write_text("__pycache__/\n*.pyc\n.venv/\n.env\n", encoding="utf-8")

            await self._git(path, "add", ".gitignore")
            await self._git(path, "commit", "-m", f"init: {slug} (auto-created by orchestrator)")

            log.info("Local repo created: %s", path)
            return path
        except Exception as e:
            log.error("Error creating local repo %s: %s", path, e)
            return None

    # -- Check 2: Base branch --

    async def _check_base_branch(self, cwd: str, frente: dict) -> str | None:
        configured = frente.get("branch", "main")

        if await self._branch_exists(cwd, configured):
            return configured

        for candidate in ("main", "master", "develop"):
            if await self._branch_exists(cwd, candidate):
                log.info("Base branch '%s' not found, using '%s'", configured, candidate)
                return candidate

        try:
            branch = await self._git(cwd, "branch", "--show-current")
            if branch:
                return branch
        except Exception:
            pass

        try:
            await self._git(cwd, "checkout", "-b", "master")
            return "master"
        except Exception:
            pass

        return None

    async def _branch_exists(self, cwd: str, branch: str) -> bool:
        try:
            await self._git(cwd, "rev-parse", "--verify", branch)
            return True
        except (RuntimeError, Exception):
            return False

    # -- Check 3: Integration branch --

    async def _check_integration_branch(self, cwd: str, int_branch: str, base: str) -> bool:
        if await self._branch_exists(cwd, int_branch):
            return False
        try:
            await self._git(cwd, "branch", int_branch, base)
            log.info("Integration branch '%s' created from '%s'", int_branch, base)
            return True
        except Exception as e:
            log.warning("Error creating integration branch: %s", e)
            return False

    # -- Check 4: Project structure --

    async def _check_project_structure(self, cwd: str, task_data: dict) -> list[str]:
        repairs = []
        archivos_glob = task_data.get("archivos_glob", "")

        if not archivos_glob:
            return repairs

        for g in archivos_glob.split(","):
            g = g.strip()
            if not g:
                continue
            parent = os.path.dirname(g)
            if not parent or parent == ".":
                continue
            full_dir = os.path.join(cwd, parent)
            if not os.path.isdir(full_dir):
                os.makedirs(full_dir, exist_ok=True)
                if any(g.endswith(ext) for ext in (".py",)):
                    init_path = os.path.join(full_dir, "__init__.py")
                    if not os.path.exists(init_path):
                        Path(init_path).write_text("", encoding="utf-8")
                repairs.append(f"Directory created: {parent}/")

        return repairs

    # -- Check 5: Dependencies --

    def _check_dependencies(self, cwd: str) -> str:
        has_python = os.path.exists(os.path.join(cwd, "requirements.txt")) or \
                     os.path.exists(os.path.join(cwd, "pyproject.toml"))
        has_rust = os.path.exists(os.path.join(cwd, "Cargo.toml"))

        if not has_python and not has_rust:
            py_files = [f for f in Path(cwd).rglob("*.py") if f.name != "__init__.py"]
            if py_files:
                req_path = os.path.join(cwd, "requirements.txt")
                Path(req_path).write_text(
                    "# Auto-generated by orchestrator preflight\nfastapi>=0.100\nuvicorn[standard]\nsqlalchemy[asyncio]>=2.0\nalembic\npydantic>=2.0\npydantic-settings\nhttpx\n",
                    encoding="utf-8",
                )
                return "requirements.txt created (auto-detect: Python project without dependencies)"
        return ""

    # -- Helpers --

    async def _git(self, cwd: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {stderr.decode().strip()[:200]}")
        return stdout.decode().strip()
