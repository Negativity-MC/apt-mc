# apt-mc

The Advanced Packaging Tool for Minecraft. A CLI plugin manager for Spigot, Paper, Purpur, and Bukkit servers, powered by the Modrinth API.

## Features
- **Parody Interface**: Mimics the familiar Debian/Ubuntu `apt` commands.
- **Modrinth Integration**: Searches and downloads plugins directly from Modrinth.
- **Smart Filtering**: Automatically filters for Spigot/Paper compatible plugins (ignoring mods).
- **Colorful Output**: Uses `rich` for a modern CLI experience.

## Installation

1.  Clone this repository.
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
3.  Run the script:
    ```bash
    chmod +x main.py
    ./main.py --help
    ```

## Usage

### Update
Updates the local "package lists" (mostly for show, ensures API connectivity).
```bash
./main.py update
```

### Search
Search for plugins on Modrinth.
```bash
./main.py search <query>
```
Example: `./main.py search worldedit`

### Install
Download and install a plugin to the `./plugins` directory.
```bash
./main.py install <plugin_slug>
```
Example: `./main.py install worldedit`

### Remove
Remove a plugin from the `./plugins` directory.
```bash
./main.py remove <plugin_slug>
```
Example: `./main.py remove worldedit`