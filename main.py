#!/usr/bin/env python3
import click
import requests
import os
import time
import json
from typing import List, Optional, Dict, Any
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn, TextColumn

console = Console()

class ModrinthAPI:
    BASE_URL = "https://api.modrinth.com/v2"
    HEADERS = {"User-Agent": "apt-mc/1.0 (parody-cli)"}

    @staticmethod
    def search(query: str, limit: int = 10) -> List[Dict[str, Any]]:
        facets = '[["project_type:plugin"], ["categories:spigot", "categories:paper", "categories:purpur", "categories:bukkit"]]'
        response = requests.get(
            f"{ModrinthAPI.BASE_URL}/search",
            params={"query": query, "facets": facets, "limit": limit},
            headers=ModrinthAPI.HEADERS
        )
        response.raise_for_status()
        return response.json().get("hits", [])

    @staticmethod
    def get_project(project_id_or_slug: str) -> Optional[Dict[str, Any]]:
        response = requests.get(
            f"{ModrinthAPI.BASE_URL}/project/{project_id_or_slug}",
            headers=ModrinthAPI.HEADERS
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    @staticmethod
    def get_versions(project_id: str, loaders: List[str]) -> List[Dict[str, Any]]:
        res = requests.get(
            f"{ModrinthAPI.BASE_URL}/project/{project_id}/version",
            params={"loaders": json.dumps(loaders)},
            headers=ModrinthAPI.HEADERS
        )
        res.raise_for_status()
        return res.json()

class PackageManager:
    def __init__(self, plugins_dir: str = "plugins"):
        self.plugins_dir = plugins_dir

    def ensure_dir(self):
        if not os.path.exists(self.plugins_dir):
            os.makedirs(self.plugins_dir)

    def download_file(self, url: str, filename: str, size: int):
        dest_path = os.path.join(self.plugins_dir, filename)
        with requests.get(url, stream=True, headers=ModrinthAPI.HEADERS) as r:
            r.raise_for_status()
            with Progress(
                TextColumn("[bold blue]{task.fields[filename]}", justify="right"),
                BarColumn(bar_width=None),
                "[progress.percentage]{task.percentage:>3.1f}%",
                "•", DownloadColumn(), "•", TransferSpeedColumn(), "•",
                TextColumn("[green]Done[/green]"),
                console=console
            ) as progress:
                task = progress.add_task("download", filename=filename, total=size)
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        progress.update(task, advance=len(chunk))

@click.group()
def cli():
    """apt-mc: The Advanced Packaging Tool for Minecraft Servers."""
    pass

@cli.command()
def update():
    """Update list of available packages."""
    for i, loader in enumerate(["spigot", "paper", "purpur"], 1):
        with console.status(f"[bold white]Hit:{i} https://api.modrinth.com/v2/search {loader}[/bold white]"):
            time.sleep(0.3)
            console.print(f"Hit:{i} https://api.modrinth.com/v2/search {loader}")
    
    console.print("Reading package lists... [green]Done[/green]")
    console.print("Building dependency tree... [green]Done[/green]")
    console.print("Reading state information... [green]Done[/green]")
    console.print(f"\n{os.getcwd()} is up to date.")

@cli.command()
@click.argument("query")
def search(query):
    """Search for plugins."""
    console.print(f"Sorting... [green]Done[/green]")
    console.print(f"Full Text Search... [green]Done[/green]")

    try:
        hits = ModrinthAPI.search(query)
        if not hits:
            console.print(f"No plugins found for '{query}'.")
            return

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Package Name", style="green")
        table.add_column("Description")
        table.add_column("Author", style="cyan")
        table.add_column("Downloads", justify="right")

        for hit in hits:
            desc = hit["description"][:50] + "..." if len(hit["description"]) > 50 else hit["description"]
            table.add_row(hit["slug"], desc, hit["author"], str(hit["downloads"]))
        
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

    pm = PackageManager()
    pm.ensure_dir()
    
    console.print("Reading package lists... [green]Done[/green]")
    console.print("Building dependency tree... [green]Done[/green]")
    
    to_install = []
    for pkg_slug in packages:
        console.print(f"Check {pkg_slug}...")
        project = ModrinthAPI.get_project(pkg_slug)
        if project:
            to_install.append(project)
        else:
            console.print(f"[red]E: Unable to locate package {pkg_slug}[/red]")

    if not to_install:
        return

    console.print(f"\nThe following NEW packages will be installed:")
    for pkg in to_install:
        console.print(f"  {pkg['slug']}")
    
    console.print(f"\n0 upgraded, {len(to_install)} newly installed, 0 to remove and 0 not upgraded.")
    
    for pkg in to_install:
        try:
            versions = ModrinthAPI.get_versions(pkg['id'], ["spigot", "paper", "purpur", "bukkit"])
            if not versions:
                console.print(f"[red]E: No compatible versions for {pkg['slug']}[/red]")
                continue
            
            latest = versions[0]
            primary_file = next((f for f in latest["files"] if f.get("primary")), latest["files"][0])
            pm.download_file(primary_file["url"], primary_file["filename"], primary_file["size"])
        except Exception as e:
            console.print(f"[red]E: Failed to install {pkg['slug']}: {e}[/red]")

@cli.command()
@click.argument("package")
def remove(package):
    """Remove a plugin."""
    console.print(f"Reading package lists... [green]Done[/green]")
    console.print(f"Building dependency tree... [green]Done[/green]")
    
    pm = PackageManager()
    if not os.path.exists(pm.plugins_dir):
        console.print(f"[red]E: Unable to locate package {package}[/red]")
        return
        
    candidates = [f for f in os.listdir(pm.plugins_dir) if f.endswith(".jar") and package.lower() in f.lower()]
    if not candidates:
        console.print(f"[red]E: Unable to locate package {package}[/red]")
        return
        
    if len(candidates) > 1:
        console.print(f"[red]E: Multiple candidates found for {package}. Be more specific.[/red]")
        return
        
    target = candidates[0]
    os.remove(os.path.join(pm.plugins_dir, target))
    console.print(f"Removing {package} ({target})...")

if __name__ == "__main__":
    cli()
