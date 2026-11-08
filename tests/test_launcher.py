"""Starting the app with a double-click: the launcher files for each system, and the `tradingagents shortcut` command.

No real shortcut is made: the PowerShell step is a stand-in and every file goes into a temporary folder."""

import os
import struct
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import main as cli_main
from tradingagents import launcher as lc

EXE = r"C:\Program Files\Trading\.venv\Scripts\tradingagents.exe"


class FakePowerShell:
    """Stands in for powershell: answers the folder question and records the shortcut script it is given."""

    def __init__(self, folder: Path, fail: str = ""):
        self.folder, self.fail, self.scripts = folder, fail, []

    def __call__(self, command, **kwargs):
        script = command[-1]
        self.scripts.append(script)
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.fail)
        if script.startswith("[Environment]::GetFolderPath"):
            return SimpleNamespace(returncode=0, stdout=str(self.folder) + "\r\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="made\r\n", stderr="")


@pytest.fixture
def home(tmp_path):
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    (folder / "sub").mkdir(parents=True)
    (folder / ".env").write_text("X=1\n", encoding="utf-8")
    return folder


@pytest.fixture(autouse=True)
def _powershell_present(monkeypatch):
    monkeypatch.setattr(lc.shutil, "which", lambda name: "powershell")


@pytest.mark.unit
class TestPieces:
    @pytest.mark.parametrize("platform, expected", [("win32", "windows"), ("darwin", "mac"), ("linux", "linux"), ("freebsd14", "linux")])
    def test_the_system_is_named(self, platform, expected):
        assert lc.system_name(platform) == expected

    def test_the_program_beside_this_python_is_used_when_there_is_one(self, tmp_path):
        (tmp_path / "python.exe").write_text("")
        assert lc.program(str(tmp_path / "python.exe")) == [str(tmp_path / "python.exe"), "-m", "cli.main", "ui"]
        (tmp_path / "tradingagents.exe").write_text("")
        assert lc.program(str(tmp_path / "python.exe")) == [str(tmp_path / "tradingagents.exe"), "ui"]
        (tmp_path / "tradingagents.exe").unlink()
        (tmp_path / "tradingagents").write_text("")
        assert lc.program(str(tmp_path / "python.exe")) == [str(tmp_path / "tradingagents"), "ui"]

    def test_it_starts_where_the_env_file_is_found_as_the_program_finds_it(self, project):
        assert lc.start_folder(project) == (project, True)
        assert lc.start_folder(project / "sub") == (project, True)             # searched upward, like the program's own search

    def test_with_no_env_file_it_starts_in_the_current_folder(self, tmp_path):
        lonely = tmp_path / "lonely"
        lonely.mkdir()
        if any((p / ".env").is_file() for p in (lonely, *lonely.parents)):
            pytest.skip("this machine has a .env above the temporary folder")
        assert lc.start_folder(lonely) == (lonely, False)

    def test_the_icon_files_are_real_images(self):
        png = lc.icon_file("png").read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (256, 256)
        ico = lc.icon_file("ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", ico[:6])
        assert (reserved, kind) == (0, 1) and count == 7
        for i in range(count):
            width, _h, _c, _r, _planes, bits, size, offset = struct.unpack("<BBBBHHII", ico[6 + 16 * i:22 + 16 * i])
            assert bits == 32 and ico[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + size <= len(ico)
        assert "assets/*.ico" in Path("pyproject.toml").read_text(encoding="utf-8")        # shipped with the package


@pytest.mark.unit
class TestWindows:
    def test_powershell_strings_double_a_quote_and_nothing_else(self):
        assert lc.ps_quote("C:\\Users\\O'Brien") == "'C:\\Users\\O''Brien'" and lc.ps_quote("a$b`c") == "'a$b`c'"

    def test_the_command_names_the_window_quotes_every_part_and_waits_only_after_a_failure(self):
        assert lc.windows_arguments([EXE, "ui"]) == f'/s /c "title Analyst Consortium & "{EXE}" "ui" || pause"'

    @pytest.mark.parametrize("path", ["C:\\a&b\\x.exe", "C:\\100%\\x.exe", "C:\\a^b\\x.exe", "C:\\a|b\\x.exe", "C:\\a!b\\x.exe"])
    def test_a_path_that_would_confuse_the_command_line_is_refused_in_plain_words(self, path):
        with pytest.raises(lc.ShortcutError, match="cannot carry"):
            lc.windows_arguments([path, "ui"])

    def test_the_script_sets_the_target_folder_icon_and_description(self, tmp_path):
        script = lc.windows_script(tmp_path / "Analyst Consortium.lnk", [EXE, "ui"], Path("F:/O'Brien/Trading"), Path("C:/x/app-icon.ico"))
        assert "CreateShortcut(" in script and "$s.TargetPath = $env:ComSpec" in script
        assert "$s.WorkingDirectory = 'F:" in script and "O''Brien" in script and "$s.IconLocation = 'C:" in script
        assert "app-icon.ico'" in script and "$s.Save()" in script

    def test_a_shortcut_is_made_in_the_desktop_folder_windows_names(self, tmp_path, project, monkeypatch):
        monkeypatch.setattr(lc.importlib.util, "find_spec", lambda name: object())          # the page's libraries are installed
        desktop = tmp_path / "OneDrive" / "Desktop"
        shell = FakePowerShell(desktop)
        made = lc.create(system="windows", run=shell, cwd=project, python=EXE)
        assert made.path == desktop / "Analyst Consortium.lnk" and made.start == project and made.notes == []
        assert shell.scripts[0] == "[Environment]::GetFolderPath('Desktop')" and "CreateShortcut(" in shell.scripts[1]
        assert str(project) in shell.scripts[1] and "title Analyst Consortium" in shell.scripts[1]
        assert desktop.is_dir()                                                  # made if it was not there

    def test_the_start_menu_and_other_places(self, tmp_path, project):
        menu = tmp_path / "Programs"
        shell = FakePowerShell(menu)
        assert lc.create("start-menu", system="windows", run=shell, cwd=project).path == menu / "Analyst Consortium.lnk"
        assert shell.scripts[0] == "[Environment]::GetFolderPath('Programs')"
        elsewhere = tmp_path / "chosen"
        shell = FakePowerShell(menu)
        assert lc.create(folder=elsewhere, system="windows", run=shell, cwd=project).path == elsewhere / "Analyst Consortium.lnk"
        assert len(shell.scripts) == 1                                           # no need to ask Windows where the desktop is
        assert lc.create("here", system="windows", run=FakePowerShell(menu), cwd=project).path == project / "Analyst Consortium.lnk"

    def test_what_windows_says_when_it_refuses_is_passed_on(self, tmp_path, project):
        with pytest.raises(lc.ShortcutError, match="Windows would not make the shortcut: access denied"):
            lc.create(system="windows", run=FakePowerShell(tmp_path, fail="access denied"), cwd=project)

    def test_a_missing_powershell_is_said_plainly(self, monkeypatch, tmp_path, project):
        monkeypatch.setattr(lc.shutil, "which", lambda name: None)
        with pytest.raises(lc.ShortcutError, match="PowerShell was not found"):
            lc.create(system="windows", run=FakePowerShell(tmp_path), cwd=project)

    def test_removing_deletes_only_the_launcher_this_made(self, tmp_path, project):
        desktop = tmp_path / "Desktop"
        desktop.mkdir()
        (desktop / "Analyst Consortium.lnk").write_text("x")
        (desktop / "Notes.lnk").write_text("keep me")
        assert lc.remove(system="windows", run=FakePowerShell(desktop), cwd=project) == desktop / "Analyst Consortium.lnk"
        assert [p.name for p in desktop.iterdir()] == ["Notes.lnk"]
        assert lc.remove(system="windows", run=FakePowerShell(desktop), cwd=project) is None


@pytest.mark.unit
class TestMacAndLinux:
    PROGRAM = ["/opt/my tools/.venv/bin/tradingagents", "ui"]

    def test_the_mac_file_starts_in_the_project_and_waits_only_after_a_failure(self):
        text = lc.mac_script(self.PROGRAM, PurePosixPath("/Users/me/My Trading"))          # a Mac's own kind of path, whatever runs the test
        lines = text.splitlines()
        assert lines[0] == "#!/bin/bash" and "cd '/Users/me/My Trading' || exit 1" in lines
        assert "'/opt/my tools/.venv/bin/tradingagents' ui" in lines and "read -n 1" in text and lines[-1] == "exit $status"

    def test_a_path_with_a_quote_in_it_cannot_break_out_of_the_mac_script(self):
        text = lc.mac_script(["/a/b'; rm -rf ~; echo '/x", "ui"], PurePosixPath("/p"))
        assert "'/a/b'\"'\"'; rm -rf ~; echo '\"'\"'/x' ui" in text

    def test_exec_arguments_follow_the_desktop_entry_quoting_rules(self):
        assert lc.desktop_quote('/a b/$x"y`z\\w%') == '"/a b/\\$x\\"y\\`z\\\\w%%"'

    def test_the_linux_entry_names_the_program_folder_icon_and_a_terminal(self):
        text = lc.linux_entry(self.PROGRAM, PurePosixPath("/home/me/trading"), PurePosixPath("/x/app-icon.png"))
        assert text.splitlines()[0] == "[Desktop Entry]" and 'Exec="/opt/my tools/.venv/bin/tradingagents" "ui"' in text
        assert "Path=/home/me/trading" in text and "Icon=/x/app-icon.png" in text and "Terminal=true" in text and "Name=Analyst Consortium" in text

    def test_a_mac_launcher_goes_on_the_desktop_and_is_executable(self, home, project):
        made = lc.create(system="mac", home=home, cwd=project, python="/opt/py/bin/python")
        assert made.path == home / "Desktop" / "Analyst Consortium.command" and made.start == project
        assert made.path.read_text(encoding="utf-8").startswith("#!/bin/bash") and str(project) in made.path.read_text(encoding="utf-8")
        if os.name != "nt":
            assert os.access(made.path, os.X_OK)

    def test_a_linux_launcher_asks_the_system_where_the_desktop_is(self, home, project, tmp_path):
        desktop = tmp_path / "Schreibtisch"

        def run(command, **kwargs):
            assert command == ["xdg-user-dir", "DESKTOP"]
            return SimpleNamespace(returncode=0, stdout=str(desktop) + "\n", stderr="")

        made = lc.create(system="linux", run=run, home=home, cwd=project)
        assert made.path == desktop / "Analyst Consortium.desktop" and "[Desktop Entry]" in made.path.read_text(encoding="utf-8")

    def test_without_the_desktop_tool_it_uses_the_desktop_folder_and_start_menu_places(self, home, project):
        def broken(command, **kwargs):
            raise FileNotFoundError("xdg-user-dir")

        assert lc.create(system="linux", run=broken, home=home, cwd=project).path == home / "Desktop" / "Analyst Consortium.desktop"
        assert lc.create("start-menu", system="linux", run=broken, home=home, cwd=project).path == \
            home / ".local/share/applications" / "Analyst Consortium.desktop"
        assert lc.create("start-menu", system="mac", home=home, cwd=project).path == home / "Applications" / "Analyst Consortium.command"

    def test_remove_on_linux_and_mac(self, home, project):
        made = lc.create(system="mac", home=home, cwd=project)
        assert lc.remove(system="mac", home=home, cwd=project) == made.path and not made.path.exists()
        assert lc.remove(system="mac", home=home, cwd=project) is None


@pytest.mark.unit
class TestNotesAndErrors:
    def test_no_env_file_is_said_and_the_launcher_still_works(self, tmp_path, home):
        lonely = tmp_path / "a" / "b"
        lonely.mkdir(parents=True)
        if any((p / ".env").is_file() for p in (lonely, *lonely.parents)):
            pytest.skip("this machine has a .env above the temporary folder")
        made = lc.create(system="mac", home=home, cwd=lonely)
        assert made.start == lonely and any("No .env file was found" in n for n in made.notes) and made.path.exists()

    def test_missing_page_libraries_are_said(self, monkeypatch, home, project):
        monkeypatch.setattr(lc.importlib.util, "find_spec", lambda name: None)
        notes = lc.create(system="mac", home=home, cwd=project).notes
        assert any('pip install "tradingagents[ui]"' in n for n in notes)

    def test_a_folder_that_cannot_be_made_is_a_plain_error(self, home, project, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(lc.ShortcutError, match="Could not use the folder"):
            lc.create(folder=blocker / "inside", system="mac", home=home, cwd=project)

    def test_an_unknown_place_is_refused(self, home, project):
        with pytest.raises(lc.ShortcutError, match="Choose where from"):
            lc.create("moon", system="mac", home=home, cwd=project)


@pytest.mark.unit
class TestCommand:
    def test_the_help_explains_what_it_does(self):
        out = CliRunner().invoke(cli_main.app, ["shortcut", "--help"]).output
        assert "double-click" in out and "--remove" in out and "--where" in out and "--folder" in out

    def test_it_says_where_the_launcher_is_where_it_starts_and_how_to_remove_it(self, monkeypatch, tmp_path):
        made = lc.Made(tmp_path / "Analyst Consortium.lnk", tmp_path, ["x", "ui"], ["Note one."])
        calls = []
        monkeypatch.setattr(lc, "create", lambda where, folder: calls.append((where, folder)) or made)
        result = CliRunner().invoke(cli_main.app, ["shortcut", "--where", "start-menu"])
        assert result.exit_code == 0 and calls == [("start-menu", None)]
        assert f"Created: {made.path}" in result.output and f"starts from {tmp_path}" in result.output
        assert "tradingagents shortcut --remove" in result.output and "Note: Note one." in result.output

    def test_an_error_is_one_plain_line_and_exit_code_1(self, monkeypatch):
        def fail(where, folder):
            raise lc.ShortcutError("Windows would not make the shortcut: access denied")

        monkeypatch.setattr(lc, "create", fail)
        result = CliRunner().invoke(cli_main.app, ["shortcut"])
        assert result.exit_code == 1 and "Error: Windows would not make the shortcut: access denied" in result.output

    def test_an_unknown_place_is_a_usage_error(self):
        result = CliRunner().invoke(cli_main.app, ["shortcut", "--where", "moon"])
        assert result.exit_code == 2 and "desktop, start-menu, here" in result.output

    def test_remove_says_what_it_removed_or_that_there_was_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lc, "remove", lambda where, folder: tmp_path / "Analyst Consortium.lnk")
        assert "Removed" in CliRunner().invoke(cli_main.app, ["shortcut", "--remove"]).output
        monkeypatch.setattr(lc, "remove", lambda where, folder: None)
        assert "no shortcut there to remove" in CliRunner().invoke(cli_main.app, ["shortcut", "--remove"]).output
