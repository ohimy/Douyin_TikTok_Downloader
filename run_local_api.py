"""PyInstaller entrypoint; keep package imports stable in a frozen executable."""

from local_api.main import main


if __name__ == "__main__":
    main()
