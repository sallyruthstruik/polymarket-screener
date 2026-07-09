#!/usr/bin/env python
import os
import sys


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    from django.core.management import execute_from_command_line

    execute_from_command_line(argv or sys.argv)


if __name__ == "__main__":
    main()
