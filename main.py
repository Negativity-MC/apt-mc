#!/usr/bin/env python3
"""
apt-mc: The Advanced Packaging Tool for Minecraft Servers (Remake)

This script provides a CLI for managing Minecraft server plugins using the Modrinth API.
Refactored for maintainability.
"""

import concurrent.futures
import threading
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Generator

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
from rich.table import Table

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
    author: str
    downloads: int
    categories: List[str]
    license: Optional[str]
    source_url: Optional[str] = None
    wiki_url: Optional[str] = None
    discord_url: Optional[str] = None

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Project":
        # Extract author safely (logic from original)
        author = "Unknown" 
        # Note: The original searched members separately. 
        # Here we will just store what's available or fetch if needed.
        # The search result has 'author' field, project endpoint does not (needs members).
        # We'll adapt based on context.
        
        return cls(
            id=data.get("id", ""),
            slug=data.get("slug", ""),
            description=data.get("description", ""),
            author=data.get("author", "Unknown"), # Only present in search hits
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
        # This endpoint can handle multiple hashes, but we'll wrap it for single use convenience too
        return self.get_versions_by_hashes([file_hash], algorithm).get(file_hash)

    def get_versions_by_hashes(self, hashes: List[str], algorithm: str = "sha1") -> Dict[str, Any]:
        if not hashes:
            return {}
        return self._post("version_files", json={"hashes": hashes, "algorithm": algorithm})

    def get_members(self, project_id_or_slug: str) -> List[Dict[str, Any]]:
        return self._get(f"project/{project_id_or_slug}/members") or []


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

    def download_files(self, files: List[Dict[str, Any]]) -> None:
        """
        Downloads multiple files in parallel.
        files: List of dicts with keys {'url', 'filename', 'size'}
        """
        self.ensure_plugins_dir()
        if not files:
            return

        # Events to signal workers to stop
        abort_events = {f['filename']: threading.Event() for f in files}
        ignored_slow_files = set()

        with Progress(
            TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
            BarColumn(bar_width=None),
            "[progress.percentage]{task.percentage:>3.1f}%",
            "•", DownloadColumn(), "•", TransferSpeedColumn(), "•",
            TextColumn("[green]Done[/green]"),
            console=console
        ) as progress:
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
                                    msg = f"\n[yellow]![/yellow] [bold]{fname}[/bold] is slow ({t.speed/1024:.1f} KB/s vs Avg {avg_speed/1024:.1f} KB/s)."
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


# --- CLI Commands ---

@click.group()
def cli():
    """apt-mc: The Advanced Packaging Tool for Minecraft Servers."""
    pass


@cli.command()
@click.argument("package")
def info(package):
    """Show details about a package."""
    api = ModrinthClient()
    pm = PackageManager(PLUGINS_DIR, api)

    try:
        project_data = api.get_project(package)
        if not project_data:
            console.print(f"[red]E: Unable to locate package {package}[/red]")
            return

        # Basic Info
        slug = project_data.get('slug')
        pid = project_data.get('id')
        
        # Determine Installation Status
        status = "[red]Not Installed[/red]"
        installed_hashes = list(pm.get_installed_plugins().values())
        if installed_hashes:
            version_map = api.get_versions_by_hashes(installed_hashes)
            for v in version_map.values():
                if v['project_id'] == pid:
                    status = "[green]Installed[/green]"
                    break

        # Author
        author = "Unknown"
        members = api.get_members(slug)
        if members:
            owner = next((m for m in members if m.get("role") == "Owner"), members[0])
            author = owner.get("user", {}).get("username", "Unknown")

        # Dependencies (from latest version)
        dependencies_str = "None"
        versions = api.get_versions(pid, LOADERS)
        if versions:
            latest = versions[0]
            deps = latest.get("dependencies", [])
            req_deps = [d['project_id'] for d in deps if d.get("dependency_type") == "required" and d.get("project_id")]
            
            if req_deps:
                # We won't resolve all names to avoid N requests in 'info' unless necessary,
                # but the original did it, so we will try to mimic best effort or just show IDs
                # For speed, we might skip full resolution or do it parallel, but let's stick to simple sequential for now as per original logic.
                dep_names = []
                for dep_id in req_deps:
                    try:
                        d_proj = api.get_project(dep_id)
                        dep_names.append(d_proj['slug'] if d_proj else dep_id)
                    except:
                        dep_names.append(dep_id)
                dependencies_str = ", ".join(dep_names)

        # Output
        console.print(f"[bold white]Package:[/bold white] {slug}")
        console.print(f"[bold white]ID:[/bold white] {pid}")
        console.print(f"[bold white]Status:[/bold white] {status}")
        console.print(f"[bold white]Author:[/bold white] {author}")
        console.print(f"[bold white]Description:[/bold white] {project_data.get('description')}")
        license_name = project_data.get('license', {}).get('name') if isinstance(project_data.get('license'), dict) else "Unknown"
        console.print(f"[bold white]License:[/bold white] {license_name}")
        console.print(f"[bold white]Categories:[/bold white] {', '.join(project_data.get('categories', []))}")
        console.print(f"[bold white]Downloads:[/bold white] {project_data.get('downloads')}")
        console.print(f"[bold white]Dependencies:[/bold white] {dependencies_str}")
        
        links = [
            project_data.get('wiki_url'),
            project_data.get('source_url'),
            project_data.get('discord_url')
        ]
        website = next((l for l in links if l), 'N/A')
        console.print(f"[bold white]Website:[/bold white] {website}")

    except Exception as e:
        console.print(f"[red]E: Failed to fetch info: {e}[/red]")
        # logger.exception(e) # Uncomment for debug


@cli.command("list")
@click.option("--installed", is_flag=True, default=True, help="List installed packages (default).")
def list_packages(installed):
    """List installed packages."""
    if not installed:
        console.print("Listing available packages is not supported yet (too many).")
        return

    pm = PackageManager(PLUGINS_DIR, ModrinthClient())
    installed_plugins = pm.get_installed_plugins()

    if not installed_plugins:
        console.print("No plugins installed.")
        return

    console.print("Listing... [green]Done[/green]")

    try:
        versions_map = pm.api.get_versions_by_hashes(list(installed_plugins.values()))
    except Exception as e:
        console.print(f"[red]E: Failed to resolve versions: {e}[/red]")
        return

    sha1_to_filename = {v: k for k, v in installed_plugins.items()}

    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column("Package", style="green")
    table.add_column("Version")
    table.add_column("Status")

    for file_sha1, filename in sha1_to_filename.items():
        version_info = versions_map.get(file_sha1)
        if version_info:
            pkg_name = version_info.get('project_id', filename)
            ver_num = version_info.get('version_number', 'unknown')
        else:
            pkg_name = filename
            ver_num = "unknown"
        
        table.add_row(str(pkg_name), str(ver_num), "[installed]")

    console.print(table)


@cli.command()
def update():
    """Update list of available packages (Simulation)."""
    # In a real package manager, this would update local DB.
    # Here we just simulate network activity as a nod to 'apt update'.
    for i, loader in enumerate(LOADERS, 1):
        if i > 3: break # Limit output
        url = f"{MODRINTH_API_BASE}/search"
        with console.status(f"[bold white]Hit:{i} {url} {loader}[/bold white]"):
            time.sleep(0.3)
            console.print(f"Hit:{i} {url} {loader}")

    console.print("Reading package lists... [green]Done[/green]")
    console.print("Building dependency tree... [green]Done[/green]")
    console.print("Reading state information... [green]Done[/green]")
    console.print(f"\n{Path.cwd()} is up to date.")


@cli.command()
@click.argument("query")
def search(query):
    """Search for plugins."""
    console.print("Sorting... [green]Done[/green]")
    console.print("Full Text Search... [green]Done[/green]")

    api = ModrinthClient()
    try:
        hits = api.search(query)
        if not hits:
            console.print(f"No plugins found for '{query}'.")
            return

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Package Name", style="green")
        table.add_column("Description")
        table.add_column("Author", style="cyan")
        table.add_column("Downloads", justify="right")

        for hit in hits:
            desc = hit.get("description", "")
            if len(desc) > 50:
                desc = desc[:50] + "..."
            table.add_row(
                hit.get("slug"),
                desc,
                hit.get("author"),
                str(hit.get("downloads"))
            )
        console.print(table)
    except Exception as e:
        console.print(f"[red]E: Failed to search: {e}[/red]")


@cli.command()
@click.argument("packages", nargs=-1)
def install(packages):
    """Install plugins."""
    if not packages:
        console.print("[red]E: No packages specified.[/red]")
        return

    api = ModrinthClient()
    pm = PackageManager(PLUGINS_DIR, api)
    pm.ensure_plugins_dir()

    console.print("Reading package lists... [green]Done[/green]")
    console.print("Building dependency tree... [green]Done[/green]")

    to_install_projects = []
    
    # Resolve packages
    for pkg_slug in packages:
        console.print(f"Check {pkg_slug}...")
        p = api.get_project(pkg_slug)
        if p:
            to_install_projects.append(p)
        else:
            console.print(f"[red]E: Unable to locate package {pkg_slug}[/red]")

    if not to_install_projects:
        return

    console.print(f"\nThe following NEW packages will be installed:")
    for p in to_install_projects:
        console.print(f"  {p['slug']}")

    console.print(f"\n0 upgraded, {len(to_install_projects)} newly installed, 0 to remove and 0 not upgraded.")

    download_queue = []
    for p in to_install_projects:
        try:
            versions = api.get_versions(p['id'], LOADERS)
            if not versions:
                console.print(f"[red]E: No compatible versions for {p['slug']}[/red]")
                continue
            
            # Simple strategy: get first compatible version, first primary file
            latest = versions[0]
            files = latest.get("files", [])
            primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)
            
            if primary_file:
                download_queue.append({
                    "url": primary_file["url"],
                    "filename": primary_file["filename"],
                    "size": primary_file["size"]
                })
            else:
                console.print(f"[red]E: No files found for {p['slug']}[/red]")
        except Exception as e:
            console.print(f"[red]E: Failed to prepare {p['slug']}: {e}[/red]")

    if download_queue:
        pm.download_files(download_queue)


@cli.command()
def upgrade():
    """Upgrade installed plugins."""
    console.print("Reading package lists... [green]Done[/green]")
    console.print("Building dependency tree... [green]Done[/green]")
    console.print("Reading state information... [green]Done[/green]")
    console.print("Calculating upgrades... ", end="")

    api = ModrinthClient()
    pm = PackageManager(PLUGINS_DIR, api)
    installed = pm.get_installed_plugins()

    if not installed:
        console.print("[green]Done[/green]")
        console.print("0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.")
        return

    # Check for updates
    updates = []
    try:
        versions_map = api.get_versions_by_hashes(list(installed.values()))
    except Exception as e:
        console.print(f"[red]E: Failed to check for updates: {e}[/red]")
        return
        
    sha1_to_filename = {v: k for k, v in installed.items()}

    for file_sha1, current_ver_info in versions_map.items():
        if not current_ver_info:
            continue
        
        pid = current_ver_info['project_id']
        
        try:
            available = api.get_versions(pid, LOADERS)
            if not available:
                continue
            
            latest = available[0]
            if latest['id'] != current_ver_info['id']:
                updates.append({
                    "filename": sha1_to_filename.get(file_sha1, "Unknown"),
                    "project_id": pid,
                    "current_version": current_ver_info['version_number'],
                    "new_version": latest['version_number'],
                    "latest_obj": latest
                })
        except Exception:
            pass

    console.print("[green]Done[/green]")

    if not updates:
        console.print("0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.")
        return

    console.print(f"\nThe following packages will be upgraded:")
    for up in updates:
        console.print(f"  {up['filename']} ({up['current_version']} -> {up['new_version']})")

    console.print(f"\n{len(updates)} upgraded, 0 newly installed, 0 to remove and 0 not upgraded.")

    if not click.confirm("Do you want to continue?", default=True):
        console.print("Abort.")
        return

    # Perform Upgrades
    download_queue = []
    for up in updates:
        latest = up['latest_obj']
        files = latest.get("files", [])
        primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)
        
        if not primary_file:
            console.print(f"[red]E: No file found for update of {up['filename']}[/red]")
            continue
            
        # Remove old
        pm.remove_plugin(up['filename'])
        
        download_queue.append({
            "url": primary_file["url"],
            "filename": primary_file["filename"],
            "size": primary_file["size"]
        })

    if download_queue:
        try:
            pm.download_files(download_queue)
        except Exception as e:
            console.print(f"[red]E: Failed during upgrade download: {e}[/red]")


@cli.command()
@click.argument("package")
def remove(package):
    """Remove a plugin."""
    console.print("Reading package lists... [green]Done[/green]")
    console.print("Building dependency tree... [green]Done[/green]")

    pm = PackageManager(PLUGINS_DIR, ModrinthClient())
    
    candidates = pm.find_plugin_file(package)
    
    if not candidates:
        console.print(f"[red]E: Unable to locate package {package}[/red]")
        return
    
    if len(candidates) > 1:
        console.print(f"[red]E: Multiple candidates found for {package}: {', '.join(candidates)}. Be more specific.[/red]")
        return

    target = candidates[0]
    if pm.remove_plugin(target):
        console.print(f"Removing {package} ({target})...")
    else:
        console.print(f"[red]E: Failed to remove {target}[/red]")


if __name__ == "__main__":
    cli()
