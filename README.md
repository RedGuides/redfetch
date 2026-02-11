![six wizards levitating a package, the word redfetch underneath](https://www.redguides.com/images/redfetchlogo.png)

redfetch is for updating software and scripts for EverQuest that RedGuides recommends, as well as those you "[watch](https://www.redguides.com/community/watched/resources)". It's also open source, how nice.

## Installation

On Windows the easiest way to install redfetch is to [download](https://www.redguides.com/community/resources/redfetch.3177/download) and run [`redfetch.exe`](https://www.redguides.com/community/resources/redfetch.3177/download). (*optional: If you're still on Windows 10 and want a more modern appearance, follow [this guide](https://www.redguides.com/community/threads/redfetch.92998/post-634938) to set [Windows Terminal](https://www.redguides.com/community/threads/redfetch.92998/post-634938) as your default terminal.*)

<details>
<summary>Terminal / Python / Linux</summary>


Make sure you have a recent version of [Python](https://www.python.org/downloads/)

1) **Install pipx**
```bash
python -m pip install --user pipx
```

2) **Make it so you can run packages without having to type "python -m"**
```bash
python -m pipx ensurepath
```

3) **Install redfetch**
```bash
pipx install redfetch
```

When you open a new terminal window, you'll be able to run redfetch by typing `redfetch` from the command line. 

</details>

## Usage


### 1) Double-click [`redfetch.exe`](https://www.redguides.com/community/resources/redfetch.3177/download) to run the script. 
Take a moment to consider your configuration and the settings tab.

### 2) Click the big blue "Easy Update" button, and then "Yes" or "Always" on the popup. 
![a screenshot showing the easy update button](https://www.redguides.com/images/redfetchupdate.gif)  
(It's updating *Very Vanilla MQ* and any of its scripts or plugins you have [watched on RedGuides](https://www.redguides.com/community/watched/resources), your licensed resources, and scripts recommended by staff. You can customize this if you like.)

Now you're ready to play EQ with the big boys.

## Add more MQ Scripts
To add more MacroQuest scripts, "watch" them on [www.redguides.com/community/resources](https://www.redguides.com/community/resources), and then click the *Easy Update* button again.

![a screenshot showing the watch button on a resource page](https://www.redguides.com/images/clickwatch.gif)

If there are non-MQ resources you'd like to keep in sync with redfetch, you can add them as a "special resource" in the local settings file, as shown in settings section.

## Command Line
To run redfetch from the command line:

| .exe file | python |
|---------|-----------|
| `.\redfetch.exe update` | `redfetch update` |

![a screenshot showing the command line interface](https://www.redguides.com/images/redfetchcliupdate.gif)

## Command Line Reference

> Run `redfetch --help` or `.\redfetch.exe --help` to see something like this in your terminal:
> 
> ### 📦 Resource Management
> - `update` - Update all watched and special resources
>   - `--force` / `-f` - Force re-download of all watched resources
> - `download <ID_OR_URL>` - Download a specific resource by ID or URL
>   - `--force` / `-f` - Force re-download by resetting this resource's download date
> - `list` - List resources and dependencies in the cache database
> - `reset` - Reset download dates for watched resources
> 
> ### 🍔 Configuration
> - `server <SERVER>` - Switch the current server/environment to `LIVE`, `TEST`, or `EMU`
> - `config <SETTING_PATH> <VALUE>` - Update a setting by path and value
>   - `SETTING_PATH` - Dot-separated setting path (e.g., `SPECIAL_RESOURCES.1974.opt_in`)
>   - `VALUE` - New value for the setting
>   - `--server` / `-s` - Server to apply the change in (`LIVE`, `TEST`, `EMU`)
> - `status` - Show the configuration for the current or specified server
>   - `--server` / `-s` - Server to show (defaults to current)
> 
> ### 🔧 System & Utilities
> - `ui` - Launch the Terminal User Interface
> - `web` - Launch the RedGuides.com web interface
> - `version` - Show version and exit
> - `logout` - Disconnect your account from redfetch
> - `uninstall` - Uninstall redfetch and clean up data
> 
> ### 📤 Publishing
> - `publish <resource_id>` - Publish an update to you or your team's resource. [There's also a github action for this.](https://github.com/marketplace/actions/redguides-publish)
>   - `resource_id` - Existing RedGuides resource ID
>   - `--description <README.md>` / `-d` - Path to a file (e.g. `README.md`) that will become the resource's overview description
>   - `--version <version_number>` / `-v` - New version string (e.g., `v1.0.1`)
>   - `--message <CHANGELOG.md | MESSAGE>` / `-m` - Version update message, a message file (e.g. `message.md` / `message.txt`), or a `CHANGELOG.md` (keep a changelog) file.
>   - `--file <FILE.zip>` / `-f` - Path to your zipped release file
>   - `--domain <URL>` - Domain to prepend to relative URLs in README.md or CHANGELOG.md files. (mostly for images. e.g., `https://raw.githubusercontent.com/yourusername/yourrepo/main/`)

## Settings

`settings.local.toml` is found in your configuration directory, which by default is `c:\Users\Public\redfetch\settings.local.toml`. Any keys you add will override their default values in [`settings.toml`](./src/redfetch/settings.toml).

All settings are prefixed with the environment,

- `[DEFAULT]` - encompasses all environments that are not explicitly defined.
- `[LIVE]` - EverQuest Live
- `[TEST]` - EverQuest Test
- `[EMU]` - EverQuest Emulator

### Adding a special resource
To add a "special resource" (a non-MQ resource that you want to keep updated), open `settings.local.toml` and add an entry. You'll need the [resource ID (numbers at the end of the url)](https://www.redguides.com/community/resources/brewalls-everquest-maps.153/) and a target directory. Example:

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
navmesh = ["befallen.navmesh", "innothuleb.navmesh"]
```

## Tinkerers

If you self-compile MacroQuest or use a discord friend's copy, you can still keep your scripts and plugins in sync with redfetch by opting out of Very Vanilla:

```powershell
redfetch.exe config SPECIAL_RESOURCES.1974.opt_in false --server LIVE
redfetch.exe config SPECIAL_RESOURCES.60.opt_in false --server EMU
redfetch.exe config SPECIAL_RESOURCES.2218.opt_in false --server TEST
```

Then assign the *Very Vanilla MQ* path to your self-compiled MacroQuest.

## Trailmap
- Add custom buttons for "fetch" tab.
- Option: Close after update
- Launch programs with cli options
- Indicate when updated VV is available
- Launch more than just mq (eqbcs, etc) upon update. 
- Run from MQ
- Deeper integration with the forums

## Contributing

I'd love help, conceptually and technically. I'm not a developer and this is my first big python script. 

> [!NOTE]
> This project is built with LLM assistance.

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
