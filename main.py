#!/usr/bin/env python3
"""
apt-mc: The Advanced Packaging Tool for Minecraft Servers (Remake)

This script provides a CLI for managing Minecraft server plugins using the Modrinth API.
Refactored for maintainability and TUI support.
"""

import sys
import time
import click
import logging
from pathlib import Path
from rich.table import Table

from core import (
    ModrinthClient, PackageManager, Project, Version, VersionFile,
    CACHE_DIR, PLUGINS_DIR, MODRINTH_API_BASE, LOADERS,
    console, logger
)

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
    for i, loader in enumerate(LOADERS, 1):
        if i > 3: break 
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

    download_queue = []
    for up in updates:
        latest = up['latest_obj']
        files = latest.get("files", [])
        primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)
        
        if not primary_file:
            console.print(f"[red]E: No file found for update of {up['filename']}[/red]")
            continue
            
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
    if len(sys.argv) == 1:
        # Run TUI
        try:
            from tui import AptMcApp
            app = AptMcApp()
            app.run()
        except ImportError as e:
            console.print(f"[red]Failed to load TUI: {e}[/red]")
            console.print("Ensure 'textual' is installed.")
        except Exception as e:
            console.print(f"[red]TUI Error: {e}[/red]")
            import traceback
            traceback.print_exc()
    else:
        cli()