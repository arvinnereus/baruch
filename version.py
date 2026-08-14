"""Single source of truth for the app version.

Bump VERSION whenever code ships, and add the matching entry to CHANGELOG.md:
  PATCH  fixes only            1.0.0 -> 1.0.1
  MINOR  new user-facing thing 1.0.1 -> 1.1.0
  MAJOR  reserved for a rewrite / breaking data change

The self-update system compares this string between the running server and the
code on disk, so a bump is what makes the Update banner truthful.
"""

VERSION = "1.6.1"
RELEASED = "2026-08-13"
CODENAME = "Caleb"  # the machine this release was built and proven on
