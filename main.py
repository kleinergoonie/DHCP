"""Entry point for the DHCP Server GUI application."""

import sys


def main() -> None:
    from src.gui import DHCPApp
    app = DHCPApp()
    app.mainloop()


if __name__ == '__main__':
    # On Windows, running as administrator is required for port 67.
    if sys.platform == 'win32':
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            # Re-launch the process with administrator privileges.
            ctypes.windll.shell32.ShellExecuteW(
                None, 'runas', sys.executable, ' '.join(sys.argv), None, 1
            )
            sys.exit(0)

    main()
