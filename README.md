# redfetch

redfetch is for downloading EverQuest software and scripts that you "[watch](https://www.redguides.com/community/watched/resources)" on RedGuides. 

## Installation

On Windows the easiest way to install redfetch is to download and run `redfetch.exe`

### Alternative: Python
If you have a recent version of Python, you can install redfetch with pip,

```bash
pip install redfetch
```

## Usage

To update everything you've watched from the command line,

```bash
redfetch.exe --download-watched
```
or if you installed with pip,

```bash
redfetch --download-watched
```

This will update *Very Vanilla MacroQuest* and any of its scripts or plugins you have [watched on RedGuides](https://www.redguides.com/community/watched/resources), your licensed resources, and scripts recommended by staff.

To add more MacroQuest scripts, "watch" them on RedGuides. 

![a screenshot showing the watch button on a resource page](./images/watch.png)

If there are non-MQ resources you'd like to keep in sync with redfetch, you can add them as a "special resource" in the [local settings file](#settings). 

## Alternative Interfaces

### Terminal UI
Run the script without any arguments to access the terminal UI. If you have a modern terminal like [Windows Terminal](https://apps.microsoft.com/store/detail/windows-terminal/9N0DX20HK701), it will look like this:

![redfetch Terminal UI, showing the settings tab](./images/terminal-ui.png)

### Web UI
Another UI option! Run this command and then browse https://www.redguides.com/community/resources
```bash
redfetch.exe --serve
```

![redfetch Web UI, with a hastily drawn circle around the install button](./images/webui.png)

## Command-Line Options

- `--download-resource <RESOURCE_ID | URL>`: Downloads a resource by its ID or URL.
- `--download-watched`: Downloads all watched and special resources.
- `--force-download`: Clears recent download dates in the cache.
- `--list-resources`: Lists all resources in the cache.
- `--serve`: Runs as a flask server to interact with the web UI.
- `--update-setting <SETTING_PATH> <VALUE> [ENVIRONMENT]`: Updates a configuration setting. The setting path should be dot-separated. Environment is optional.
- `--switch-env <ENVIRONMENT>`: Changes the server type (`LIVE`, `TEST`, `EMU`).
- `--logout`: Logs out and clears cached tokens.
- `--uninstall`: Uninstalls redfetch and outputs a text guide for cleaning up downloaded data.
- `--version`: Displays the current version of redfetch.
- `push <resource_id> [options]`: Update you or your team's resource. [There's also a github action for this.](https://github.com/marketplace/actions/redguides-publish) Options include:
  - `--description <README.md>`: Path to a description file which will become the resource's overview description.
  - `--version <version_number>`: Specifies a new version number.
  - `--message <CHANGELOG.md | MESSAGE>`: Version update message or path to a changelog file.
  - `--file <FILE.zip>`: Path to the zipped release file.
  - `--domain <URL>`: Domain to prepend to relative URLs in README.md or CHANGELOG.md files. (mostly for images. e.g., `https://raw.githubusercontent.com/yourusername/yourrepo/main/`)

## Settings

`settings.local.toml` is found in your configuration directory, which by default is `c:\Users\Public\redfetch\settings.local.toml`. Any keys you add will override their default values in [`settings.toml`](./src/redfetch/settings.toml).

All settings are prefixed with the environment,

- `[DEFAULT]` - encompasses all environments that are not explicitly defined.
- `[LIVE]` - EverQuest Live
- `[TEST]` - EverQuest Test
- `[EMU]` - EverQuest Emulator

### Adding a special resource
Here's how to add a non-MQ resource. You need the [resource ID (numbers at the end of the url)](https://www.redguides.com/community/resources/brewalls-everquest-maps.153/) and a target directory.

```toml
[LIVE.SPECIAL_RESOURCES.153]
custom_path = 'C:\Users\Public\Daybreak Game Company\Installed Games\EverQuest\maps\Brewall_Maps'
opt_in = true
```
* Note the use of single quotes around the path, which are required for windows paths.

The above will install Brewall's maps to the EQ maps directory the next time `--download-watched` is run for `LIVE` servers.

### Overwrite protection

If there are local files you don't want overwritten by a resource, you can add them to the `PROTECTED_FILES_BY_RESOURCE` setting. Include the resource ID and files you want to protect. e.g.,

```toml
[LIVE.PROTECTED_FILES_BY_RESOURCE]
1974 = ["CharSelect.cfg", "Zoned.cfg", "MQ2Map.ini", "MQ2MoveUtils.ini"]
153 = ["citymist.txt", "innothule.txt", "oasis.txt"]
```

## Tinkerers

If you self-compile MacroQuest or use a discord friend's copy, you can still keep your scripts and plugins in sync with redfetch by opting out of Very Vanilla:

```powershell
redfetch.exe --update-setting SPECIAL_RESOURCES.1974.opt_in false LIVE
redfetch.exe --update-setting SPECIAL_RESOURCES.60.opt_in false EMU
redfetch.exe --update-setting SPECIAL_RESOURCES.2218.opt_in false TEST
```
or edit the `settings.local.toml` file directly:
```toml
[LIVE.SPECIAL_RESOURCES.1974]
opt_in = false
```
Then assign the *Very Vanilla MQ* path to your self-compiled MacroQuest.

## Known Issues
- Directory selectors can't change drive letters yet. To change the drive letter in a path, you'll need to type the new drive letter in the input field.

## Todo
- Instead of keeping a record of each file downloaded and its version, we should reference the file's hash.
- Re-write auth for latest Xenforo version.

## Contributing

I'd love help. I'm not a developer and this is my first big python project.

To set up a [development environment](https://hatch.pypa.io/latest/environment/),

```bash
git clone https://github.com/RedGuides/redfetch
cd redfetch
pip install hatch
hatch env create dev
hatch shell dev
```
You can then run your dev version with,

`redfetch`

Or if the issue is ui-specific, run the [terminal UI in debug mode](https://textual.textualize.io/guide/devtools/#live-editing),

`textual run --dev .\src\redfetch\main.py`

When you're done, type `exit` to leave the shell.
