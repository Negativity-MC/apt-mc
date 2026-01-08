"Core logic for apt-mc."

import concurrent.futures
import threading
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import requests
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TransferSpeedColumn,
)

# --- Configuration ---

CACHE_DIR = Path(".apt-mc-cache")
PLUGINS_DIR = Path("plugins")
MODRINTH_API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "apt-mc/2.0 (refactored-cli)"
LOADERS = ["spigot", "paper", "purpur", "bukkit"]

# --- Logging / Output ---

console = Console()
logger = logging.getLogger("apt-mc")
logging.basicConfig(level=logging.ERROR, format="%(message)s")


# --- Domain Models ---

@dataclass
class Project:
    id: str
    slug: str
    description: str
    body: str
    author: str
    downloads: int
    categories: List[str]
    license: Optional[str]
    source_url: Optional[str] = None
    wiki_url: Optional[str] = None
    discord_url: Optional[str] = None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Project":
        return cls(
            id=data.get("id", ""),
            slug=data.get("slug", ""),
            description=data.get("description", ""),
            body=data.get("body", ""),
            author=data.get("author", "Unknown"),
            downloads=data.get("downloads", 0),
            categories=data.get("categories", []),
            license=data.get("license", {}).get("name") if isinstance(data.get("license"), dict) else data.get("license"),
            source_url=data.get("source_url"),
            wiki_url=data.get("wiki_url"),
            discord_url=data.get("discord_url"),
        )

@dataclass
class VersionFile:
    url: str
    filename: str
    size: int
    primary: bool
    hashes: Dict[str, str]

@dataclass
class Version:
    id: str
    project_id: str
    version_number: str
    files: List[VersionFile]
    dependencies: List[Dict[str, Any]]

    @property
    def primary_file(self) -> Optional[VersionFile]:
        return next((f for f in self.files if f.primary), self.files[0] if self.files else None)


# --- Services ---

class ModrinthClient:
    """Handles interaction with the Modrinth API."""

    def __init__(self, base_url: str = MODRINTH_API_BASE, user_agent: str = USER_AGENT):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = self.session.get(f"{self.base_url}/{endpoint}", params=params)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def _post(self, endpoint: str, json_data: Any) -> Any:
        response = self.session.post(f"{self.base_url}/{endpoint}", json=json_data)
        response.raise_for_status()
        return response.json()

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        # Correctly formatted facets for Modrinth API: serialized JSON array of arrays
        facets = '[["project_type:plugin"], ["categories:spigot", "categories:paper", "categories:purpur", "categories:bukkit"]]'
        data = self._get("search", params={"query": query, "facets": facets, "limit": limit})
        return data.get("hits", []) if data else []

    def get_project(self, project_id_or_slug: str) -> Optional[Dict[str, Any]]:
        return self._get(f"project/{project_id_or_slug}")

    def get_versions(self, project_id: str, loaders: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        params = {}
        if loaders:
            params["loaders"] = json.dumps(loaders)
        return self._get(f"project/{project_id}/version", params=params) or []

    def get_version_by_hash(self, file_hash: str, algorithm: str = "sha1") -> Optional[Dict[str, Any]]:
        return self.get_versions_by_hashes([file_hash], algorithm).get(file_hash)

    def get_versions_by_hashes(self, hashes: List[str], algorithm: str = "sha1") -> Dict[str, Any]:
        if not hashes:
            return {}
        return self._post("version_files", json={"hashes": hashes, "algorithm": algorithm})

    def get_members(self, project_id_or_slug: str) -> List[Dict[str, Any]]:
        return self._get(f"project/{project_id_or_slug}/members") or []


class SilentProgress:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def add_task(self, *args, **kwargs): return 0
    def update(self, *args, **kwargs): pass
    def start_task(self, *args, **kwargs): pass
    def remove_task(self, *args, **kwargs): pass
    def stop(self): pass
    @property
    def tasks(self): return []

class PackageManager:
    """Handles local file operations and package logic."""

    def __init__(self, plugins_dir: Path, api: ModrinthClient):
        self.plugins_dir = plugins_dir
        self.api = api

    def ensure_plugins_dir(self):
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def calculate_sha1(self, filepath: Path) -> str:
        sha1 = hashlib.sha1()
        with filepath.open('rb') as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                sha1.update(data)
        return sha1.hexdigest()

    def get_installed_plugins(self) -> Dict[str, str]:
        """Returns a dict of filename -> sha1 for all .jar files in plugins dir."""
        if not self.plugins_dir.exists():
            return {}
        
        plugins = {}
        for f in self.plugins_dir.glob("*.jar"):
            plugins[f.name] = self.calculate_sha1(f)
        return plugins

    def download_files(self, files: List[Dict[str, Any]], quiet: bool = False) -> None:
        """
        Downloads multiple files in parallel.
        files: List of dicts with keys {'url', 'filename', 'size'}
        quiet: If True, suppresses the progress bar.
        """
        self.ensure_plugins_dir()
        if not files:
            return

        # Events to signal workers to stop
        abort_events = {f['filename']: threading.Event() for f in files}
        ignored_slow_files = set()

        if quiet:
            progress_ctx = SilentProgress()
        else:
            progress_ctx = Progress(
                TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
                BarColumn(bar_width=None),
                "[progress.percentage]{task.percentage:>3.1f}%",
                "•", DownloadColumn(), "•", TransferSpeedColumn(), "•",
                TextColumn("[green]Done[/green]"),
                console=console
            )

        with progress_ctx as progress:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                active_futures = {} # Future -> (file_info, task_id)

                def submit_task(file_info):
                    # Reset event
                    abort_events[file_info['filename']].clear()
                    
                    t_id = progress.add_task("download", filename=file_info['filename'], total=file_info['size'], start=False)
                    
                    future = executor.submit(
                        self._download_worker, 
                        file_info['url'], 
                        file_info['filename'], 
                        t_id, 
                        progress, 
                        abort_events[file_info['filename']]
                    )
                    active_futures[future] = (file_info, t_id)

                # Initial submission
                for f in files:
                    submit_task(f)

                while active_futures:
                    # Wait for any completion or timeout to check speeds
                    done, _ = concurrent.futures.wait(
                        active_futures.keys(), 
                        timeout=1.0, 
                        return_when=concurrent.futures.FIRST_COMPLETED
                    )

                    # Handle completed/aborted tasks
                    for fut in done:
                        f_info, t_id = active_futures.pop(fut)
                        try:
                            result = fut.result()
                            if result == "ABORTED":
                                # Remove old task visual
                                progress.remove_task(t_id)
                                # Resubmit
                                submit_task(f_info)
                        except Exception as e:
                            console.print(f"[red]E: Download failed for {f_info['filename']}: {e}[/red]")
                    
                    # Monitor speeds
                    active_tasks = [t for t in progress.tasks if not t.finished and t.started and t.speed is not None]
                    if len(active_tasks) > 1:
                        avg_speed = sum(t.speed for t in active_tasks) / len(active_tasks)
                        
                        for t in active_tasks:
                            fname = t.fields['filename']
                            if fname in ignored_slow_files:
                                continue
                            
                            # Criteria: >3s elapsed, <50% avg speed
                            if t.elapsed > 3 and t.speed < (avg_speed * 0.5):
                                # Prompt user
                                progress.stop()
                                try:
                                    msg = f"\n! {fname} is slow ({t.speed/1024:.1f} KB/s vs Avg {avg_speed/1024:.1f} KB/s)."
                                    if click.confirm(f"{msg} Restart download?", default=True):
                                        abort_events[fname].set()
                                        console.print(f"[yellow]Restarting {fname}...[/yellow]")
                                    else:
                                        ignored_slow_files.add(fname)
                                finally:
                                    progress.start()

    def _download_worker(self, url: str, filename: str, task_id: TaskID, progress: Progress, abort_event: threading.Event):
        dest_path = self.plugins_dir / filename
        try:
            with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}) as r:
                r.raise_for_status()
                progress.start_task(task_id)
                with dest_path.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if abort_event.is_set():
                            return "ABORTED"
                        f.write(chunk)
                        progress.update(task_id, advance=len(chunk))
        except Exception as e:
            if dest_path.exists():
                dest_path.unlink()
            raise e

    def remove_plugin(self, filename: str) -> bool:
        target = self.plugins_dir / filename
        if target.exists():
            target.unlink()
            return True
        return False

    def find_plugin_file(self, query: str) -> List[str]:
        """Finds installed plugin filenames matching the query."""
        if not self.plugins_dir.exists():
            return []
        return [f.name for f in self.plugins_dir.glob("*.jar") if query.lower() in f.name.lower()]