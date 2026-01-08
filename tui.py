from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, DataTable, Input, Button, Label, TabbedContent, TabPane, Static, Markdown
from textual.screen import Screen, ModalScreen
from textual import on, work
from textual.binding import Binding
from textual.timer import Timer

from core import PackageManager, ModrinthClient, PLUGINS_DIR, LOADERS, Project
import asyncio
import sys

# --- Screens ---

class PluginDetailScreen(ModalScreen):
    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("i", "install", "Install"),
    ]

    CSS = """
    PluginDetailScreen {
        align: center middle;
    }

    .detail-dialog {
        width: 80%;
        height: 80%;
        background: $surface;
        border: wide $accent;
        padding: 1 2;
        layout: vertical;
    }

    .detail-header {
        dock: top;
        height: 3;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $text;
        margin-bottom: 1;
    }

    .detail-content {
        height: 1fr;
        overflow-y: scroll;
    }

    .detail-actions {
        dock: bottom;
        height: 3;
        align: center middle;
        margin-top: 1;
    }

    .detail-actions Button {
        margin: 0 1;
    }
    
    .stat-bar {
        height: auto;
        margin-bottom: 1;
        color: $text-muted;
    }
    """

    def __init__(self, slug: str, api: ModrinthClient, install_callback):
        super().__init__()
        self.slug = slug
        self.api = api
        self.install_callback = install_callback
        self.project: Project | None = None

    def compose(self) -> ComposeResult:
        with Container(classes="detail-dialog"):
            yield Label("Loading...", classes="detail-header", id="title")
            yield Label("", classes="stat-bar", id="stats")
            with VerticalScroll(classes="detail-content"):
                yield Markdown("", id="markdown_body")
            with Horizontal(classes="detail-actions"):
                yield Button("Install", variant="success", id="install_btn")
                yield Button("Close", variant="primary", id="close_btn")

    def on_mount(self) -> None:
        self.fetch_details()

    @work(thread=True)
    def fetch_details(self):
        try:
            data = self.api.get_project(self.slug)
            if data:
                self.project = Project.from_json(data)
                self.app.call_from_thread(self.update_ui)
            else:
                self.app.call_from_thread(self.notify, "Failed to load project details.", severity="error")
                self.app.call_from_thread(self.dismiss)
        except Exception as e:
             self.app.call_from_thread(self.notify, f"Error: {e}", severity="error")

    def update_ui(self):
        if not self.project:
            return
        
        self.query_one("#title", Label).update(f"{self.project.slug}")
        
        stats = f"Author: {self.project.author} | Downloads: {self.project.downloads} | License: {self.project.license}"
        self.query_one("#stats", Label).update(stats)
        
        body = self.project.body if self.project.body else self.project.description
        self.query_one("#markdown_body", Markdown).update(body)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close_btn":
            self.dismiss()
        elif event.button.id == "install_btn":
            self.action_install()

    def action_install(self):
        self.dismiss()
        self.install_callback(self.slug)
    
    def action_dismiss(self):
        self.dismiss()

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    
    CSS = """
    ConfirmScreen {
        align: center middle;
    }
    
    .confirm-dialog {
        padding: 1 2;
        background: $surface;
        border: solid $warning;
        width: 40%;
        height: auto;
        align: center middle;
    }
    
    .confirm-buttons {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    
    .confirm-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(classes="confirm-dialog"):
            yield Label(self.message, classes="confirm-message")
            with Horizontal(classes="confirm-buttons"):
                yield Button("Yes", variant="success", id="yes_btn")
                yield Button("No", variant="error", id="no_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes_btn":
            self.dismiss(True)
        else:
            self.dismiss(False)
    
    def action_cancel(self):
        self.dismiss(False)


# --- App ---

class AptMcApp(App):
    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    Header {
        background: $primary-darken-2;
        color: $text;
        height: 3;
        content-align: center middle; 
        text-style: bold;
    }
    
    Footer {
        background: $primary-darken-2;
        color: $text;
    }
    
    TabbedContent {
        height: 1fr;
    }
    
    TabPane {
        padding: 1;
    }

    DataTable {
        height: 1fr;
        border: solid $primary;
    }
    
    DataTable > .datatable--header {
        background: $primary;
        color: $text;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $accent;
        color: $text;
    }

    #search_input {
        dock: top;
        margin-bottom: 1;
        border: solid $accent;
        background: $surface-lighten-1;
    }
    
    .search-status {
        dock: bottom;
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_installed", "Refresh Installed"),
    ]

    def __init__(self):
        super().__init__()
        self.api = ModrinthClient()
        self.pm = PackageManager(PLUGINS_DIR, self.api)
        self.search_timer: Timer | None = None
        self.current_query: str = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with TabbedContent():
            with TabPane("Installed Plugins", id="tab_installed"):
                yield DataTable(id="installed_table", cursor_type="row")
            with TabPane("Search Modrinth", id="tab_search"):
                yield Input(placeholder="Search plugins...", id="search_input")
                yield DataTable(id="search_table", cursor_type="row")
                yield Label("", id="search_status", classes="search-status")

    def on_mount(self) -> None:
        # Setup Installed Table
        table = self.query_one("#installed_table", DataTable)
        table.add_columns("Plugin Name", "Version", "SHA1")
        self.refresh_installed()

        # Setup Search Table
        search_table = self.query_one("#search_table", DataTable)
        search_table.add_columns("Name", "Description", "Author", "Downloads", "Slug")

    def refresh_installed(self):
        table = self.query_one("#installed_table", DataTable)
        table.clear()
        
        plugins = self.pm.get_installed_plugins()
        if not plugins:
            return

        self.resolve_installed_versions(plugins)

    @work(thread=True)
    def resolve_installed_versions(self, plugins: dict):
        # Initial populate with filenames
        rows = []
        for filename, sha1 in plugins.items():
            rows.append((filename, "Loading...", sha1))
        
        self.call_from_thread(self._update_table, rows)

        # Fetch details
        try:
            versions_map = self.api.get_versions_by_hashes(list(plugins.values()))
            
            final_rows = []
            for filename, sha1 in plugins.items():
                v_info = versions_map.get(sha1)
                if v_info:
                    name = v_info.get('project_id', filename)
                    ver = v_info.get('version_number', 'unknown')
                else:
                    name = filename
                    ver = "unknown"
                final_rows.append((name, ver, sha1))
            
            self.call_from_thread(self._update_table, final_rows, clear=True)
        except Exception as e:
            pass

    def _update_table(self, rows, clear=False):
        table = self.query_one("#installed_table", DataTable)
        if clear:
            table.clear()
        for row in rows:
            table.add_row(*row, key=row[2]) # Key is SHA1

    @on(Input.Changed, "#search_input")
    def on_search_change(self, event: Input.Changed):
        if self.search_timer:
            self.search_timer.stop()
        
        self.current_query = event.value.strip()
        if self.current_query:
            self.query_one("#search_status", Label).update("Typing...")
            self.search_timer = self.set_timer(0.25, self.trigger_search)
        else:
            self.query_one("#search_status", Label).update("")
            self.query_one("#search_table", DataTable).clear()

    @on(Input.Submitted, "#search_input")
    def search_submit(self, event: Input.Submitted):
        if self.search_timer:
            self.search_timer.stop()
        self.current_query = event.value.strip()
        if self.current_query:
            self.perform_search(self.current_query)

    def trigger_search(self):
        self.perform_search(self.current_query)

    @work(thread=True)
    def perform_search(self, query: str):
        # Prevent race conditions by checking query
        if query != self.current_query:
            return
        
        self.call_from_thread(self.query_one("#search_status", Label).update, f"Searching for '{query}'...")

        try:
            hits = self.api.search(query)
        except Exception as e:
            self.call_from_thread(self.notify, f"Search error: {e}", severity="error")
            self.call_from_thread(self.query_one("#search_status", Label).update, "Error.")
            return

        # Check again before updating UI
        if query != self.current_query:
            return
            
        def update_table():
            if query != self.current_query:
                return
            table = self.query_one("#search_table", DataTable)
            table.clear()
            for hit in hits:
                slug = hit.get("slug")
                if not slug:
                    continue
                try:
                    table.add_row(
                        slug,
                        hit.get("description", "")[:50],
                        hit.get("author"),
                        str(hit.get("downloads")),
                        slug,
                        key=slug
                    )
                except Exception:
                    pass
            self.query_one("#search_status", Label).update(f"Found {len(hits)} results.")
        
        self.call_from_thread(update_table)

    @on(DataTable.RowSelected, "#search_table")
    def on_search_select(self, event: DataTable.RowSelected):
        slug = event.row_key.value
        # Show detail screen instead of direct install
        self.push_screen(PluginDetailScreen(slug, self.api, self.install_plugin_confirm))

    @on(DataTable.RowSelected, "#installed_table")
    def on_installed_select(self, event: DataTable.RowSelected):
        sha1 = event.row_key.value
        self.push_screen(ConfirmScreen(f"Delete plugin with SHA {sha1[:8]}?"), lambda res: self.delete_plugin(sha1) if res else None)

    def install_plugin_confirm(self, slug: str):
        self.push_screen(ConfirmScreen(f"Confirm install: {slug}?"), lambda res: self.install_plugin(slug) if res else None)

    @work(thread=True)
    def install_plugin(self, slug: str):
        self.call_from_thread(self.notify, f"Starting install for {slug}...", title="Info")
        
        try:
            p = self.api.get_project(slug)
            if not p:
                self.call_from_thread(self.notify, "Project not found.", severity="error")
                return
            
            versions = self.api.get_versions(p['id'], LOADERS)
            if not versions:
                self.call_from_thread(self.notify, "No compatible versions found.", severity="error")
                return

            latest = versions[0]
            version_number = latest.get("version_number", "unknown")
            
            files = latest.get("files", [])
            primary_file = next((f for f in files if f.get("primary")), files[0] if files else None)
            
            if primary_file:
                filename = primary_file["filename"]
                self.call_from_thread(self.notify, f"Downloading {filename}...", title="Info")
                
                # Silent download to avoid messing up TUI
                self.pm.download_files([{
                    "url": primary_file["url"],
                    "filename": filename,
                    "size": primary_file["size"]
                }], quiet=True)
                
                # Verify installation
                dest_path = self.pm.plugins_dir / filename
                if dest_path.exists():
                    self.call_from_thread(self.notify, f"Installed {slug} ({version_number})", title="Success")
                else:
                    self.call_from_thread(self.notify, f"Failed to install {slug} (File missing)", severity="error")

                import time
                time.sleep(1)
            else:
                self.call_from_thread(self.notify, "No files found in latest version.", severity="error")
        except Exception as e:
            self.call_from_thread(self.notify, f"Error: {e}", severity="error")
            import traceback
            traceback.print_exc()
    
        self.call_from_thread(self.refresh_installed)

    @work(thread=True)
    def delete_plugin(self, sha1: str):
        plugins = self.pm.get_installed_plugins()
        filename = next((f for f, s in plugins.items() if s == sha1), None)
        
        if filename:
            self.pm.remove_plugin(filename)
            self.call_from_thread(self.notify, f"Removed {filename}")
            self.call_from_thread(self.refresh_installed)
        else:
            self.call_from_thread(self.notify, "Could not find file to remove.", severity="error")

if __name__ == "__main__":
    app = AptMcApp()
    app.run()
