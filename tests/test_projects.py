"""Project registry parity and discovery regressions, using temporary metadata."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from jarvis.codex import CodexClient
from jarvis.config import Config
from jarvis.projects import ProjectRegistry
from jarvis.security import GateError
from jarvis.store import Store
from jarvis.supervisor import Supervisor


class ProjectDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"; self.home.mkdir()
        self.store = Store(self.root / "state.db")
        self.client = CodexClient()
        self.client.require_subscription = AsyncMock()
        self.client.list_threads = AsyncMock(return_value={"data": [], "nextCursor": None})
        self.registry = ProjectRegistry(self.home)
        self.supervisor = Supervisor(Config(), self.store, self.client, registry=self.registry)

    async def asyncTearDown(self):
        self.store.close(); self.temp.cleanup()

    def project(self, folder, name=None, identifier=None):
        path = self.root / folder; path.mkdir(exist_ok=True)
        return {"id": identifier or folder, "name": name or folder, "rootPaths": [str(path)]}

    def write(self, records, assignments=None):
        path = self.home / ".codex-global-state.json"
        path.write_text(json.dumps({"local-projects": {p["id"]: p for p in records},
            "project-order": [p["id"] for p in records], "thread-project-assignments": assignments or {}}), encoding="utf-8")
        return path

    async def test_registered_project_without_any_chats_is_visible_and_selectable(self):
        record = self.project("AIDemo")
        path = self.write([record]); original = path.read_bytes(); stamp = path.stat().st_mtime_ns
        catalog = await self.supervisor.project_catalog()
        self.assertEqual([p["name"] for p in catalog["projects"]], ["AIDemo"])
        self.assertEqual(catalog["chats"], [])
        await self.supervisor.select_voice_project(catalog["projects"][0]["id"])
        self.assertEqual(self.store.setting("selected_project"), record["rootPaths"][0])
        self.assertEqual(path.read_bytes(), original); self.assertEqual(path.stat().st_mtime_ns, stamp)

    async def test_sidebar_name_is_preserved_instead_of_directory_basename(self):
        self.write([self.project("New project", "Hospital Project")])
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(catalog["projects"][0]["name"], "Hospital Project")

    async def test_new_project_invalidates_catalog_without_waiting_for_cache_expiry(self):
        first = self.project("First"); self.write([first])
        await self.supervisor.project_catalog()
        self.write([first, self.project("Newly added")])
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(len(catalog["projects"]), 2)

    async def test_unavailable_folder_remains_registered_and_reports_availability(self):
        self.write([{"id": "missing", "name": "Offline project", "rootPaths": [str(self.root / "missing")]}])
        catalog = await self.supervisor.project_catalog()
        self.assertFalse(catalog["projects"][0]["available"])
        with self.assertRaisesRegex(GateError, "folder is unavailable"):
            await self.supervisor.select_voice_project(catalog["projects"][0]["id"])

    async def test_multi_root_and_shared_folder_projects_are_not_lost(self):
        first = self.project("frontend", "Workspace", "workspace")
        second = self.project("backend")
        first["rootPaths"].extend(second["rootPaths"])
        shared = {"id": "another-workspace", "name": "Another workspace", "rootPaths": [first["rootPaths"][0]]}
        self.write([first, shared])
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(len(catalog["projects"]), 3)
        self.assertEqual(len({p["id"] for p in catalog["projects"]}), 3)

    async def test_scratch_chat_folder_is_not_promoted_into_registered_projects(self):
        project = self.project("Registered"); scratch = self.project("scratch")
        self.write([project])
        self.client.list_threads.return_value = {"data": [{"id": "chat", "cwd": scratch["rootPaths"][0], "name": "Scratch"}], "nextCursor": None}
        catalog = await self.supervisor.project_catalog()
        self.assertEqual([p["name"] for p in catalog["projects"]], ["Registered"])
        self.assertEqual([p["name"] for p in catalog["discovered_folders"]], ["scratch"])

    async def test_project_assignment_maps_a_worktree_chat_to_its_registered_project(self):
        project = self.project("Registered"); worktree = self.project("worktree")
        self.write([project], {"chat": {"projectKind": "local", "projectId": project["id"]}})
        self.client.list_threads.return_value = {"data": [{"id": "chat", "cwd": worktree["rootPaths"][0]}], "nextCursor": None}
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(catalog["chats"][0]["project_id"], catalog["projects"][0]["id"])
        self.assertEqual(catalog["discovered_folders"], [])

    async def test_failed_chat_listing_does_not_erase_registered_projects(self):
        self.write([self.project("Registered")])
        self.client.list_threads.side_effect = GateError("disconnected")
        catalog = await self.supervisor.project_catalog()
        self.assertFalse(catalog["connected"])
        self.assertEqual(catalog["projects"][0]["name"], "Registered")
        self.assertTrue(catalog["discovery_warnings"])

    async def test_transient_malformed_desktop_metadata_preserves_last_good_registry(self):
        path = self.write([self.project("Registered")])
        await self.supervisor.project_catalog()
        path.write_text("{", encoding="utf-8")
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(catalog["projects"][0]["name"], "Registered")
        self.assertIn("Using the last valid registered-project snapshot", catalog["discovery_warnings"])

    async def test_config_fallback_lists_project_with_no_chat(self):
        project = self.project("Configured")
        (self.home / "config.toml").write_text("[projects." + json.dumps(project["rootPaths"][0]) + "]\ntrust_level = \"trusted\"\n", encoding="utf-8")
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(catalog["projects"][0]["name"], "Configured")
        self.assertEqual(catalog["project_source"], "codex_config")
        self.assertFalse(catalog["registered_projects_available"])

    async def test_chat_listing_continues_beyond_four_pages(self):
        project = self.project("Late project")
        pages = [{"data": [{"id": "chat-" + str(i)}], "nextCursor": "page-" + str(i + 1)} for i in range(5)]
        pages.append({"data": [{"id": "late", "cwd": project["rootPaths"][0]}], "nextCursor": None})
        self.client.list_threads.side_effect = pages
        catalog = await self.supervisor.project_catalog()
        self.assertEqual(catalog["projects"][0]["name"], "Late project")
        self.assertEqual(self.client.list_threads.await_count, 6)

    async def test_chat_filters_include_all_providers_and_desktop_worker_sources(self):
        self.client.request = AsyncMock(return_value={"data": [], "nextCursor": None})
        await CodexClient.list_threads(self.client, "cursor")
        params = self.client.request.call_args.args[1]
        self.assertEqual(params["modelProviders"], [])
        self.assertTrue({"vscode", "appServer", "exec", "subAgent"}.issubset(params["sourceKinds"]))
        self.assertEqual(params["sortKey"], "created_at")
        self.assertEqual(params["cursor"], "cursor")


if __name__ == "__main__": unittest.main()
